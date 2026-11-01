"""Closed Sales Orders kept their Stock Reservation Entries (ERPNext does not release them on
close), so stock no one will deliver stayed reserved. Free what they still hold."""

import frappe


def execute():
	from delivery_management.orders.reservation import release_undelivered

	for name in frappe.db.sql(
		"""select distinct sre.voucher_no from `tabStock Reservation Entry` sre
		join `tabSales Order` so on so.name = sre.voucher_no
		where sre.voucher_type = 'Sales Order' and sre.docstatus = 1 and so.status = 'Closed'
		and sre.reserved_qty > sre.delivered_qty""",
		pluck=True,
	):
		release_undelivered(name)
