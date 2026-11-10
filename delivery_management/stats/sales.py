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
* **Units** (AC, non-AC, small items) are frozen on each invoice line when it is
  saved, with Product Bundles counted by their contents: see ``stats/units.py``.
* **Commission** (see ``commission``): the small items sold against the salesman's
  Small Items Target set the commission % between the Min and Max of Delivery
  Settings; 75% of it is paid on the sales, the other 25% times the score of the
  salesman's latest Sales Evaluation that overlaps the period. Targets are monthly
  and compared as they are, whatever the period.

The salesman totals are grouped in SQL from the same per-invoice query that
feeds the invoice list, so the table and the list always agree. The cost is one
pass over the period's invoices and their lines (index on company, docstatus,
creation), then a few small lookups per salesman (targets, evaluations). The
summary is one row per salesman; the invoice list is read a page at a time.
Everything is read with SQL (no documents are loaded).
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, cint, flt, getdate, now_datetime


# A paid sale, or a credit note against a paid sale.
PAID_CONDITION = """(
	(si.is_return = 0 and si.outstanding_amount <= 0)
	or (si.is_return = 1 and exists (select 1 from `tabSales Invoice` orig where orig.name = si.return_against
		and orig.docstatus = 1 and orig.is_return = 0 and orig.outstanding_amount <= 0)))"""

TOTAL_COLUMN = "if(si.disable_rounded_total = 1 or si.base_rounded_total = 0, si.base_grand_total, si.base_rounded_total)"

COUNT_KEYS = ("invoices", "orders", "returns")
UNIT_KEYS = ("ac_units", "non_ac_units", "small_units")
MONEY_KEYS = ("total", "net_total")
SUM_KEYS = COUNT_KEYS + MONEY_KEYS + UNIT_KEYS
BASES = MONEY_KEYS  # the commission is on the Total (with VAT) or the Net Total; the page chooses
INVOICES_PER_PAGE = 100
ITEMS_SHARE = 0.75  # paid on the small-items commission %; the rest is paid on the evaluation score
EVALUATION = "Sales Evaluation"


def _salesman() -> str:
	if frappe.db.has_column("Sales Invoice", "posa_cashier"):
		return "coalesce(nullif(si.posa_cashier, ''), si.owner)"
	return "si.owner"


def _params(from_date, to_date, company, user=None, search=None) -> dict:
	from_date, to_date = getdate(from_date), getdate(to_date)
	return {
		"company": company,
		"user": user,
		# Whole days: from 00:00 of From Date up to (not including) 00:00 after To Date.
		"start": f"{from_date} 00:00:00",
		"end": f"{add_days(to_date, 1)} 00:00:00",
		"q": f"%{search.strip()}%" if search and search.strip() else None,
		"from_date": str(from_date),
		"to_date": str(to_date),
	}


def _invoice_query(params: dict) -> str:
	"""One row per counted invoice with its units, summed from the units frozen on its lines."""
	search = """and (si.name like %(q)s or si.customer like %(q)s or si.customer_name like %(q)s
		or si.return_against like %(q)s)""" if params["q"] else ""
	return f"""select si.name, {_salesman()} as user, si.creation, si.posting_date, si.customer, si.customer_name,
			si.is_return, si.return_against, ifnull(si.dm_order_type, '') as order_type, si.status,
			{TOTAL_COLUMN} as total, si.base_net_total as net_total,
			coalesce(sum(sii.dm_ac_units), 0) as ac_units, coalesce(sum(sii.dm_non_ac_units), 0) as non_ac_units,
			coalesce(sum(sii.dm_small_units), 0) as small_units
		from `tabSales Invoice` si
		left join `tabSales Invoice Item` sii on sii.parent = si.name and sii.parenttype = 'Sales Invoice'
		where si.company = %(company)s and si.docstatus = 1
			and si.creation >= %(start)s and si.creation < %(end)s
			{f"and {_salesman()} = %(user)s" if params["user"] else ""}
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


def _latest_evaluations(users: list[str], from_date: str, to_date: str) -> dict:
	"""Each salesman's latest Sales Evaluation whose dates overlap the period."""
	rows = frappe.get_all(EVALUATION,
		filters={"salesman": ["in", users or [""]], "from_date": ["<=", to_date], "to_date": [">=", from_date]},
		fields=["name", "salesman", "score", "from_date", "to_date"],
		order_by="to_date desc, modified desc")
	latest = {}
	for r in rows:
		latest.setdefault(r.salesman, {"name": r.name, "score": flt(r.score), "from_date": str(r.from_date),
			"to_date": str(r.to_date)})
	return latest


def _targeted_users(user: str | None) -> list[str]:
	"""Salesmen with a target show up even before their first sale of the period."""
	filters = {"enabled": 1, "user_type": "System User"}
	if user:
		filters["name"] = user
	return frappe.get_all("User", filters=filters,
		or_filters={"dm_sales_target": [">", 0], "dm_small_items_target": [">", 0]}, pluck="name")


# ---------------------------------------------------------------- results


def compute(from_date, to_date, company: str, user: str | None = None) -> dict:
	"""Per-salesman totals, targets and commission for the period (one row per salesman)."""
	params = _params(from_date, to_date, company, user)
	sold = frappe.db.sql(
		f"""select inv.user, count(*) - sum(inv.is_return) as invoices,
			sum(inv.is_return = 0 and inv.order_type = 'Order') as orders, sum(inv.is_return) as returns,
			sum(inv.total) as total, sum(inv.net_total) as net_total, sum(inv.ac_units) as ac_units,
			sum(inv.non_ac_units) as non_ac_units, sum(inv.small_units) as small_units
		from ({_invoice_query(params)}) inv
		group by inv.user""",
		params,
		as_dict=True,
	)
	by_user = {r.user: r for r in sold}
	for name in _targeted_users(user):
		by_user.setdefault(name, frappe._dict(user=name, **{k: 0 for k in SUM_KEYS}))

	users = list(by_user)
	people = {u.name: u for u in frappe.get_all("User", filters={"name": ["in", users or [""]]},
		fields=["name", "full_name", "dm_sales_target", "dm_small_items_target"])}
	evaluations = _latest_evaluations(users, params["from_date"], params["to_date"])
	low = flt(frappe.db.get_single_value("Delivery Settings", "commission_min_percent"))
	high = flt(frappe.db.get_single_value("Delivery Settings", "commission_max_percent"))

	rows = []
	for name, r in by_user.items():
		person = people.get(name) or frappe._dict()
		row = {"user": name, "full_name": person.full_name or name}
		row.update({k: cint(r[k]) for k in COUNT_KEYS})
		row.update({k: _money(r[k]) for k in MONEY_KEYS})
		row.update({k: _clean_qty(r[k]) for k in UNIT_KEYS})
		row["sales_target"] = _money(person.dm_sales_target)
		row["small_items_target"] = cint(person.dm_small_items_target)
		row["commission_percent"] = flt(commission_percent(row["small_units"], row["small_items_target"], low, high), 4)
		row["evaluation"] = evaluations.get(name)
		score = row["evaluation"]["score"] if row["evaluation"] else 0
		row["commission"] = {basis: commission(row[basis], row["commission_percent"], score) for basis in BASES}
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
		"commission_range": {"min": low, "max": high, "items_share": ITEMS_SHARE},
		"currency": frappe.get_cached_value("Company", company, "default_currency"),
		"filters": {"from_date": params["from_date"], "to_date": params["to_date"], "company": company, "user": user},
		"generated_at": str(now_datetime()),
	}


def invoices(from_date, to_date, company: str, user: str | None = None, search: str | None = None,
		page: int = 0, page_len: int | None = INVOICES_PER_PAGE) -> dict:
	"""The counted invoices behind ``compute``, newest first; ``page_len=None`` returns them all."""
	params = _params(from_date, to_date, company, user, search)
	query = _invoice_query(params)
	count = cint(frappe.db.sql(f"select count(*) from ({query}) inv", params)[0][0])
	limit = f"limit {cint(page_len)} offset {cint(page) * cint(page_len)}" if page_len else ""
	rows = frappe.db.sql(f"{query} order by si.creation desc {limit}", params, as_dict=True)
	return {"count": count, "page": cint(page), "page_len": page_len, "invoices": [{
		"name": inv.name,
		"user": inv.user,
		"created": str(inv.creation)[:19],
		"posting_date": str(inv.posting_date),
		"customer": inv.customer,
		"customer_name": inv.customer_name or inv.customer,
		"type": "Return" if cint(inv.is_return) else (inv.order_type or "Invoice"),
		"return_against": inv.return_against,
		"status": inv.status,
		"total": flt(inv.total),
		"net_total": flt(inv.net_total),
		**{k: _clean_qty(inv[k]) for k in UNIT_KEYS},
	} for inv in rows]}


def _money(value) -> float:
	return flt(value, 3)


def _clean_qty(value):
	value = round(flt(value), 3)
	return int(value) if value == int(value) else value
