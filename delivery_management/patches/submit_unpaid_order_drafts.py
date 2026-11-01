"""Unpaid delivery Orders now get a submitted invoice the customer owes (orders.service).
Submit the draft invoices of unpaid Orders made before. An order closed meanwhile is opened
for the submit (ERPNext bills no closed order) and settled again after."""

import frappe


def execute():
	from delivery_management.returns.service import settle_order

	drafts = frappe.db.sql(
		"""select distinct si.name from `tabSales Invoice` si join `tabSales Invoice Item` i on i.parent = si.name
		where si.docstatus = 0 and si.is_return = 0 and ifnull(i.sales_order, '') != ''""",
		pluck=True,
	)
	for name in drafts:
		si = frappe.get_doc("Sales Invoice", name)
		orders = {r.sales_order for r in si.items if r.sales_order}
		closed = [frappe.get_doc("Sales Order", o) for o in orders
			if frappe.db.get_value("Sales Order", o, "status") == "Closed"]
		for so in closed:
			so.flags.ignore_permissions = True
			so.update_status("Draft")
		# Posted on the day the order was placed, not today.
		si.update({"is_pos": 0, "update_stock": 0, "posa_pos_opening_shift": None, "set_posting_time": 1})
		si.set("payments", [])
		si.flags.ignore_permissions = True
		si.submit()
		for order in orders:
			settle_order(order)
