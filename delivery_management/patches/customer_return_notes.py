"""Goods that only came back (a Sales Return on the Delivery Note) are delivered again; goods
the customer gave back through a Customer Return are not (returns.service). Mark the
Return Delivery Notes the Customer Returns made, count the order lines' Returned Qty again
from them, and bring the returned notes up to date (AC Units, Delivery Status Returned)."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	from delivery_management.install import get_custom_fields
	from delivery_management.returns.service import returned_for_good, update_returned_note

	create_custom_fields({"Delivery Note": get_custom_fields()["Delivery Note"]}, ignore_validate=True, update=True)
	notes = {n for text in frappe.get_all("Customer Return", filters={"return_delivery_notes": ["is", "set"]},
		pluck="return_delivery_notes") for n in text.split("\n") if n}
	if notes:
		frappe.db.sql("update `tabDelivery Note` set dm_customer_return = 1 where name in %(notes)s",
			{"notes": tuple(notes)})
	for so_detail in frappe.db.sql(
		"""select distinct dni.so_detail from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
		where dn.docstatus = 1 and dn.is_return = 1 and ifnull(dni.so_detail, '') != ''""",
		pluck=True,
	):
		frappe.db.set_value("Sales Order Item", so_detail, "returned_qty", returned_for_good(so_detail),
			update_modified=False)
	for note in frappe.get_all("Delivery Note", filters={"docstatus": 1, "is_return": 1,
			"return_against": ["is", "set"]}, pluck="return_against", distinct=True):
		update_returned_note(note)
