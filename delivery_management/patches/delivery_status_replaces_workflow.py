"""The Delivery Note workflow overrode ERPNext's status. Its On the way / Delivered states
move to the Delivery Status field and the workflow is switched off."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

WORKFLOW = "delivery note on the way"


def execute():
	from delivery_management.install import get_custom_fields

	create_custom_fields({"Delivery Note": get_custom_fields()["Delivery Note"]}, ignore_validate=True, update=True)
	if frappe.db.has_column("Delivery Note", "workflow_state"):
		frappe.db.sql("""update `tabDelivery Note` set dm_delivery_status = workflow_state
			where docstatus = 1 and is_return = 0 and workflow_state in ('On the way', 'Delivered')""")
	if frappe.db.exists("Workflow", WORKFLOW):
		frappe.db.set_value("Workflow", WORKFLOW, "is_active", 0)
		frappe.clear_cache(doctype="Delivery Note")
