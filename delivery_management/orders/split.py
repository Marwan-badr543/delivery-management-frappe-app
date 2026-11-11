"""Split delivery: part of an order goes on another day, as its own Delivery Note.

The office splits a draft Delivery Note still in the store (``split``): the quantities it
moves leave the note for a **new draft Delivery Note** of the same order and type, linked
line by line to the same Sales Order rows (so the order lists every note), with its own
day: an AC installation day for an AC note, a delivery slot for a Non AC note. From there
each note is an order of its own: Driver Assignment, Stock Keeper, Delivery Schedule,
returns.

* **Slots.** The new note books its own capacity in its own name (``Delivery Note``); the
  order keeps its booking for the first note. AC units moved off the first note leave the
  order's AC booking (``engine.set_ac_units``). A note's own booking is freed when it is
  cancelled or deleted.
* **Stock.** Reservations stay on the order lines: nothing moves. While one part is out
  with a driver ERPNext keeps no reservation on its lines (``transit.service.dispatch``),
  so the POS holds the parts still in the store (``orders.stock.held_for_split_notes``);
  the last part to be delivered or come back reserves the rest again.
* **What moves.** Stock lines (bundles by whole bundles), never the delivery charge or
  services; the first note keeps at least one stock line. Serials are picked at send-out,
  so the split lines lose the serials a draft may hold.
* **Where.** The new note may go to another Delivery Area and address (block, road,
  building); its slot is booked in that area.
* **Merge back** (``merge``): a part not sent out yet returns to its first note (both still
  in the store) and gives its day back.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from delivery_management.control import log
from delivery_management.drivers import assignment as asg
from delivery_management.drivers.delivery_note import sales_order_of
from delivery_management.slots import engine, profile_cart
from delivery_management.slots.timeutils import to_time_str

DN = "Delivery Note"
MANAGERS = ("System Manager", "Delivery Manager")
EPSILON = 0.0005


def require_office():
	if frappe.session.user != "Administrator" and not set(MANAGERS) & set(frappe.get_roles()):
		frappe.throw(_("Only the delivery office can split or merge a delivery."), frappe.PermissionError)


def check_in_store(dn) -> None:
	if dn.docstatus != 0 or cint(dn.is_return):
		frappe.throw(_("{0} is not a draft Delivery Note.").format(dn.name))
	if dn.get("dm_delivery_status"):
		frappe.throw(_("The goods of {0} already left the store ({1}). Split or merge only notes still in the store.")
			.format(dn.name, _(dn.dm_delivery_status)))
	if not dn.get("dm_delivery_type"):
		frappe.throw(_("Choose the Order Type of {0} first.").format(dn.name))


def lines(dn) -> list[dict]:
	"""The note's lines and how much of each may move (stock goods, whole bundles)."""
	codes = {r.item_code for r in dn.items}
	stock = set(frappe.get_all("Item", filters={"name": ["in", list(codes) or [""]], "is_stock_item": 1}, pluck="name"))
	bundles = set(frappe.get_all("Product Bundle", filters={"name": ["in", list(codes) or [""]], "disabled": 0},
		pluck="name"))
	return [{
		"dn_detail": r.name, "item_code": r.item_code, "item_name": r.item_name, "qty": flt(r.qty), "uom": r.uom,
		"is_bundle": r.item_code in bundles, "can_move": r.item_code in stock or r.item_code in bundles,
		"whole": r.item_code in bundles or bool(frappe.get_cached_value("UOM", r.uom, "must_be_whole_number")),
	} for r in dn.items]


def _plan(dn, moves: dict) -> dict[str, float]:
	"""``{dn_detail: qty}`` checked against the note."""
	allowed = {line["dn_detail"]: line for line in lines(dn)}
	plan = {}
	for dn_detail, qty in (moves or {}).items():
		qty = flt(qty)
		if qty <= EPSILON:
			continue
		line = allowed.get(dn_detail)
		if not line:
			frappe.throw(_("Line {0} is not on {1}.").format(dn_detail, dn.name))
		if not line["can_move"]:
			frappe.throw(_("{0} stays on the first note: only goods are split.").format(line["item_name"]))
		if qty > line["qty"] + EPSILON:
			frappe.throw(_("{0}: {1} can move at most.").format(line["item_name"], line["qty"]))
		if line["whole"] and qty != int(qty):
			frappe.throw(_("{0} moves in whole units.").format(line["item_name"]))
		plan[dn_detail] = qty
	if not plan:
		frappe.throw(_("Enter how much of each item goes on the new delivery."))
	kept = [line for line in allowed.values() if line["can_move"] and line["qty"] - plan.get(line["dn_detail"], 0) > EPSILON]
	if not kept:
		frappe.throw(_("Leave at least one item on {0}. To deliver all of it on another day, move its slot instead.")
			.format(dn.name))
	return plan


def _clear_serials(row) -> None:
	# Serials are scanned when the goods leave the store; a split line keeps none.
	row.serial_no = None
	row.serial_and_batch_bundle = None


def _set_qty(row, qty: float) -> None:
	row.qty = qty
	row.stock_qty = flt(qty) * (flt(row.conversion_factor) or 1)


def _booking_kwargs(dn, delivery_date=None, delivery_time=None, ac_date=None) -> dict:
	party = {"customer": dn.customer,
		"contact_phone": dn.get("contact_mobile") or frappe.db.get_value("Customer", dn.customer, "mobile_no")}
	if dn.dm_delivery_type == asg.TYPE_AC:
		if not ac_date:
			frappe.throw(_("Choose the installation day of the new delivery."))
		units = profile_cart(dn.items).ac_units
		return {**party, "ac_date": getdate(ac_date), "ac_units": units} if units else {}
	if not (delivery_date and delivery_time):
		frappe.throw(_("Choose the delivery day and time of the new delivery."))
	if not dn.get("dm_delivery_area"):
		frappe.throw(_("{0} has no Delivery Area.").format(dn.name))
	return {**party, "area": dn.dm_delivery_area, "delivery_date": getdate(delivery_date),
		"delivery_time": to_time_str(delivery_time)}


def ac_booking_of(dn) -> str | None:
	"""The AC booking the note's units are counted in: its own, or else its order's."""
	for doctype, name in ((DN, dn.name), ("Sales Order", sales_order_of(dn))):
		row = next((r for r in engine.get_reservations(doctype, name) if r.slot_type == engine.SLOT_AC), None) \
			if name else None
		if row:
			return row
	return None


def _change_ac_booking(dn, change: int) -> None:
	if not change:
		return
	row = ac_booking_of(dn)
	if row:
		engine.set_ac_units(row.name, cint(row.qty) + change)


ADDRESS = ("dm_delivery_area", "dm_block_no", "dm_road_no", "dm_building_no")


def split(delivery_note: str, moves: dict, delivery_date=None, delivery_time=None, ac_date=None,
		address: dict | None = None) -> str:
	"""Move ``moves`` (``{dn_detail: qty}``) of a draft note to a new note on another day,
	at ``address`` (``ADDRESS`` fields; the first note's by default). Returns the new Delivery Note."""
	require_office()
	dn = frappe.get_doc(DN, delivery_note, for_update=True)
	check_in_store(dn)
	plan = _plan(dn, moves)
	# All or nothing: a slot refused after the notes were saved leaves both as they were.
	frappe.db.savepoint("dm_split")
	try:
		return _split(dn, plan, delivery_date, delivery_time, ac_date, address or {})
	except Exception:
		frappe.db.rollback(save_point="dm_split")
		raise


def _split(dn, plan, delivery_date, delivery_time, ac_date, address) -> str:
	part = frappe.copy_doc(dn)
	for field in ADDRESS:
		if field in address:
			part.set(field, (address[field] or "").strip() or None)
	if part.dm_delivery_area and not frappe.db.exists("Delivery Area", part.dm_delivery_area):
		frappe.throw(_("Delivery Area {0} does not exist.").format(part.dm_delivery_area))
	for row, new_row in zip(dn.items, part.items):
		moved = plan.get(row.name, 0)
		_set_qty(new_row, moved)
		_clear_serials(new_row)
		if moved:
			_set_qty(row, flt(row.qty) - moved)
			_clear_serials(row)
	part.set("items", [r for r in part.items if flt(r.qty) > EPSILON])
	dn.set("items", [r for r in dn.items if flt(r.qty) > EPSILON])
	for note in (dn, part):
		note.set("packed_items", [])  # rebuilt from the lines on validate
		for i, r in enumerate(note.items, 1):
			r.idx = i
	# A fixed charge (an "Actual" tax) is charged once, on the first note.
	part.set("taxes", [t for t in part.taxes if t.charge_type != "Actual"])

	moved_ac = profile_cart(part.items).ac_units
	booking = _booking_kwargs(part, delivery_date, delivery_time, ac_date)
	part.update({"dm_split_from": dn.name, "dm_driver": None, "dm_delivery_status": None})
	if dn.dm_delivery_type == asg.TYPE_AC:
		part.update({"dm_ac_installation_date": booking.get("ac_date") or ac_date, "dm_ac_units": moved_ac})
		dn.dm_ac_units = profile_cart(dn.items).ac_units
	else:
		part.update({"dm_delivery_date": booking["delivery_date"], "dm_delivery_time": booking["delivery_time"]})

	dn.flags.ignore_permissions = part.flags.ignore_permissions = True
	dn.save()
	part.insert()
	_change_ac_booking(dn, -moved_ac)
	if booking:
		engine.reserve(DN, part.name, **booking)

	when = booking.get("ac_date") or booking.get("delivery_date")
	note = _("Moved to {0} for {1}").format(part.name, frappe.format(when, "Date")) if when else None
	if note and part.dm_delivery_area != dn.dm_delivery_area:
		note += " " + _("in {0}").format(part.dm_delivery_area or _("no area"))
	log.add("Delivery split", delivery_note=dn.name, reference=(DN, part.name),
		items=[{"item_name": r.item_name, "qty": r.qty} for r in part.items], note=note)
	return part.name


def merge(delivery_note: str) -> str:
	"""A split part not sent out yet goes back to the note it came from. Returns that note."""
	require_office()
	part = frappe.get_doc(DN, delivery_note, for_update=True)
	first = part.get("dm_split_from")
	if not first:
		frappe.throw(_("{0} was not split from another note.").format(part.name))
	check_in_store(part)
	if not frappe.db.exists(DN, first):
		frappe.throw(_("{0} no longer exists.").format(first))
	dn = frappe.get_doc(DN, first, for_update=True)
	check_in_store(dn)

	rows = {(r.so_detail, r.item_code): r for r in dn.items}
	for r in part.items:
		target = rows.get((r.so_detail, r.item_code))
		if target:
			_set_qty(target, flt(target.qty) + flt(r.qty))
			_clear_serials(target)
		else:
			new_row = dn.append("items", {k: v for k, v in r.as_dict().items()
				if k not in ("name", "parent", "parentfield", "parenttype", "idx", "doctype")})
			_clear_serials(new_row)
	dn.set("packed_items", [])
	back_ac = profile_cart(part.items).ac_units
	if dn.dm_delivery_type == asg.TYPE_AC:
		dn.dm_ac_units = profile_cart(dn.items).ac_units

	engine.release(DN, part.name)
	_change_ac_booking(dn, back_ac)
	dn.flags.ignore_permissions = True
	dn.save()
	log.add("Delivery merged back", delivery_note=dn.name, reference=(DN, part.name),
		items=[{"item_name": r.item_name, "qty": r.qty} for r in part.items])
	frappe.delete_doc(DN, part.name, ignore_permissions=True)
	return dn.name


def parts(dn) -> list[dict]:
	"""Every non-cancelled note of the note's order, for the split dialog."""
	so = sales_order_of(dn)
	if not so:
		return []
	names = frappe.get_all("Delivery Note Item", filters={"against_sales_order": so, "docstatus": ["<", 2]},
		pluck="parent", distinct=True)
	notes = frappe.get_all(DN, filters={"name": ["in", names or [""]], "is_return": 0},
		fields=["name", "docstatus", "dm_delivery_status", "dm_delivery_type", "dm_delivery_date", "dm_delivery_time",
			"dm_ac_installation_date", "dm_driver"], order_by="creation asc")
	return notes


def release_own_booking(doc, method=None):
	"""Delivery Note ``on_cancel`` / ``on_trash``: a split part's own day is freed."""
	engine.release(DN, doc.name)


def detach_parts(doc, method=None):
	"""Delivery Note ``on_trash``: its split parts (e.g. its goods all cancelled from the POS)
	stand on their own."""
	for name in frappe.get_all(DN, filters={"dm_split_from": doc.name}, pluck="name"):
		frappe.db.set_value(DN, name, "dm_split_from", None, update_modified=False)


def booking_owner(dn) -> tuple[str, str] | None:
	"""Whose booking holds the note's day: the note itself (a split part) or its order."""
	slot_type = engine.SLOT_AC if dn.dm_delivery_type == asg.TYPE_AC else engine.SLOT_DELIVERY
	if any(r.slot_type == slot_type for r in engine.get_reservations(DN, dn.name)):
		return DN, dn.name
	so = sales_order_of(dn)
	return ("Sales Order", so) if so else None


def own_booking_notes(names: list[str]) -> set[str]:
	"""The notes among ``names`` that hold a day of their own (split parts)."""
	return set(frappe.get_all(engine.LEDGER, filters={"reference_doctype": DN, "reference_name": ["in", names or [""]],
		"status": engine.STATUS_RESERVED}, pluck="reference_name", distinct=True))

