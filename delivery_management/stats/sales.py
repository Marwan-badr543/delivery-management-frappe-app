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
* **Units** are stock-UOM quantities: **AC** = items that use AC installation
  capacity (the AC Item Groups minus the exclusions, same rule as the slot
  engine); **Non-AC** = every other stock item. Services/charges are not units.

The per-salesman numbers are summed from the same per-invoice list that the
page shows, so the table and the invoice list always agree. Everything is read
with SQL (no documents are loaded); the page runs it as a background job.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import add_days, flt, getdate, now_datetime

from delivery_management.slots.cart import KIND_AC, classify_items

# A paid sale, or a credit note against a paid sale.
PAID_CONDITION = """(
	(si.is_return = 0 and si.outstanding_amount <= 0)
	or (si.is_return = 1 and exists (select 1 from `tabSales Invoice` orig where orig.name = si.return_against
		and orig.docstatus = 1 and orig.is_return = 0 and orig.outstanding_amount <= 0)))"""

TOTAL_COLUMN = "if(si.disable_rounded_total = 1 or si.base_rounded_total = 0, si.base_grand_total, si.base_rounded_total)"

SUM_KEYS = ("invoices", "orders", "returns", "total", "net_total", "ac_units", "non_ac_units")


def _salesman() -> str:
	if frappe.db.has_column("Sales Invoice", "posa_cashier"):
		return "coalesce(nullif(si.posa_cashier, ''), si.owner)"
	return "si.owner"


def _where(user: str | None) -> str:
	return f"""si.docstatus = 1 and si.company = %(company)s
		and si.creation >= %(start)s and si.creation < %(end)s
		{f"and {_salesman()} = %(user)s" if user else ""}
		and {PAID_CONDITION}"""


def compute(from_date, to_date, company: str, user: str | None = None) -> dict:
	from_date, to_date = getdate(from_date), getdate(to_date)
	params = {
		"company": company,
		"user": user,
		# Whole days: from 00:00 of From Date up to (not including) 00:00 after To Date.
		"start": f"{from_date} 00:00:00",
		"end": f"{add_days(to_date, 1)} 00:00:00",
	}
	where = _where(user)

	invoices = frappe.db.sql(
		f"""select si.name, {_salesman()} as user, si.creation, si.posting_date, si.customer, si.customer_name,
			si.is_return, si.return_against, ifnull(si.dm_order_type, '') as order_type, si.status,
			{TOTAL_COLUMN} as total, si.base_net_total as net_total
		from `tabSales Invoice` si
		where {where}
		order by si.creation desc""",
		params,
		as_dict=True,
	)
	lines = frappe.db.sql(
		f"""select sii.parent, sii.item_code, sum(sii.stock_qty) as qty
		from `tabSales Invoice` si join `tabSales Invoice Item` sii on sii.parent = si.name
		where {where} and ifnull(sii.item_code, '') != ''
		group by sii.parent, sii.item_code""",
		params,
		as_dict=True,
	)

	codes = {line.item_code for line in lines}
	kinds = classify_items(codes)
	stock_items = set(
		frappe.get_all("Item", filters={"name": ["in", list(codes) or [""]], "is_stock_item": 1}, pluck="name")
	)
	units = defaultdict(lambda: {"ac_units": 0.0, "non_ac_units": 0.0})
	for line in lines:
		if kinds.get(line.item_code) == KIND_AC:
			units[line.parent]["ac_units"] += flt(line.qty)
		elif line.item_code in stock_items:
			units[line.parent]["non_ac_units"] += flt(line.qty)

	invoice_rows = []
	for inv in invoices:
		is_return = int(inv.is_return or 0)
		invoice_rows.append({
			"name": inv.name,
			"user": inv.user,
			"created": str(inv.creation)[:19],
			"posting_date": str(inv.posting_date),
			"customer": inv.customer,
			"customer_name": inv.customer_name or inv.customer,
			"type": "Return" if is_return else (inv.order_type or "Invoice"),
			"return_against": inv.return_against,
			"status": inv.status,
			"total": flt(inv.total),
			"net_total": flt(inv.net_total),
			"ac_units": _clean_qty(units[inv.name]["ac_units"]),
			"non_ac_units": _clean_qty(units[inv.name]["non_ac_units"]),
		})

	by_user: dict[str, dict] = {}
	for inv in invoice_rows:
		row = by_user.setdefault(inv["user"], {"user": inv["user"], **{k: 0 for k in SUM_KEYS}})
		if inv["type"] == "Return":
			row["returns"] += 1
		else:
			row["invoices"] += 1
			if inv["type"] == "Order":
				row["orders"] += 1
		for key in ("total", "net_total", "ac_units", "non_ac_units"):
			row[key] += inv[key]

	names = dict(
		frappe.get_all("User", filters={"name": ["in", list(by_user) or [""]]}, fields=["name", "full_name"], as_list=True)
	)
	rows = []
	for row in by_user.values():
		row["full_name"] = names.get(row["user"]) or row["user"]
		row["total"], row["net_total"] = _money(row["total"]), _money(row["net_total"])
		row["ac_units"], row["non_ac_units"] = _clean_qty(row["ac_units"]), _clean_qty(row["non_ac_units"])
		rows.append(row)
	rows.sort(key=lambda r: (-r["total"], r["full_name"]))

	totals = {key: sum(r[key] for r in rows) for key in SUM_KEYS}
	totals["total"], totals["net_total"] = _money(totals["total"]), _money(totals["net_total"])
	totals["ac_units"], totals["non_ac_units"] = _clean_qty(totals["ac_units"]), _clean_qty(totals["non_ac_units"])
	return {
		"rows": rows,
		"totals": totals,
		"invoices": invoice_rows,
		"currency": frappe.get_cached_value("Company", company, "default_currency"),
		"filters": {"from_date": str(from_date), "to_date": str(to_date), "company": company, "user": user},
		"generated_at": str(now_datetime()),
	}


def _money(value) -> float:
	return flt(value, 3)


def _clean_qty(value):
	value = round(flt(value), 3)
	return int(value) if value == int(value) else value
