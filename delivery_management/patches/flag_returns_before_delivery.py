"""Returns made before this field existed: those whose note is still On the way were
returned before delivery (Installation Statistics takes them off)."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	from delivery_management.install import get_custom_fields

	create_custom_fields({"Delivery Note": get_custom_fields()["Delivery Note"]}, ignore_validate=True, update=True)
	frappe.db.sql(
		"""update `tabDelivery Note` ret join `tabDelivery Note` dn on dn.name = ret.return_against
		set ret.dm_returned_before_delivery = 1
		where ret.is_return = 1 and ret.docstatus = 1 and dn.dm_delivery_status = 'On the way'"""
	)
