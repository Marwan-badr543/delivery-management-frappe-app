"""Sales statistics per salesman, counted from **paid** Sales Invoices only.

Rules (each one is covered by ``tests/test_sales_stats.py``):

* **Salesman** = the cashier who placed the sale. POS Awesome V15 lets several
  cashiers share one login and switch with a PIN; it records the verified cashier in
  ``posa_cashier``. So the salesman is ``posa_cashier``, or the user who created the
  invoice (``owner``) when there is none (e.g. a credit note made in the desk).
* **Period** = the date the invoice was **created** (the day the sale was made),
  not its Posting Date: the POS lets the cashier change the Posting Date (e.g.
  to the delivery day), and such a sale must still count on the day it was made.
* **Paid** = submitted, not a return, nothing outstanding (status Paid, or Credit
  Note Issued after a return). Drafts, cancelled, unpaid and partly paid
  invoices are left out; an unpaid order counts once it is fully paid (in the
  period it was created).
* **Returns** = submitted credit notes against a paid invoice, created in the
  period. Their negative amounts and units are netted in, so a returned sale
  does not earn commission.
* **Total** = what the customer pays: the rounded total (the grand total when
  rounding is disabled), with VAT. **Net Total** = before VAT. Company currency.
* **Services** = lines whose Item Group is one of the Service Item Groups of Delivery
  Settings (or under one). By default they are left out of the sales (Total, Net Total)
  and so of the commission; the page can count them in. A service line's share of the
  Total is its net amount × the invoice's Total ÷ Net Total (its share of VAT and
  rounding).
* **Units** (AC, non-AC, small items) are frozen on each invoice line when it is
  saved, with Product Bundles counted by their contents: see ``stats/units.py``.
* **Commission** is worked out **month by month** (``month_commission``). Each month uses
  the salesman's Sales Commission Rule of that month (User ▸ Commission Rules), or else
  the closest one: its Small Items Target sets the commission % between its Min and Max.
  75% of it is paid on the month's sales, the other 25% times the score of the Sales
  Evaluation overlapping the month (the latest), or else the closest one. A period's
  commission is the sum of its months. The Sales Commission Payment uses the same code.
* **Paid commission**: an invoice paid on a submitted Sales Commission Payment carries it
  in ``dm_commission_payment``. A return against such an invoice is never subtracted (the
  salesman already got the money); a return of an invoice not paid yet is.

The salesman totals are grouped in SQL from the same per-invoice query that
feeds the invoice list, so the table and the list always agree. The cost is one
pass over the period's invoices and their lines (index on company, docstatus,
creation), then a few small lookups per salesman (targets, evaluations). The
summary is one row per salesman; the invoice list is read a page at a time.
Everything is read with SQL (no documents are loaded).
"""

from __future__ import annotations

import datetime

import frappe
from frappe.utils import add_days, add_months, cint, flt, get_last_day, getdate, now_datetime


# A paid sale, or a credit note against a paid sale.
PAID_CONDITION = """(
	(si.is_return = 0 and si.outstanding_amount <= 0)
	or (si.is_return = 1 and exists (select 1 from `tabSales Invoice` orig where orig.name = si.return_against
		and orig.docstatus = 1 and orig.is_return = 0 and orig.outstanding_amount <= 0)))"""

TOTAL_COLUMN = "if(si.disable_rounded_total = 1 or si.base_rounded_total = 0, si.base_grand_total, si.base_rounded_total)"
SERVICE_NET = "coalesce(sum(if(sii.item_group in %(services)s, sii.base_net_amount, 0)), 0)"
SERVICE_TOTAL = f"if(si.base_net_total = 0, 0, {SERVICE_NET} * {TOTAL_COLUMN} / si.base_net_total)"

COUNT_KEYS = ("invoices", "orders", "returns")
UNIT_KEYS = ("ac_units", "non_ac_units", "small_units")
MONEY_KEYS = ("total", "net_total", "services")
SUM_KEYS = COUNT_KEYS + MONEY_KEYS + UNIT_KEYS
BASES = ("total", "net_total")  # the commission is on the Total (with VAT) or the Net Total; the page chooses
INVOICES_PER_PAGE = 100
ITEMS_SHARE = 0.75  # paid on the small-items commission %; the rest is paid on the evaluation score
EVALUATION = "Sales Evaluation"


def _salesman() -> str:
	"""The salesman frozen on the invoice at submit (``dm_salesman``); older invoices: the cashier, else the owner."""
	if frappe.db.has_column("Sales Invoice", "posa_cashier"):
		return "coalesce(nullif(si.dm_salesman, ''), nullif(si.posa_cashier, ''), si.owner)"
	return "coalesce(nullif(si.dm_salesman, ''), si.owner)"


def salesman_of(doc) -> str:
	return doc.get("posa_cashier") or doc.owner


def set_salesman(doc, method=None):
	"""Sales Invoice ``before_submit``: freeze who made the sale (indexed, for the commission payment)."""
	doc.dm_salesman = salesman_of(doc)


# A return whose original sale's commission was already paid (on another payment) is not
# subtracted: the salesman already took that money.
RETURN_OF_PAID = """(si.is_return = 1 and exists (select 1 from `tabSales Invoice` comm
	where comm.name = si.return_against and comm.dm_commission_payment is not null
	and (si.dm_commission_payment is null or si.dm_commission_payment != comm.dm_commission_payment)))"""


def service_groups() -> tuple[str, ...]:
	"""The Service Item Groups of Delivery Settings and every group under them."""
	roots = frappe.get_all("Delivery Service Item Group", filters={"parent": "Delivery Settings"}, pluck="item_group")
	if not roots:
		return ()
	ranges = frappe.get_all("Item Group", filters={"name": ["in", roots]}, fields=["lft", "rgt"])
	return tuple(frappe.get_all("Item Group", or_filters=[["lft", "between", [r.lft, r.rgt]] for r in ranges],
		pluck="name"))


def _params(from_date, to_date, company, user=None, search=None, exclude_services=True, pos_profile=None,
		branch=None, unpaid_of=None) -> dict:
	"""``unpaid_of``: only that salesman's invoices whose commission is not paid yet, from any
	time up to ``to_date`` (the Sales Commission Payment; read through the commission index)."""
	from delivery_management.stats.branches import profiles_of

	from_date, to_date = getdate(from_date), getdate(to_date)
	return {
		"pos_profile": pos_profile or None,
		"branch": branch or None,
		# A branch's sales are those made through its POS Profiles.
		"profiles": tuple(profiles_of([branch]) or [""]) if branch else None,
		"exclude": 1 if exclude_services else 0,
		"services": service_groups() or ("",),
		"company": company,
		"user": user,
		# Whole days: from 00:00 of From Date up to (not including) 00:00 after To Date.
		"start": f"{from_date} 00:00:00",
		"end": f"{add_days(to_date, 1)} 00:00:00",
		"q": f"%{search.strip()}%" if search and search.strip() else None,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"unpaid_of": unpaid_of or None,
	}


def _invoice_query(params: dict) -> str:
	"""One row per counted invoice with its units, summed from the units frozen on its lines.
	``services`` is its services' share of the Total; with ``exclude`` it is taken off the sales."""
	search = """and (si.name like %(q)s or si.customer like %(q)s or si.customer_name like %(q)s
		or si.return_against like %(q)s)""" if params["q"] else ""
	# The payment reads its rows through the commission index and skips returns it must not subtract.
	unpaid = f"""and si.dm_salesman = %(unpaid_of)s and si.dm_commission_payment is null
		and not {RETURN_OF_PAID}""" if params.get("unpaid_of") else ""
	return f"""select si.name, {_salesman()} as user, si.creation, date_format(si.creation, '%%Y-%%m-01') as month,
			si.posting_date, si.customer, si.customer_name, not {RETURN_OF_PAID} as commissionable,
			si.is_return, si.return_against, ifnull(si.dm_order_type, '') as order_type, si.status,
			{TOTAL_COLUMN} - %(exclude)s * {SERVICE_TOTAL} as total,
			si.base_net_total - %(exclude)s * {SERVICE_NET} as net_total, {SERVICE_TOTAL} as services,
			coalesce(sum(sii.dm_ac_units), 0) as ac_units, coalesce(sum(sii.dm_non_ac_units), 0) as non_ac_units,
			coalesce(sum(sii.dm_small_units), 0) as small_units
		from `tabSales Invoice` si {"force index (dm_commission_index)" if params.get("unpaid_of") else ""}
		left join `tabSales Invoice Item` sii on sii.parent = si.name and sii.parenttype = 'Sales Invoice'
		where si.company = %(company)s and si.docstatus = 1
			{"" if params.get("unpaid_of") else "and si.creation >= %(start)s"} and si.creation < %(end)s
			{unpaid}
			{f"and {_salesman()} = %(user)s" if params["user"] else ""}
			{"and si.pos_profile = %(pos_profile)s" if params["pos_profile"] else ""}
			{"and si.pos_profile in %(profiles)s" if params["profiles"] else ""}
			{search}
			and {PAID_CONDITION}
		group by si.name"""


# ---------------------------------------------------------------- commission


def commission_percent(sold, target, low, high) -> float:
	"""Min + (sold ÷ target) × (Max − Min), never above Max; Max when there is no target."""
	if cint(target) <= 0:
		return flt(high)
	reached = min(max(flt(sold), 0) / cint(target), 1)
	return flt(low) + reached * (flt(high) - flt(low))


def commission(amount, percent, score) -> dict:
	"""75% of ``amount × percent`` always, 25% of it times the evaluation ``score`` (0–100).
	Returns more than they sold (a negative amount) earn nothing, not a negative commission."""
	base = max(flt(amount), 0) * flt(percent) / 100
	items = base * ITEMS_SHARE
	evaluation = base * (1 - ITEMS_SHARE) * flt(score) / 100
	return {"items": _money(items), "evaluation": _money(evaluation), "amount": _money(items + evaluation)}


# ---------------------------------------------------------------- rules and months

RULE = "Sales Commission Rule"


def month_start(day) -> datetime.date:
	return getdate(day).replace(day=1)


def month_end(day) -> datetime.date:
	return getdate(get_last_day(day))


def month_parts(from_date, to_date) -> list[tuple]:
	"""``(month, part_from, part_to)`` for each month the period touches."""
	start, end = getdate(from_date), getdate(to_date)
	parts, month = [], month_start(start)
	while month <= end:
		parts.append((month, max(start, month), min(end, month_end(month))))
		month = getdate(add_months(month, 1))
	return parts


def rules_of(users: list[str]) -> dict[str, list]:
	"""Each salesman's Sales Commission Rules, oldest month first."""
	rows = frappe.get_all(RULE, filters={"parenttype": "User", "parentfield": "dm_commission_rules",
		"parent": ["in", users or [""]]}, fields=["parent", "month", "sales_target", "small_items_target",
		"min_percent", "max_percent"], order_by="month asc")
	out = {}
	for r in rows:
		out.setdefault(r.parent, []).append(r)
	return out


def rule_for(rules: list, month) -> dict | None:
	"""The month's rule, else the closest one (however old); on a tie the older one."""
	if not rules:
		return None
	month = month_start(month)
	best = min(rules, key=lambda r: (abs((getdate(r.month) - month).days), getdate(r.month) > month))
	return {"month": str(best.month), "exact": getdate(best.month) == month, "sales_target": _money(best.sales_target),
		"small_items_target": cint(best.small_items_target), "min_percent": flt(best.min_percent),
		"max_percent": flt(best.max_percent)}


def evaluations_of(users: list[str]) -> dict[str, list]:
	out = {}
	for r in frappe.get_all(EVALUATION, filters={"salesman": ["in", users or [""]], "docstatus": ["<", 2]},
			fields=["name", "salesman", "score", "from_date", "to_date"], order_by="to_date desc, modified desc"):
		out.setdefault(r.salesman, []).append(r)
	return out


def evaluation_for(evaluations: list, from_date, to_date) -> dict | None:
	"""The latest Sales Evaluation overlapping the period, else the closest one to it."""
	start, end = getdate(from_date), getdate(to_date)

	def gap(r):
		if getdate(r.to_date) < start:
			return (start - getdate(r.to_date)).days
		if getdate(r.from_date) > end:
			return (getdate(r.from_date) - end).days
		return 0

	if not evaluations:
		return None
	best = min(evaluations, key=gap)  # the list is latest first, so the latest wins a tie
	return {"name": best.name, "score": flt(best.score), "from_date": str(best.from_date), "to_date": str(best.to_date),
		"overlaps": gap(best) == 0}


def month_commission(sales: dict, small_units, rule: dict | None, evaluation: dict | None) -> dict:
	"""One month: the % from its small items against its rule, then the commission on ``sales``
	(``{basis: amount}``). No rule: nothing is earned."""
	percent = commission_percent(small_units, rule["small_items_target"], rule["min_percent"], rule["max_percent"]) \
		if rule else 0
	score = evaluation["score"] if evaluation else 0
	return {"percent": flt(percent, 4), "score": score,
		"commission": {basis: commission(sales.get(basis), percent, score) for basis in BASES}}


def _targeted_users(user: str | None) -> list[str]:
	"""Salesmen with a commission rule show up even before their first sale of the period."""
	having = set(frappe.get_all(RULE, filters={"parenttype": "User", "parentfield": "dm_commission_rules"},
		pluck="parent", distinct=True))
	filters = {"enabled": 1, "user_type": "System User", "name": ["in", list(having) or [""]]}
	if user:
		filters["name"] = user if user in having else ""
	return frappe.get_all("User", filters=filters, pluck="name")


def sold_by_month(params: dict) -> dict:
	"""``{(user, month): totals}`` from the per-invoice query (the same rows as the invoice list)."""
	rows = frappe.db.sql(
		f"""select inv.user, inv.month, count(*) - sum(inv.is_return) as invoices,
			sum(inv.is_return = 0 and inv.order_type = 'Order') as orders, sum(inv.is_return) as returns,
			sum(inv.total) as total, sum(inv.net_total) as net_total, sum(inv.services) as services, sum(inv.ac_units) as ac_units,
			sum(inv.non_ac_units) as non_ac_units, sum(inv.small_units) as small_units,
			sum(inv.total * inv.commissionable) as commission_total,
			sum(inv.net_total * inv.commissionable) as commission_net_total,
			sum(inv.small_units * inv.commissionable) as commission_small_units
		from ({_invoice_query(params)}) inv
		group by inv.user, inv.month""",
		params,
		as_dict=True,
	)
	return {(r.user, str(r.month)): r for r in rows}


# ---------------------------------------------------------------- results


def compute(from_date, to_date, company: str, user: str | None = None, exclude_services: bool = True,
		pos_profile: str | None = None, branch: str | None = None, branch_cards: list[str] | None = None) -> dict:
	"""Per-salesman totals, targets and commission for the period (one row per salesman), and
	the whole-branch totals of ``branch_cards`` (every salesman's sales in that branch)."""
	params = _params(from_date, to_date, company, user, exclude_services=exclude_services, pos_profile=pos_profile,
		branch=branch)
	sold = sold_by_month(params)
	users = sorted({u for u, _m in sold})
	# With a POS Profile or a branch, only who sold through it.
	for name in [] if params["pos_profile"] or params["branch"] else _targeted_users(user):
		if name not in users:
			users.append(name)

	people = {u.name: u for u in frappe.get_all("User", filters={"name": ["in", users or [""]]},
		fields=["name", "full_name"])}
	rules = rules_of(users)
	evaluations = evaluations_of(users)
	parts = month_parts(params["from_date"], params["to_date"])
	zero = frappe._dict({k: 0 for k in SUM_KEYS})

	rows = []
	for name in users:
		person = people.get(name) or frappe._dict()
		row = {"user": name, "full_name": person.full_name or name, "months": []}
		row.update({k: 0 for k in SUM_KEYS + ("sales_target", "small_items_target")})
		row["commission"] = {b: {"items": 0, "evaluation": 0, "amount": 0} for b in BASES}
		for month, part_from, part_to in parts:
			r = sold.get((name, str(month))) or zero
			rule = rule_for(rules.get(name), month)
			evaluation = evaluation_for(evaluations.get(name), part_from, part_to)
			# Returns of sales whose commission was paid count in the sales, not in the commission.
			result = month_commission({b: r.get(f"commission_{b}", r[b]) for b in BASES},
				r.get("commission_small_units", r.small_units), rule, evaluation)
			for k in SUM_KEYS:
				row[k] += flt(r[k])
			row["sales_target"] += rule["sales_target"] if rule else 0
			row["small_items_target"] += rule["small_items_target"] if rule else 0
			for b in BASES:
				for part in ("items", "evaluation", "amount"):
					row["commission"][b][part] = _money(row["commission"][b][part] + result["commission"][b][part])
			row["months"].append({"month": str(month), "from_date": str(part_from), "to_date": str(part_to),
				**{k: _money(r[k]) for k in MONEY_KEYS}, "small_units": _clean_qty(r.small_units),
				"rule": rule, "evaluation": evaluation, **result})
		row.update({k: cint(row[k]) for k in COUNT_KEYS})
		row.update({k: _money(row[k]) for k in MONEY_KEYS + ("sales_target",)})
		row.update({k: _clean_qty(row[k]) for k in UNIT_KEYS})
		# One month: its %. Several: the average % weighted by each month's sales (for display).
		sales = sum(max(m["total"], 0) for m in row["months"])
		row["commission_percent"] = row["months"][0]["percent"] if len(row["months"]) == 1 else \
			flt(sum(m["percent"] * max(m["total"], 0) for m in row["months"]) / sales, 4) if sales else 0
		row["evaluation"] = evaluation_for(evaluations.get(name), params["from_date"], params["to_date"])
		rows.append(row)
	rows.sort(key=lambda r: (-r["total"], r["full_name"]))

	totals = {k: sum(r[k] for r in rows) for k in SUM_KEYS + ("sales_target", "small_items_target")}
	totals.update({k: _money(totals[k]) for k in MONEY_KEYS + ("sales_target",)})
	totals.update({k: _clean_qty(totals[k]) for k in UNIT_KEYS})
	totals["commission"] = {basis: {part: _money(sum(r["commission"][basis][part] for r in rows))
		for part in ("items", "evaluation", "amount")} for basis in BASES}
	return {
		"rows": rows,
		"totals": totals,
		"items_share": ITEMS_SHARE,
		"currency": frappe.get_cached_value("Company", company, "default_currency"),
		"branches": [branch_summary(params, name) for name in branch_cards or []],
		"filters": {"from_date": params["from_date"], "to_date": params["to_date"], "company": company, "user": user,
			"exclude_services": params["exclude"], "pos_profile": params["pos_profile"], "branch": params["branch"]},
		"generated_at": str(now_datetime()),
	}


def branch_summary(params: dict, branch: str) -> dict:
	"""Everyone's sales in the branch for the period, against the branch's Sales Target."""
	from delivery_management.stats.branches import profiles_of

	p = {**params, "user": None, "q": None, "pos_profile": None, "branch": branch,
		"profiles": tuple(profiles_of([branch]) or [""])}
	r = frappe.db.sql(f"""select count(*) - coalesce(sum(inv.is_return), 0) as invoices, sum(inv.total) as total,
		sum(inv.net_total) as net_total, sum(inv.services) as services from ({_invoice_query(p)}) inv""", p, as_dict=True)[0]
	return {"branch": branch, "target": _money(frappe.db.get_value("Branch", branch, "dm_sales_target")),
		"invoices": cint(r.invoices), **{k: _money(r[k]) for k in MONEY_KEYS}}


def invoices(from_date, to_date, company: str, user: str | None = None, search: str | None = None,
		page: int = 0, page_len: int | None = INVOICES_PER_PAGE, exclude_services: bool = True,
		pos_profile: str | None = None, branch: str | None = None) -> dict:
	"""The counted invoices behind ``compute``, newest first; ``page_len=None`` returns them all."""
	params = _params(from_date, to_date, company, user, search, exclude_services, pos_profile, branch)
	query = _invoice_query(params)
	count = cint(frappe.db.sql(f"select count(*) from ({query}) inv", params)[0][0])
	# The export (no page) is capped too, so one click cannot read a whole year of lines.
	limit = f"limit {cint(page_len)} offset {cint(page) * cint(page_len)}" if page_len else "limit 20000"
	rows = frappe.db.sql(f"{query} order by si.creation desc {limit}", params, as_dict=True)
	return {"count": count, "page": cint(page), "page_len": page_len, "invoices": [{
		"name": inv.name,
		"user": inv.user,
		"created": str(inv.creation)[:19],
		"posting_date": str(inv.posting_date),
		"month": str(inv.month),
		"customer": inv.customer,
		"customer_name": inv.customer_name or inv.customer,
		"type": "Return" if cint(inv.is_return) else (inv.order_type or "Invoice"),
		"return_against": inv.return_against,
		"status": inv.status,
		"total": flt(inv.total),
		"net_total": flt(inv.net_total),
		"services": flt(inv.services),
		**{k: _clean_qty(inv[k]) for k in UNIT_KEYS},
	} for inv in rows]}


def _money(value) -> float:
	return flt(value, 3)


def _clean_qty(value):
	value = round(flt(value), 3)
	return int(value) if value == int(value) else value


# ---------------------------------------------------------------- commission payment


def unpaid_commission(salesman: str, company: str, to_month, exclude_services: bool = True, basis: str = "total",
		from_month=None, names: list[str] | None = None) -> dict:
	"""What a Sales Commission Payment pays: every invoice / return of the salesman whose
	commission is not paid yet, created up to the end of ``to_month`` (late ones from older
	months too), each at **its own month's** % (from the whole month, ``month_commission``).

	Cost: the unpaid invoices come through the index (dm_salesman, dm_commission_payment,
	creation), so already paid ones are never read; then one grouped query per month that
	has unpaid invoices, for that month's whole sales."""
	if basis not in BASES:
		frappe.throw(frappe._("Unknown commission basis {0}.").format(basis))
	end = month_end(to_month)
	params = _params(end, end, company, exclude_services=exclude_services, unpaid_of=salesman)
	lines = frappe.db.sql(f"{_invoice_query(params)} order by si.creation", params, as_dict=True)
	if names is not None:
		lines = [r for r in lines if r.name in set(names)]

	rules = rules_of([salesman]).get(salesman)
	evaluations = evaluations_of([salesman]).get(salesman)
	first = month_start(from_month) if from_month else None
	months, invoices = [], []
	for month in sorted({str(r.month) for r in lines}):
		start, stop = getdate(month), month_end(month)
		whole = sold_by_month(_params(start, stop, company, user=salesman, exclude_services=exclude_services))
		totals = whole.get((salesman, month)) or frappe._dict({k: 0 for k in SUM_KEYS})
		rule = rule_for(rules, month)
		evaluation = evaluation_for(evaluations, start, stop)
		mine = [r for r in lines if str(r.month) == month]
		unpaid_sales = _money(sum(flt(r[basis]) for r in mine))
		result = month_commission({basis: unpaid_sales}, totals.get("commission_small_units", 0), rule, evaluation)
		factor = flt(result["percent"]) / 100 * (ITEMS_SHARE + (1 - ITEMS_SHARE) * flt(result["score"]) / 100)
		months.append({
			"month": month, "late": bool(first and start < first),
			"month_sales": _money(totals.get(f"commission_{basis}", 0)),
			"month_small_units": _clean_qty(totals.get("commission_small_units", 0)),
			"unpaid_sales": unpaid_sales, "invoices": len(mine),
			"rule_month": rule["month"] if rule else None,
			"sales_target": rule["sales_target"] if rule else 0,
			"small_items_target": rule["small_items_target"] if rule else 0,
			"min_percent": rule["min_percent"] if rule else 0, "max_percent": rule["max_percent"] if rule else 0,
			"percent": result["percent"], "evaluation": evaluation["name"] if evaluation else None,
			"score": result["score"], "commission": result["commission"][basis]["amount"],
		})
		for r in mine:
			invoices.append({
				"sales_invoice": r.name, "month": month, "late": bool(first and start < first),
				"created": str(r.creation)[:19], "customer_name": r.customer_name or r.customer,
				"is_return": cint(r.is_return), "return_against": r.return_against,
				"amount": _money(r[basis]), "small_units": _clean_qty(r.small_units),
				"percent": result["percent"], "commission": _money(flt(r[basis]) * factor),
			})
	return {"months": months, "invoices": invoices, "total": _money(sum(m["commission"] for m in months))}
