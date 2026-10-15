"""Give existing (not cancelled) Delivery Notes their schedule and Order Type, so the
driver pages see orders made before this feature. Values only; no document is re-saved."""

import frappe

from delivery_management.drivers.assignment import order_type_of
from delivery_management.drivers.delivery_note import SCHEDULE_FIELDS, sales_order_of


def execute():
	for name in frappe.get_all("Delivery Note", filters={"docstatus": ["<", 2],
			"dm_delivery_type": ["in", ["", None]]}, pluck="name"):
		dn = frappe.get_doc("Delivery Note", name)
		values = {"dm_delivery_type": order_type_of(dn.items)}
		so = sales_order_of(dn)
		if so:
			schedule = frappe.db.get_value("Sales Order", so, list(SCHEDULE_FIELDS), as_dict=True) or {}
			values.update({f: v for f, v in schedule.items() if v and not dn.get(f)})
		frappe.db.set_value("Delivery Note", name, values, update_modified=False)
