"""Sales Dashboard: port of the old admin "Dashboard Summary" page.

* **Summary** (all time): orders = Sales Invoices not cancelled; active products =
  enabled sales Items; customers = customers with a submitted Sales Invoice; pending
  orders = draft Delivery Notes (goods not delivered yet).
* **Status** (period): every Sales Invoice by its status (drafts and cancelled included).
* **Area** (period) and **top products**: submitted sales only (no cancelled invoices,
  returns or invoices with a credit note), counted per POS Profile (branch); **Online** =
  territory "All Territories", as in the old system.
* The period is the Posting Date. Top products: 20 items by stock quantity (the
  delivery charge is not a product).
"""

from __future__ import annotations

import frappe

from delivery_management.slots.settings import get_settings

ONLINE = "online"
ONLINE_TERRITORY = "All Territories"
TOP = 20


def summary() -> dict:
	return {
		"orders": frappe.db.count("Sales Invoice", {"docstatus": ["<", 2]}),
		"products": frappe.db.count("Item", {"disabled": 0, "is_sales_item": 1}),
		"customers": frappe.db.sql("select count(distinct customer) from `tabSales Invoice` where docstatus = 1")[0][0],
		"pending_orders": frappe.db.count("Delivery Note", {"docstatus": 0, "is_return": 0}),
	}


def status_stats(start, end) -> list[dict]:
	return frappe.db.sql("""select status, count(*) as count from `tabSales Invoice`
		where posting_date between %s and %s group by status order by count desc""", (start, end), as_dict=True)


# Submitted sales of the period, without returns and returned (credited) invoices.
_SALES = """si.docstatus = 1 and si.is_return = 0 and si.status != 'Credit Note Issued'
	and si.posting_date between %(start)s and %(end)s"""


def area_stats(start, end) -> list[dict]:
	values = {"start": start, "end": end, "online": ONLINE_TERRITORY}
	rows = frappe.db.sql(f"""select si.pos_profile as area, count(*) as count from `tabSales Invoice` si
		where {_SALES} and ifnull(si.pos_profile, '') != '' group by si.pos_profile order by si.pos_profile""",
		values, as_dict=True)
	online = frappe.db.sql(f"""select count(*) from `tabSales Invoice` si
		where {_SALES} and si.territory = %(online)s""", values)[0][0]
	return [{"area": r.area, "label": r.area, "count": r.count} for r in rows] + \
		[{"area": ONLINE, "label": "Online", "count": online}]


def top_products(start, end, area: str | None = None) -> list[dict]:
	values = {"start": start, "end": end, "area": area, "online": ONLINE_TERRITORY, "top": TOP,
		"charge": get_settings().delivery_charge_item or ""}
	where = _SALES + " and item.item_code != %(charge)s"
	if area == ONLINE:
		where += " and si.territory = %(online)s"
	elif area:
		where += " and si.pos_profile = %(area)s"
	return frappe.db.sql(f"""select item.item_code, item.item_name, sum(item.stock_qty) as qty
		from `tabSales Invoice Item` item join `tabSales Invoice` si on si.name = item.parent
		where {where} group by item.item_code, item.item_name order by qty desc, item.item_code limit %(top)s""",
		values, as_dict=True)
