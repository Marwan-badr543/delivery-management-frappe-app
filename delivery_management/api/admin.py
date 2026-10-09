"""Endpoints for the Delivery Slots admin page. Restricted to delivery managers."""

import frappe
from frappe import _
from frappe.utils import cint

from delivery_management.orders.schedule import stamp_schedule
from delivery_management.slots import engine, get_ac_item_codes
from delivery_management.slots.timeutils import to_date, to_time_str

MANAGER_ROLES = ("System Manager", "Delivery Manager")


def _only_managers():
	frappe.only_for(MANAGER_ROLES)


def _reference_rows(filters):
	rows = frappe.get_all(
		engine.LEDGER,
		filters={**filters, "status": engine.STATUS_RESERVED},
		fields=["name", "reference_doctype", "reference_name", "customer", "contact_phone", "remarks", "qty",
			"forced", "delivery_time", "creation", "owner"],
		order_by="creation asc",
	)
	so_names = [r.reference_name for r in rows if r.reference_doctype == "Sales Order"]
	so_info = {
		r.name: r
		for r in frappe.get_all(
			"Sales Order",
			filters={"name": ["in", so_names or [""]]},
			fields=["name", "customer_name", "grand_total", "status", "dm_block_no", "dm_road_no", "dm_building_no"],
		)
	}
	ac_lines = {}
	if so_names and filters.get("slot_type") == engine.SLOT_AC:
		lines = frappe.get_all("Sales Order Item", filters={"parent": ["in", so_names]},
			fields=["parent", "item_code", "item_name", "stock_qty"], order_by="idx asc")
		ac_codes = get_ac_item_codes([l.item_code for l in lines])
		for l in lines:
			if l.item_code in ac_codes:
				ac_lines.setdefault(l.parent, []).append(l)
	for r in rows:
		r.order = so_info.get(r.reference_name)
		r.ac_items = ac_lines.get(r.reference_name, [])
	return rows


# ----------------------------- delivery slots -----------------------------


@frappe.whitelist()
def get_slot_board(area, date):
	_only_managers()
	area_row = engine.get_area(area)
	return {"area": area, "date": str(to_date(date)), "cars_number": area_row.cars_number,
		"enabled": cint(area_row.enabled), "slots": engine.get_delivery_slots(area, date)}


@frappe.whitelist()
def get_slot_reservations(area, date, time):
	_only_managers()
	return _reference_rows({"slot_type": engine.SLOT_DELIVERY, "area": area, "delivery_date": to_date(date),
		"delivery_time": to_time_str(time)})


@frappe.whitelist(methods=["POST"])
def set_slot_blocked(area, date, time, blocked, remarks=None):
	_only_managers()
	engine.set_slot_manual_block(area, date, time, cint(blocked), remarks)
	return engine.get_delivery_slots(area, date)


@frappe.whitelist(methods=["POST"])
def add_slot_reservation(area, date, time, remarks=None, contact_phone=None, customer=None, force=0):
	"""Manual booking (phone order, old system order...). ``force`` may exceed capacity."""
	_only_managers()
	engine.reserve(area=area, delivery_date=date, delivery_time=time, force=cint(force),
		remarks=remarks, contact_phone=contact_phone, customer=customer or None)
	return engine.get_delivery_slots(area, date)


@frappe.whitelist(methods=["POST"])
def release_reservation(reservation):
	_only_managers()
	row = frappe.db.get_value(engine.LEDGER, reservation, ["reference_doctype", "reference_name"], as_dict=True)
	if row and row.reference_doctype:
		# Bookings owned by a document are freed by cancelling that document.
		frappe.throw(_("This booking belongs to {0} {1}. Cancel that document to free it.").format(
			_(row.reference_doctype), row.reference_name))
	return engine.release_reservation(reservation)


@frappe.whitelist(methods=["POST"])
def move_reservation(reservation, area=None, date=None, time=None, ac_date=None, force=0):
	"""Reschedule one booking (delivery slot or AC day) of an order."""
	_only_managers()
	row = frappe.db.get_value(engine.LEDGER, reservation, ["reference_doctype", "reference_name"], as_dict=True)
	new = engine.move_reservation(reservation, area=area, delivery_date=date, delivery_time=time, ac_date=ac_date,
		force=cint(force))
	if row and row.reference_doctype:
		stamp_schedule(row.reference_doctype, row.reference_name)
	return new


# ------------------------------- AC days ---------------------------------


@frappe.whitelist()
def get_ac_day(date):
	_only_managers()
	status = engine.get_ac_day_status(date)
	status["reservations"] = _reference_rows({"slot_type": engine.SLOT_AC, "delivery_date": to_date(date)})
	return status


@frappe.whitelist()
def get_ac_overview(start_date=None, days=14):
	_only_managers()
	return engine.get_ac_calendar(start_date, cint(days) or 14)


@frappe.whitelist(methods=["POST"])
def set_ac_day_blocked(date, blocked):
	_only_managers()
	engine.set_ac_day_manual_block(date, cint(blocked))
	return get_ac_day(date)


@frappe.whitelist(methods=["POST"])
def set_ac_day_capacity(date, capacity=None):
	"""Empty ``capacity`` removes the override and falls back to the global value."""
	_only_managers()
	engine.set_ac_day_capacity(date, None if capacity in (None, "") else cint(capacity))
	return get_ac_day(date)


@frappe.whitelist(methods=["POST"])
def add_ac_reservation(date, units, remarks=None, contact_phone=None, customer=None, force=0):
	_only_managers()
	engine.reserve(ac_date=date, ac_units=cint(units), force=cint(force), remarks=remarks,
		contact_phone=contact_phone, customer=customer or None)
	return get_ac_day(date)
