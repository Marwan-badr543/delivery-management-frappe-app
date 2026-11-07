"""Goods out with the driver / AC Tech: the transit store between the main store and the customer.

The order's Delivery Note stays a draft until the goods are installed:

* **Send out** (Stock Keeper page, ``dispatch``): the stock keeper scans every serial of the
  note. The order's reservation in the main store is released, a Material Transfer moves the
  goods to the transit store (Delivery Settings), the note takes the transit store and the
  scanned serials and goes "On the way", and a **Delivery Transit** records what left, when,
  with whom, for which customer.
* **At the customer** (Delivery Schedule page): the driver takes off the note what is not
  installed (``take_off_note``; a Credit Note follows, ``returns.service.cancel_not_installed``)
  and submits it when the customer signs (``api.drivers.mark_delivered``): the stock leaves
  the transit store to the customer. When nothing can be delivered ("Customer not found",
  ``bring_back_all``), the note is "Returned": still a draft, nothing credited.
* **Back in the store** (Stock Keeper page, ``receive``): the stock keeper scans what came
  back; a Material Transfer moves it to the main store. A note that came back whole is
  ready to be sent again (main store, no serials), its goods reserved again for the order
  when asked. The Delivery Transit is removed once nothing of it is outside the store.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, get_fullname, now_datetime

from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

from delivery_management.control import log
from delivery_management.drivers import assignment as asg
from delivery_management.drivers.delivery_note import DELIVERED, ON_THE_WAY, RETURNED, sales_order_of
from delivery_management.orders import reservation
from delivery_management.slots.cart import profile_cart
from delivery_management.slots.settings import get_settings

DN = "Delivery Note"
TRANSIT = "Delivery Transit"
COMING_BACK = "Coming back"
EPSILON = 1e-6


def stores() -> tuple[str, str]:
	"""``(main store, transit store)`` from Delivery Settings."""
	settings = get_settings()
	if not settings.main_warehouse or not settings.transit_warehouse:
		frappe.throw(_("Set the Main Store and the Transit Store in Delivery Settings first."),
			title=_("Delivery Settings"))
	return settings.main_warehouse, settings.transit_warehouse


def is_out(dn) -> bool:
	"""The goods of this draft note left the main store (On the way, or Returned whole)."""
	return dn.docstatus == 0 and dn.get("dm_delivery_status") in (ON_THE_WAY, RETURNED)


# ---------------------------------------------------------------------------
# Stock lines of a Delivery Note: stock rows and the stock components of bundles
# ---------------------------------------------------------------------------


def key(dn_detail: str, item_code: str) -> str:
	return f"{dn_detail}|{item_code}"


def stock_lines(dn) -> list[frappe._dict]:
	"""What physically moves: ``key``, ``item_code``, ``qty`` (stock UOM), ``serials``,
	``dn_detail`` (the note row; a bundle's row for its components), ``bundle_item``."""
	packed = list(dn.get("packed_items") or [])
	codes = {r.item_code for r in dn.items} | {p.item_code for p in packed}
	items = {i.name: i for i in frappe.get_all("Item", filters={"name": ["in", list(codes) or [""]]},
		fields=["name", "item_name", "is_stock_item", "has_serial_no", "stock_uom"])}
	bundles = {p.parent_detail_docname for p in packed}
	lines = []
	for r in dn.items:
		item = items.get(r.item_code)
		if r.name in bundles or not item or not item.is_stock_item:
			continue
		lines.append(_line(r.name, item, flt(r.stock_qty) or flt(r.qty) * (flt(r.conversion_factor) or 1), r, None))
	for p in packed:
		item = items.get(p.item_code)
		if item and item.is_stock_item:
			lines.append(_line(p.parent_detail_docname, item, flt(p.qty), p, p.parent_item))
	return lines


def _line(dn_detail, item, qty, row, bundle_item) -> frappe._dict:
	return frappe._dict(key=key(dn_detail, item.name), dn_detail=dn_detail, item_code=item.name,
		item_name=item.item_name, stock_uom=item.stock_uom, has_serial_no=cint(item.has_serial_no), qty=qty,
		serials=get_serial_nos(row.get("serial_no") or ""), warehouse=row.get("warehouse"), row=row,
		bundle_item=bundle_item)


def _set_rows(dn, warehouse: str, serials: dict[str, list[str]] | None) -> None:
	"""Put the note's stock rows (bundles and their components too) in ``warehouse`` with the
	given serials (``{key: [..]}``; None clears them)."""
	dn.set_warehouse = warehouse
	moved = {line.dn_detail for line in stock_lines(dn)}
	for r in dn.items:
		if r.name not in moved:
			continue
		r.warehouse = warehouse
		sns = (serials or {}).get(key(r.name, r.item_code))
		r.update({"serial_no": "\n".join(sns) if sns else None, "serial_and_batch_bundle": None,
			"use_serial_batch_fields": 1})
	for p in dn.get("packed_items") or []:
		p.warehouse = warehouse
		sns = (serials or {}).get(key(p.parent_detail_docname, p.item_code))
		p.update({"serial_no": "\n".join(sns) if sns else None, "serial_and_batch_bundle": None,
			"use_serial_batch_fields": 1})


def _save(dn) -> None:
	dn.flags.dm_transit = True
	dn.flags.ignore_permissions = True
	dn.save()


def _transfer(dn, lines, source: str, target: str, remark: str) -> str:
	"""Material Transfer of ``lines`` (``item_code``, ``qty``, ``serials``) for the note."""
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Transfer",
		"purpose": "Material Transfer",
		"company": dn.company,
		"from_warehouse": source,
		"to_warehouse": target,
		"dm_delivery_note": dn.name,
		"remarks": remark,
		"items": [{"item_code": line.item_code, "qty": line.qty, "uom": line.stock_uom, "stock_uom": line.stock_uom,
			"conversion_factor": 1, "s_warehouse": source, "t_warehouse": target,
			"serial_no": "\n".join(line.serials) if line.serials else None,
			"use_serial_batch_fields": 1} for line in lines],
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


# ---------------------------------------------------------------------------
# Send out
# ---------------------------------------------------------------------------


def dispatch(delivery_note: str, scans: dict) -> str:
	"""The stock keeper hands the goods of a draft note to its driver. ``scans``:
	``{line key: [serials]}`` for every serialised line. Returns the Delivery Transit."""
	main, transit = stores()
	dn = frappe.get_doc(DN, delivery_note, for_update=True)
	if dn.docstatus != 0 or cint(dn.is_return):
		frappe.throw(_("{0} is not a draft Delivery Note.").format(dn.name))
	if dn.get("dm_delivery_status"):
		frappe.throw(_("The goods of {0} already left the store ({1}).").format(dn.name, _(dn.dm_delivery_status)))
	if not dn.get("dm_driver"):
		frappe.throw(_("Assign a driver / AC Tech to {0} before the goods leave.").format(dn.name))
	lines = stock_lines(dn)
	if not lines:
		frappe.throw(_("{0} has no stock items to send out.").format(dn.name))
	away = [line for line in lines if line.warehouse != main]
	if away:
		frappe.throw(_("{0} is to leave from {1}; the Stock Keeper sends goods out of the Main Store ({2}) only.")
			.format(away[0].item_code, away[0].warehouse, main))
	from delivery_management.returns.service import check_not_returned, unreserve

	check_not_returned(dn)
	serials = _check_out_scans(lines, scans or {}, main)
	for line in lines:
		line.serials = serials.get(line.key, [])

	# ERPNext refuses a note row out of the store its order line is reserved in: the whole line
	# is released while the goods are out (what it delivers later is reserved again, ``reserve_rest``).
	for so_detail in {r.so_detail for r in dn.items if r.get("so_detail")}:
		unreserve(so_detail, flt(frappe.db.get_value("Sales Order Item", so_detail, "qty")))
	driver = get_fullname(dn.dm_driver)
	entry = _transfer(dn, lines, main, transit, _("{0}: out with {1}").format(dn.name, driver))
	_set_rows(dn, transit, serials)
	dn.dm_delivery_status = ON_THE_WAY
	_save(dn)

	doc = frappe.get_doc({
		"doctype": TRANSIT, "delivery_note": dn.name, "sales_order": sales_order_of(dn), "status": ON_THE_WAY,
		"delivery_type": dn.dm_delivery_type, "customer": dn.customer, "customer_name": dn.customer_name,
		"mobile_no": dn.get("contact_mobile") or frappe.db.get_value("Customer", dn.customer, "mobile_no"),
		"address": _address(dn), "driver": dn.dm_driver, "driver_name": driver,
		"work_date": asg.schedule_of(dn)[0], "out_at": now_datetime(), "out_by": frappe.session.user,
		"out_entry": entry,
		"items": [{"item_code": line.item_code, "item_name": line.item_name, "stock_uom": line.stock_uom,
			"qty": line.qty, "serial_no": "\n".join(line.serials) or None, "dn_detail": line.dn_detail,
			"bundle_item": line.bundle_item} for line in lines],
	})
	doc.insert(ignore_permissions=True)
	log.add("Sent out", delivery_note=dn.name, driver=dn.dm_driver, reference=("Stock Entry", entry),
		items=[{"item_name": line.item_name, "qty": line.qty, "serials": line.serials} for line in lines])
	return doc.name


def reserve_rest(dn, warehouse: str) -> None:
	"""Reserve again in ``warehouse`` what the note's order lines still deliver later and is
	not out with a driver (released when the note was sent out)."""
	from delivery_management.returns.service import out_qty

	later: dict[tuple, float] = defaultdict(float)
	for so_detail in {r.so_detail for r in dn.items if r.get("so_detail")}:
		soi = frappe.db.get_value("Sales Order Item", so_detail, ["item_code", "qty", "delivered_qty", "returned_qty",
			"conversion_factor", "reserve_stock"], as_dict=True)
		held = sum(flt(r.reserved_qty) - flt(r.delivered_qty) for r in frappe.get_all("Stock Reservation Entry",
			filters={"voucher_detail_no": so_detail, "docstatus": 1}, fields=["reserved_qty", "delivered_qty"]))
		rest = flt(soi.qty) - flt(soi.delivered_qty) - flt(soi.returned_qty) - out_qty(so_detail)
		if not cint(soi.reserve_stock) or held > EPSILON or rest <= EPSILON:
			continue
		packed = frappe.get_all("Packed Item", filters={"parenttype": "Sales Order", "parent_detail_docname": so_detail},
			fields=["item_code", "qty"])
		for p in packed:
			later[(so_detail, p.item_code, True)] += flt(p.qty) * rest / flt(soi.qty)
		if not packed:
			later[(so_detail, soi.item_code, False)] += rest * (flt(soi.conversion_factor) or 1)
	if later:
		reservation.reserve_again(sales_order_of(dn), warehouse, later)


def _address(dn) -> str:
	from delivery_management.api.drivers import format_address

	return format_address(frappe._dict(area=dn.get("dm_delivery_area"), block_no=dn.get("dm_block_no"),
		road_no=dn.get("dm_road_no"), building_no=dn.get("dm_building_no"),
		shipping_address=dn.get("shipping_address"), address_display=dn.get("address_display")))


def _check_out_scans(lines, scans: dict, warehouse: str) -> dict[str, list[str]]:
	errors, seen, out = [], set(), {}
	for line in lines:
		if not line.has_serial_no:
			continue
		sns = list(dict.fromkeys(s.strip() for s in scans.get(line.key) or [] if s and s.strip()))
		if len(sns) != cint(line.qty):
			errors.append(_("{0}: scan {1} serial(s), {2} scanned.").format(line.item_code, cint(line.qty), len(sns)))
		out[line.key] = sns
	chosen = [(line.item_code, sn) for line in lines for sn in out.get(line.key, [])]
	records = {r.name: r for r in frappe.get_all("Serial No", filters={"name": ["in", [sn for _c, sn in chosen] or [""]]},
		fields=["name", "item_code", "warehouse", "status"])}
	for code, sn in chosen:
		r = records.get(sn)
		if sn in seen:
			errors.append(_("{0} is scanned twice.").format(sn))
		elif not r:
			errors.append(_("Serial {0} does not exist.").format(sn))
		elif r.item_code != code:
			errors.append(_("Serial {0} belongs to {1}, not {2}.").format(sn, r.item_code, code))
		elif r.warehouse != warehouse or r.status != "Active":
			errors.append(_("Serial {0} is not in {1}.").format(sn, warehouse))
		seen.add(sn)
	if errors:
		frappe.throw("<br>".join(errors), title=_("Scan the Serial Numbers"))
	return out


# ---------------------------------------------------------------------------
# At the customer
# ---------------------------------------------------------------------------


def take_off_note(delivery_note: str, lines: list) -> list[str]:
	"""The driver takes off the note what the customer does not get. ``lines``:
	``[{dn_detail, qty, serials}]`` (qty in the row's UOM; ``serials`` the units coming back,
	a bundle's components included). The goods stay in the transit store until the stock
	keeper receives them; the order no longer delivers them and a Credit Note is made.
	Returns the Credit Notes."""
	from delivery_management.returns.service import cancel_not_installed

	dn = frappe.get_doc(DN, delivery_note, for_update=True)
	if not (dn.docstatus == 0 and dn.get("dm_delivery_status") == ON_THE_WAY):
		frappe.throw(_("Only goods of an order On the way can be taken off its note."))
	rows = {r.name: r for r in dn.items}
	by_line = {line.key: line for line in stock_lines(dn)}
	serials: dict[str, list[str]] = {k: list(line.serials) for k, line in by_line.items()}
	taken, logged = [], []
	for req in lines or []:
		req = frappe._dict(req)
		row = rows.get(req.dn_detail)
		qty = flt(req.qty)
		if not row or qty <= 0:
			continue
		if qty > flt(row.qty) + EPSILON:
			frappe.throw(_("{0}: only {1} on the note.").format(row.item_code, flt(row.qty)))
		back = set(req.serials or [])
		share = qty / flt(row.qty)
		for line in (line for line in by_line.values() if line.dn_detail == row.name and line.has_serial_no):
			need = cint(round(line.qty * share))
			mine = [sn for sn in line.serials if sn in back]
			if len(mine) != need:
				frappe.throw(_("{0}: choose the {1} serial(s) of {2} that come back ({3} chosen).").format(
					row.item_code, need, line.item_code, len(mine)), title=_("Serial numbers"))
			serials[line.key] = [sn for sn in line.serials if sn not in back]
		row.qty = flt(row.qty) - qty
		logged.append({"item_name": row.item_name, "qty": qty, "serials": sorted(back)})
		taken.append(frappe._dict(so_detail=row.get("so_detail"), item_code=row.item_code, qty=qty,
			conversion_factor=flt(row.conversion_factor) or 1))
	if not taken:
		frappe.throw(_("Choose what comes back."))
	if not any(flt(r.qty) > EPSILON for r in dn.items):
		frappe.throw(_("Nothing would be left to deliver: use Customer Not Found to bring the whole order back."),
			title=_("Nothing left"))
	dn.set("items", [r for r in dn.items if flt(r.qty) > EPSILON])
	for i, r in enumerate(dn.items, 1):
		r.idx = i
		r.stock_qty = flt(r.qty) * (flt(r.conversion_factor) or 1)
	if dn.dm_delivery_type == asg.TYPE_AC:
		dn.dm_ac_units = profile_cart([{"item_code": r.item_code, "qty": r.qty} for r in dn.items]).ac_units
	_set_serials(dn, serials)
	_save(dn)
	credit_notes = cancel_not_installed(dn, [t for t in taken if t.so_detail])
	log.add("Not delivered items", delivery_note=dn.name, driver=dn.dm_driver, items=logged,
		reference=("Sales Invoice", credit_notes[0]) if credit_notes else None)
	return credit_notes


def _set_serials(dn, serials: dict[str, list[str]]) -> None:
	for r in dn.items:
		if key(r.name, r.item_code) in serials:
			r.serial_no = "\n".join(serials[key(r.name, r.item_code)]) or None
	for p in dn.get("packed_items") or []:
		if key(p.parent_detail_docname, p.item_code) in serials:
			p.serial_no = "\n".join(serials[key(p.parent_detail_docname, p.item_code)]) or None


def bring_back_all(delivery_note: str) -> None:
	"""Nothing can be delivered (customer not found, refused...): the whole note comes back
	to the store. It stays a draft, nothing is credited; the order still has to deliver it."""
	dn = frappe.get_doc(DN, delivery_note, for_update=True)
	if not (dn.docstatus == 0 and dn.get("dm_delivery_status") == ON_THE_WAY):
		frappe.throw(_("Only an order On the way can come back."))
	dn.db_set("dm_delivery_status", RETURNED)
	frappe.db.set_value(TRANSIT, dn.name, "status", COMING_BACK)
	log.add("Customer not available", delivery_note=dn.name, driver=dn.dm_driver,
		note=_("Whole order coming back to the store, to be sent again"))


def before_delivery(dn, method=None):
	"""Delivery Note ``before_submit``: submitting the note delivers it, so it is submitted
	only while its goods are out with the driver (sent from the Stock Keeper page)."""
	if cint(dn.get("is_return")):
		return
	if not (dn.get("dm_delivery_status") == ON_THE_WAY and frappe.db.exists(TRANSIT, dn.name)):
		frappe.throw(_("Send the goods of {0} out from the Stock Keeper page first; the driver submits the note "
			"when they are delivered.").format(dn.name), title=_("Not sent out yet"))
	dn.dm_delivery_status = DELIVERED


def after_delivery(dn, method=None):
	"""Delivery Note ``on_submit``: the Delivery Transit keeps only what is coming back."""
	if cint(dn.get("is_return")) or not frappe.db.exists(TRANSIT, dn.name):
		return
	doc = frappe.get_doc(TRANSIT, dn.name)
	on_note = {line.key: line for line in stock_lines(dn)}
	for row in doc.items:
		line = on_note.get(key(row.dn_detail, row.item_code))
		row.delivered_qty = line.qty if line else 0
		row.delivered_serials = "\n".join(line.serials) if line and line.serials else None
	doc.status = COMING_BACK
	_keep_or_remove(doc, dn)
	reserve_rest(dn, stores()[0])


def check_locked(dn, method=None):
	"""Delivery Note ``validate``: a note whose goods are out changes only through the Delivery
	Schedule and Stock Keeper pages."""
	if dn.is_new() or dn.flags.get("dm_transit") or not is_out(dn.get_doc_before_save() or dn):
		return
	frappe.throw(_("The goods of {0} are out of the store ({1}): it changes only from the Delivery Schedule "
		"and Stock Keeper pages.").format(dn.name, _(dn.dm_delivery_status)), title=_("Goods out"))


def set_driver(delivery_note: str, driver: str | None) -> bool:
	"""The note changed driver while its goods are out (the drivers swap them outside the
	store). A whole order coming back goes to the new driver instead: On the way again.
	Returns whether it was coming back."""
	if not frappe.db.exists(TRANSIT, delivery_note):
		return False
	values = {"driver": driver, "driver_name": get_fullname(driver) if driver else None}
	coming_back = frappe.db.get_value(DN, delivery_note, ["docstatus", "dm_delivery_status"]) == (0, RETURNED)
	if coming_back:
		if any(flt(r.received_qty) for r in frappe.get_doc(TRANSIT, delivery_note).items):
			frappe.throw(_("Part of {0} is already back in the store: receive the rest on the Stock Keeper page, "
				"then send it out with the new driver.").format(delivery_note), title=_("Partly back in the store"))
		frappe.db.set_value(DN, delivery_note, "dm_delivery_status", ON_THE_WAY)
		values["status"] = ON_THE_WAY
	frappe.db.set_value(TRANSIT, delivery_note, values)
	return coming_back


# ---------------------------------------------------------------------------
# Back in the store
# ---------------------------------------------------------------------------


def pending(doc, dn=None) -> dict[str, frappe._dict]:
	"""``{Delivery Transit row: {qty, serials}}`` still outside the store and not with the
	customer: what the stock keeper can receive."""
	dn = dn or frappe.get_doc(DN, doc.delivery_note)
	on_note: dict = {}
	if dn.docstatus == 1:
		on_note = {key(r.dn_detail, r.item_code): (flt(r.delivered_qty), get_serial_nos(r.delivered_serials or ""))
			for r in doc.items}
	elif dn.get("dm_delivery_status") == ON_THE_WAY:
		on_note = {line.key: (line.qty, line.serials) for line in stock_lines(dn)}
	out = {}
	for r in doc.items:
		qty_on, sns_on = on_note.get(key(r.dn_detail, r.item_code), (0, []))
		qty = flt(r.qty) - qty_on - flt(r.received_qty)
		if qty > EPSILON:
			gone = set(sns_on) | set(get_serial_nos(r.received_serials or ""))
			out[r.name] = frappe._dict(qty=qty, serials=[s for s in get_serial_nos(r.serial_no or "") if s not in gone])
	return out


def receive(transit: str, scans: dict, reserve_again: bool = False) -> str:
	"""The stock keeper receives goods back into the main store. ``scans``: ``{Delivery Transit
	row: [serials]}`` for serialised items, ``{row: qty}`` for the others. When the whole
	note came back, it is ready to be sent again, its goods reserved for the order again
	if ``reserve_again``. Returns the Stock Entry."""
	main, transit_store = stores()
	doc = frappe.get_doc(TRANSIT, transit, for_update=True)
	dn = frappe.get_doc(DN, doc.delivery_note, for_update=True)
	waiting = pending(doc, dn)
	rows = {r.name: r for r in doc.items}
	lines, errors = [], []
	for name, value in (scans or {}).items():
		row, left = rows.get(name), waiting.get(name)
		if not row or not left:
			continue
		if isinstance(value, list):
			sns = list(dict.fromkeys(s.strip() for s in value if s and s.strip()))
			foreign = [s for s in sns if s not in left.serials]
			if foreign:
				errors.append(_("{0}: serial {1} is not coming back on {2}.").format(row.item_code, ", ".join(foreign),
					doc.name))
			qty = len(sns)
		else:
			sns, qty = [], flt(value)
			if left.serials and qty:
				errors.append(_("{0}: scan the serials.").format(row.item_code))
		if qty > left.qty + EPSILON:
			errors.append(_("{0}: only {1} is coming back.").format(row.item_code, left.qty))
		if qty > EPSILON:
			lines.append(frappe._dict(row=row, item_code=row.item_code, stock_uom=row.stock_uom, qty=qty, serials=sns))
	if errors:
		frappe.throw("<br>".join(errors), title=_("Receive"))
	if not lines:
		frappe.throw(_("Scan what came back."))

	entry = _transfer(dn, lines, transit_store, main, _("{0}: back from {1}").format(dn.name, doc.driver_name or ""))
	for line in lines:
		line.row.received_qty = flt(line.row.received_qty) + line.qty
		line.row.received_serials = "\n".join(get_serial_nos(line.row.received_serials or "") + line.serials) or None
	doc.back_entries = "\n".join(filter(None, [doc.back_entries, entry]))
	whole = dn.docstatus == 0 and dn.get("dm_delivery_status") == RETURNED and not pending(doc, dn)
	if whole:
		_back_in_store(dn, main, reserve_again)
	log.add("Received back", delivery_note=dn.name, driver=doc.driver, reference=("Stock Entry", entry),
		items=[{"item_name": line.row.item_name, "qty": line.qty, "serials": line.serials} for line in lines],
		note=(_("Reserved again for the order") if reserve_again else _("Not reserved")) if whole else None)
	_keep_or_remove(doc, dn)
	return entry


def _back_in_store(dn, main: str, reserve_again: bool) -> None:
	"""The whole note is back: ready to be sent again from the main store."""
	_set_rows(dn, main, None)
	dn.dm_delivery_status = None
	_save(dn)
	if reserve_again:
		reserve_rest(dn, main)


def _keep_or_remove(doc, dn) -> None:
	"""Saved while something of it is outside the store, removed once all is delivered or back."""
	if (dn.docstatus == 0 and dn.get("dm_delivery_status") == ON_THE_WAY) or pending(doc, dn):
		doc.save(ignore_permissions=True)
	else:
		frappe.delete_doc(TRANSIT, doc.name, ignore_permissions=True, force=True)
