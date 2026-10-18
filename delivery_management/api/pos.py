"""Endpoints used by POS Awesome."""

import json

import frappe
from frappe import _

from delivery_management.orders import address as address_utils
from delivery_management.orders import service
from delivery_management.orders.stock import get_sellable_qty
from delivery_management.slots import engine, profile_cart
from delivery_management.slots.cart import KIND_AC as KIND_AC_ITEM, KIND_OTHER as KIND_OTHER_ITEM, classify_items
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
def get_items_availability(item_codes, pos_profile, rules=1):
	"""Sellable quantity of each item for both POS types: branch (Invoice) and main store (Order),
	plus the POS rules of each item (see :func:`item_rules`)."""
	item_codes = _load(item_codes, [])
	branch = frappe.db.get_value("POS Profile", pos_profile, "warehouse")
	settings = get_settings()
	main = settings.main_warehouse
	return {
		"invoice": with_bundles(item_codes, branch),
		"order": with_bundles(item_codes, main) if main else {},
		# Rules do not change with stock: the refresh after a sale skips them (rules=0).
		"rules": item_rules(item_codes) if frappe.utils.cint(rules) else {},
		"delivery_charge_item": settings.delivery_charge_item,
	}


def with_bundles(item_codes, warehouse) -> dict:
	"""Sellable qty per item; a Product Bundle can be sold as many times as its scarcest
	stock component allows (non-stock components do not limit it)."""
	qty = get_sellable_qty(item_codes, warehouse)
	bundles = dict(frappe.get_all("Product Bundle", filters={"new_item_code": ["in", list(item_codes) or [""]],
		"disabled": 0}, fields=["new_item_code", "name"], as_list=True))
	if not bundles:
		return qty
	rows = frappe.get_all("Product Bundle Item", filters={"parent": ["in", list(bundles.values())],
		"parenttype": "Product Bundle"}, fields=["parent", "item_code", "qty"])
	comp_qty = get_sellable_qty([r.item_code for r in rows], warehouse)
	for code, bundle in bundles.items():
		limits = [frappe.utils.flt(comp_qty.get(r.item_code)) // frappe.utils.flt(r.qty)
			for r in rows if r.parent == bundle and comp_qty.get(r.item_code) is not None and frappe.utils.flt(r.qty) > 0]
		qty[code] = int(min(limits)) if limits else None
	return qty


# Delivery kind of one item for the POS guard. Bundles take the kind of their components.
KIND_AC, KIND_OTHER, KIND_MIXED, KIND_NEUTRAL = "ac", "other", "mixed", "neutral"


def item_rules(item_codes) -> dict:
	"""``{item_code: {"kind", "delivery_charge"}}``.

	kind: ``ac`` (AC unit), ``other`` (TV / other stock goods), ``mixed`` (a bundle with
	both), ``neutral`` (AC accessory, service, charge: fits any order). Same rule as
	``profile_cart``, done in a few queries for the whole list.
	"""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	if not codes:
		return {}
	flagged = set(frappe.get_all("Item", filters={"name": ["in", codes], "dm_has_delivery_charge": 1}, pluck="name"))
	bundles = dict(frappe.get_all("Product Bundle", filters={"new_item_code": ["in", codes], "disabled": 0},
		fields=["new_item_code", "name"], as_list=True))
	components: dict[str, list[str]] = {}
	if bundles:
		for row in frappe.get_all("Product Bundle Item", filters={"parent": ["in", list(bundles.values())],
				"parenttype": "Product Bundle"}, fields=["parent", "item_code"]):
			components.setdefault(row.parent, []).append(row.item_code)
	parts = {code: components.get(bundles[code], []) if code in bundles else [code] for code in codes}
	all_codes = list({c for lst in parts.values() for c in lst})
	kinds = classify_items(all_codes)
	stock = set(frappe.get_all("Item", filters={"name": ["in", all_codes or [""]], "is_stock_item": 1}, pluck="name"))
	rules = {}
	for code in codes:
		has_ac = any(kinds.get(c) == KIND_AC_ITEM for c in parts[code])
		has_other = any(kinds.get(c) == KIND_OTHER_ITEM and c in stock for c in parts[code])
		kind = KIND_MIXED if has_ac and has_other else KIND_AC if has_ac else KIND_OTHER if has_other else KIND_NEUTRAL
		rules[code] = {"kind": kind, "delivery_charge": code in flagged}
	return rules


def check_pos_order_rules(items) -> None:
	"""POS Orders only: the delivery charge item at most once, qty 1 (one order pays
	one delivery). AC and TV / other goods may share an order: it gets one Delivery
	Note per team (``orders.delivery_note.split_by_order_type``).
	"""
	charge = get_settings().delivery_charge_item
	if charge:
		rows = [i for i in items if i.get("item_code") == charge]
		if len(rows) > 1 or sum(frappe.utils.flt(i.get("qty")) for i in rows) > 1:
			frappe.throw(_("The delivery charge ({0}) can be in an order only once, with qty 1.").format(charge),
				title=_("Delivery charge"))


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
			check_pos_order_rules(invoice.get("items") or [])
			return service.submit_pos_order(invoice, data, delivery, unpaid=bool(frappe.utils.cint(unpaid)))
		return service.submit_pos_invoice(invoice, data, delivery)


def extend_bootinfo(bootinfo):
	"""Tells POS Awesome that delivery_management handles Orders (see the POS contract in DELIVERY_SYSTEM.md)."""
	bootinfo.delivery_management = {"enabled": 1}
