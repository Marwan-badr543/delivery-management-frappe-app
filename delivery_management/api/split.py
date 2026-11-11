"""Endpoints of the Delivery Note's Split Delivery / Merge Back buttons (``orders.split``)."""

import frappe

from delivery_management.orders import split as service
from delivery_management.slots import engine


@frappe.whitelist()
def get_split(delivery_note):
	"""What the split dialog needs: the note's lines, its type and address, the order's notes."""
	service.require_office()
	dn = frappe.get_doc("Delivery Note", delivery_note)
	service.check_in_store(dn)
	return {"name": dn.name, "order_type": dn.dm_delivery_type, "address": {f: dn.get(f) for f in service.ADDRESS},
		"lines": service.lines(dn), "notes": service.parts(dn)}


@frappe.whitelist()
def get_slots(area, date):
	service.require_office()
	return engine.get_delivery_slots(area, date)


@frappe.whitelist()
def get_ac_days(units=0, start_date=None):
	service.require_office()
	return engine.get_ac_calendar(start_date, 21, units)


@frappe.whitelist(methods=["POST"])
def split_delivery(delivery_note, moves, delivery_date=None, delivery_time=None, ac_date=None, address=None):
	moves = frappe.parse_json(moves) if isinstance(moves, str) else moves
	address = frappe.parse_json(address) if isinstance(address, str) else address
	return {"delivery_note": service.split(delivery_note, moves, delivery_date, delivery_time, ac_date, address)}


@frappe.whitelist(methods=["POST"])
def merge_back(delivery_note):
	return {"delivery_note": service.merge(delivery_note)}
