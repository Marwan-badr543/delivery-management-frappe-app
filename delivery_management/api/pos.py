"""Endpoints used by POS Awesome."""

import json

import frappe
from frappe import _

from delivery_management.orders import address as address_utils
from delivery_management.orders import service
from delivery_management.orders.stock import MODE_INVOICE, MODE_ORDER, get_sellable_qty
from delivery_management.slots import engine, profile_cart
from delivery_management.slots.settings import get_settings


def _load(value, default=None):
	if value in (None, ""):
		return default
	return json.loads(value) if isinstance(value, str) else value


def _require_pos_user():
	if not frappe.has_permission("Sales Invoice", "create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
def get_delivery_setup():
	"""Static data for the POS delivery panel."""
	settings = get_settings()
	return {
		"areas": frappe.get_all("Delivery Area", filters={"enabled": 1}, pluck="name", order_by="area_name asc"),
		"booking_window_days": settings.booking_window_days,
		"slot_lead_time_hours": settings.slot_lead_time_hours,
		"main_warehouse": settings.main_warehouse,
	}


@frappe.whitelist()
def get_cart_profile(items):
	return profile_cart(_load(items, [])).as_dict()


@frappe.whitelist()
def get_delivery_slots(area, date):
	return engine.get_delivery_slots(area, date)


@frappe.whitelist()
def get_ac_calendar(units=0, start_date=None, days=None):
	return engine.get_ac_calendar(start_date, days, units)


@frappe.whitelist()
def get_customer_delivery_address(customer):
	if not frappe.has_permission("Customer", "read", customer):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return address_utils.get_customer_delivery_address(customer)


@frappe.whitelist()
def get_items_availability(item_codes, pos_profile):
	"""Sellable quantity of each item for both POS types: branch (Invoice) and main store (Order)."""
	item_codes = _load(item_codes, [])
	branch = frappe.db.get_value("POS Profile", pos_profile, "warehouse")
	main = get_settings().main_warehouse
	return {
		"invoice": get_sellable_qty(item_codes, branch, MODE_INVOICE),
		"order": get_sellable_qty(item_codes, main, MODE_ORDER) if main else {},
	}


@frappe.whitelist(methods=["POST"])
def submit(invoice, data, order_type="Invoice", delivery=None, unpaid=0):
	"""Single submit endpoint for POS Awesome, replacing ``posapp.submit_invoice``.

	``unpaid=1`` (Save/New on an Order): submit the order and invoice without payment.
	"""
	_require_pos_user()
	invoice, data, delivery = _load(invoice, {}), _load(data, {}), _load(delivery, {})
	done = service.already_submitted(invoice, data)
	if done:
		return done
	# No desk pop-ups in the POS: an informational msgprint (e.g. a server script's
	# "Delivery Note created") fights POS Awesome V15 for focus and freezes it.
	with service.quiet_messages():
		if order_type == service.ORDER_TYPE_ORDER:
			return service.submit_pos_order(invoice, data, delivery, unpaid=bool(frappe.utils.cint(unpaid)))
		return service.submit_pos_invoice(invoice, data, delivery)


def extend_bootinfo(bootinfo):
	"""Tells POS Awesome that delivery_management handles Orders (see the POS contract in DELIVERY_SYSTEM.md)."""
	bootinfo.delivery_management = {"enabled": 1}
