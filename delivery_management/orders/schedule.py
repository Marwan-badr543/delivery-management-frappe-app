"""Keep the schedule shown on Sales Orders/Invoices equal to what the ledger holds."""

import frappe

from delivery_management.slots import get_reservations


def stamp_schedule(reference_doctype: str, reference_name: str) -> None:
	"""After an admin moves a booking, copy the new slot onto the order and its invoices."""
	if reference_doctype != "Sales Order":
		return
	values = {"dm_delivery_area": None, "dm_delivery_date": None, "dm_delivery_time": None,
		"dm_ac_installation_date": None}
	for r in get_reservations(reference_doctype, reference_name):
		if r.slot_type == "AC":
			values["dm_ac_installation_date"] = r.delivery_date
		else:
			values.update(dm_delivery_area=r.area, dm_delivery_date=r.delivery_date, dm_delivery_time=r.delivery_time)
	frappe.db.set_value("Sales Order", reference_name, values)
	invoices = frappe.get_all("Sales Invoice Item", filters={"sales_order": reference_name, "docstatus": ["<", 2]},
		pluck="parent", distinct=True)
	for invoice in invoices:
		frappe.db.set_value("Sales Invoice", invoice, values)
