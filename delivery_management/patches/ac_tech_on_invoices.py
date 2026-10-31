"""AC orders assigned before the invoice had its AC Tech fields: put the AC Tech on them."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	from delivery_management.drivers.assignment import set_invoice_ac_tech
	from delivery_management.install import get_custom_fields

	create_custom_fields({"Sales Invoice": get_custom_fields()["Sales Invoice"]}, ignore_validate=True, update=True)
	for name, tech in frappe.get_all("Delivery Note", filters={"docstatus": ["<", 2], "is_return": 0,
			"dm_delivery_type": "AC", "dm_driver": ["is", "set"]}, fields=["name", "dm_driver"], as_list=True,
			order_by="creation"):
		set_invoice_ac_tech(name, tech)
