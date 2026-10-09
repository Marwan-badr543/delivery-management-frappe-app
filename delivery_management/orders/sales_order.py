"""Delivery schedule on the Sales Order itself.

The schedule fields (``dm_delivery_area``, ``dm_delivery_date``,
``dm_delivery_time``, ``dm_ac_installation_date``) are optional on any Sales
Order, whether it comes from the POS or is made by hand in the Desk. When they
are filled, submitting the order books that capacity (``before_submit``, so a
slot that just became full stops the submit before the Delivery Note server
script runs). Cancelling releases it (``events.on_sales_order_cancel``).

``validate`` refuses half-filled schedules and slots that are not free, so a
user cannot pick a taken slot by typing it in.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from delivery_management.orders import address as address_utils
from delivery_management.slots import engine, profile_cart
from delivery_management.slots.timeutils import to_date, to_time_str

REASON_LABELS = {
	engine.REASON_AREA_DISABLED: "the area is disabled",
	engine.REASON_NOT_WORKING: "it is not a working hour of the area",
	engine.REASON_TOO_SOON: "it is too soon",
	engine.REASON_PAST: "it is in the past",
	engine.REASON_MANUAL: "it is closed",
	engine.REASON_FULL: "it is full",
	engine.REASON_NOT_ENOUGH: "there is not enough capacity left",
}


def validate(doc, method=None):
	_validate_address_numbers(doc)
	booking = requested_booking(doc)
	if doc.docstatus == 0:
		_check_available(doc, booking)


def before_submit(doc, method=None):
	booking = requested_booking(doc)
	held = {r.slot_type for r in engine.get_reservations(doc.doctype, doc.name)}
	if engine.SLOT_AC in held:
		booking.pop("ac_date", None)
		booking.pop("ac_units", None)
	if engine.SLOT_DELIVERY in held:
		for key in ("area", "delivery_date", "delivery_time"):
			booking.pop(key, None)
	if not booking:
		return
	engine.reserve(
		doc.doctype,
		doc.name,
		customer=doc.customer,
		contact_phone=doc.get("contact_mobile") or frappe.db.get_value("Customer", doc.customer, "mobile_no"),
		**booking,
	)


def requested_booking(doc) -> dict:
	"""The capacity the order's schedule fields ask for, as ``engine.reserve`` kwargs."""
	booking: dict = {}
	date, time = doc.get("dm_delivery_date"), doc.get("dm_delivery_time")
	if not date:
		# A Time field defaults to "now" on new documents; without a date it means nothing.
		doc.dm_delivery_time = time = None
	if date:
		missing = [
			label
			for value, label in ((doc.get("dm_delivery_area"), _("Delivery Area")), (date, _("Delivery Slot Date")),
				(time, _("Delivery Slot Time")))
			if not value
		]
		if missing:
			frappe.throw(
				_("Please also set {0}, or clear the delivery slot.").format(", ".join(missing)),
				title=_("Delivery Slot"),
			)
		booking.update(area=doc.dm_delivery_area, delivery_date=to_date(date), delivery_time=to_time_str(time))

	ac_units = profile_cart(doc.get("items") or []).ac_units
	doc.dm_ac_units = ac_units
	if doc.get("dm_ac_installation_date"):
		if not ac_units:
			frappe.throw(
				_("This order has no AC unit to install. Clear the AC Installation Date."),
				title=_("AC Installation"),
			)
		booking.update(ac_date=to_date(doc.dm_ac_installation_date), ac_units=ac_units)
	return booking


def _check_available(doc, booking: dict) -> None:
	"""Early, non-locking check; ``engine.reserve`` re-checks under a lock on submit."""
	held = {r.slot_type: r for r in engine.get_reservations(doc.doctype, doc.name)} if not doc.is_new() else {}

	if booking.get("delivery_time") and engine.SLOT_DELIVERY not in held:
		time = booking["delivery_time"]
		slot = next(
			(s for s in engine.get_delivery_slots(booking["area"], booking["delivery_date"]) if s["delivery_time"] == time),
			None,
		)
		if not slot:
			_refuse(_("{0} is not a delivery slot of {1} on {2}.").format(
				time[:5], booking["area"], frappe.format(booking["delivery_date"], "Date")))
		if not slot["available"]:
			_refuse(_("The delivery slot {0} in {1} on {2} cannot be booked: {3}.").format(
				time[:5], booking["area"], frappe.format(booking["delivery_date"], "Date"), _reasons(slot["reasons"])))

	if booking.get("ac_date") and engine.SLOT_AC not in held:
		day = engine.get_ac_day_status(booking["ac_date"], booking["ac_units"])
		if not day["available"]:
			_refuse(_("AC installation on {0} cannot take {1} unit(s): {2} ({3} left).").format(
				frappe.format(booking["ac_date"], "Date"), booking["ac_units"], _reasons(day["reasons"]),
				day["remaining"]))


def _reasons(reasons) -> str:
	return ", ".join(_(REASON_LABELS.get(r, r)) for r in reasons)


def _refuse(message: str) -> None:
	frappe.throw(message, engine.SlotUnavailableError, title=_("Slot not available"))


def _validate_address_numbers(doc) -> None:
	address_utils.clean_numbers(doc)


@frappe.whitelist()
def get_schedule_options(area=None, date=None, items=None, sales_order=None):
	"""Free delivery slots and AC days for the Sales Order form's slot picker."""
	import json

	if not frappe.has_permission("Sales Order", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	items = json.loads(items) if isinstance(items, str) else (items or [])
	profile = profile_cart(items)
	result = {"ac_units": profile.ac_units, "slots": [], "ac_days": []}
	if area and date:
		result["slots"] = [s for s in engine.get_delivery_slots(area, date) if s["available"]]
	if profile.ac_units:
		result["ac_days"] = [d for d in engine.get_ac_calendar(None, None, profile.ac_units) if d["available"]]
	return result
