"""Goods returned on a Sales Order are not reserved for it any more (Selling Settings), and
the store reservation (Bin reserved qty) of the items already returned is recounted."""

import frappe
from erpnext.stock.stock_balance import get_reserved_qty, update_bin_qty


def execute():
	frappe.db.set_single_value("Selling Settings", "dont_reserve_sales_order_qty_on_sales_return", 1)
	rows = frappe.db.sql(
		"""select distinct coalesce(pi.item_code, soi.item_code), coalesce(pi.warehouse, soi.warehouse)
		from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
		left join `tabPacked Item` pi on pi.parent_detail_docname = soi.name and pi.parenttype = 'Sales Order'
		where so.docstatus = 1 and soi.returned_qty > 0""",
	)
	for item_code, warehouse in rows:
		if item_code and warehouse and frappe.db.exists("Bin", {"item_code": item_code, "warehouse": warehouse}):
			update_bin_qty(item_code, warehouse, {"reserved_qty": get_reserved_qty(item_code, warehouse)})
