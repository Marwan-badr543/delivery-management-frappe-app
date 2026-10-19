"""Serials of the stock that leaves: chosen at the POS for an Invoice, scanned on the
Delivery Note of an Order (whose reservation holds only the quantity).

A cart row brings its own serials in ``serial_no``; a bundle line brings the serials of
its components in ``dm_component_serials`` (``{component item: [serials]}``). Each chosen
serial must belong to the item, be in the sale's warehouse and not be reserved for
another order; a serial is used once per cart. When nothing is chosen, ERPNext picks the
oldest free serials.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_sre_reserved_serial_nos_details,
)

COMPONENT_SERIALS = "dm_component_serials"


def _component_serials(row) -> dict:
	value = row.get(COMPONENT_SERIALS) or {}
	if isinstance(value, str):
		value = json.loads(value or "{}")
	return {code: get_serial_nos("\n".join(sns) if isinstance(sns, list) else sns or "") for code, sns in value.items()}


def _needs(items) -> list[tuple[str, str, int, list[str]]]:
	"""``(row id, item, stock qty, chosen serials)`` for every serialised item of the cart,
	bundle components included."""
	codes = {r.get("item_code") for r in items}
	bundles: dict[str, list] = {}
	for b in frappe.get_all("Product Bundle Item", filters={"parent": ["in", list(codes) or [""]],
			"parenttype": "Product Bundle"}, fields=["parent", "item_code", "qty"], order_by="idx asc"):
		bundles.setdefault(b.parent, []).append((b.item_code, flt(b.qty)))
	all_codes = codes | {c for parts in bundles.values() for c, _q in parts}
	serialised = set(frappe.get_all("Item", filters={"name": ["in", list(all_codes) or [""]], "has_serial_no": 1},
		pluck="name"))

	needs = []
	for row in items:
		code = row.get("item_code")
		stock_qty = flt(row.get("qty")) * (flt(row.get("conversion_factor")) or 1)
		if code in bundles:
			chosen = _component_serials(row)
			needs += [(row.get("posa_row_id"), c, cint(per * stock_qty), chosen.get(c, []))
				for c, per in bundles[code] if c in serialised]
		elif code in serialised:
			needs.append((row.get("posa_row_id"), code, cint(stock_qty), get_serial_nos(row.get("serial_no") or "")))
	return needs


def _check(needs, warehouse) -> None:
	chosen = [(code, sn) for _row, code, _qty, sns in needs for sn in sns]
	if not chosen:
		return
	errors = []
	seen = set()
	for code, sn in chosen:
		if sn in seen:
			errors.append(_("{0} is chosen twice.").format(sn))
		seen.add(sn)
	for _row, code, qty, sns in needs:
		if len(sns) > qty:
			errors.append(_("{0}: {1} serials chosen for a quantity of {2}.").format(code, len(sns), qty))
	records = {r.name: r for r in frappe.get_all("Serial No", filters={"name": ["in", list(seen)]},
		fields=["name", "item_code", "warehouse", "status"])}
	for code, sn in chosen:
		r = records.get(sn)
		if not r:
			errors.append(_("Serial {0} does not exist.").format(sn))
		elif r.item_code != code:
			errors.append(_("Serial {0} belongs to {1}, not {2}.").format(sn, r.item_code, code))
		elif r.warehouse != warehouse or r.status != "Active":
			errors.append(_("Serial {0} is not available in {1}.").format(sn, warehouse))
	by_item: dict[str, list] = {}
	for code, sn in chosen:
		by_item.setdefault(code, []).append(sn)
	for code, sns in by_item.items():
		for sn, sre in get_sre_reserved_serial_nos_details(code, warehouse, sns).items():
			errors.append(_("Serial {0} is reserved for another order ({1}).").format(sn, sre))
	if errors:
		frappe.throw("<br>".join(errors), title=_("Serial Numbers"))


def free_serials(item_code, warehouse, exclude=()) -> list[str]:
	"""Serials of the item in the warehouse that nobody holds, oldest first."""
	reserved = set(get_sre_reserved_serial_nos_details(item_code, warehouse))
	return [sn for sn in frappe.get_all("Serial No", filters={"item_code": item_code, "warehouse": warehouse,
			"status": "Active"}, order_by="creation asc, name asc", pluck="name")
		if sn not in reserved and sn not in exclude]


def _free(item_code, warehouse, count, exclude) -> list[str]:
	"""The ``count`` oldest free serials (see :func:`free_serials`)."""
	free = free_serials(item_code, warehouse, exclude)
	if len(free) < count:
		frappe.throw(_("Not enough free serials of {0} in {1}.").format(item_code, warehouse))
	return free[:count]


def plan(items, warehouse) -> dict[str, dict[str, list[str]]]:
	"""Check the chosen serials of an Invoice cart. When any is chosen, returns
	``{row id: {item: serials}}`` with a full list for every serialised item of the cart
	(missing serials are the oldest free ones), so nothing is left to pick later."""
	needs = _needs(items)
	_check(needs, warehouse)
	if not any(sns for *_x, sns in needs):
		return {}
	taken = {sn for *_x, sns in needs for sn in sns}
	out: dict[str, dict] = {}
	for row_id, code, qty, sns in needs:
		if len(sns) < qty:
			extra = _free(code, warehouse, qty - len(sns), taken)
			taken.update(extra)
			sns = sns + extra
		if sns:
			out.setdefault(row_id, {}).setdefault(code, []).extend(sns)
	return out


def apply_to_packed_items(doc, method=None):
	"""Sales Invoice ``validate``: put the chosen component serials of a POS sale on the
	packing list (ERPNext builds it from the bundle lines during validate)."""
	chosen = frappe.flags.get("dm_bundle_serials")
	if not chosen or doc.docstatus == 2 or not doc.get("packed_items"):
		return
	row_ids = {r.name: r.get("posa_row_id") for r in doc.items}
	for packed in doc.packed_items:
		serials = list((chosen.get(row_ids.get(packed.parent_detail_docname)) or {}).get(packed.item_code) or [])
		if serials:
			packed.serial_no = "\n".join(serials[: cint(packed.qty)])
			packed.use_serial_batch_fields = 1
			packed.serial_and_batch_bundle = None


def _order_rows(dn) -> list:
	"""Item and packed rows of a Delivery Note made from an order (returns excluded)."""
	if cint(dn.get("is_return")):
		return []
	from_order = {r.name for r in dn.items if r.get("against_sales_order")}
	return [r for r in dn.items if r.name in from_order] + [p for p in dn.get("packed_items") or []
		if p.parent_detail_docname in from_order]


def _key(row) -> tuple:
	# Packed rows are rebuilt during validate, so they are known by their bundle row and item.
	return (row.parent_detail_docname, row.item_code) if row.doctype == "Packed Item" else (row.name,)


def clear_order_serials(dn, method=None):
	"""Delivery Note ``before_insert``: a note made from an order starts without serials.
	ERPNext fills in the oldest free ones, which are not the boxes the stock keeper takes."""
	for row in _order_rows(dn):
		row.serial_no = None
		row.serial_and_batch_bundle = None


def keep_scanned(dn, method=None):
	"""Delivery Note ``before_validate``: note what the stock keeper scanned (ERPNext's
	validate fills empty serials again), and on submit require every serial."""
	rows = _order_rows(dn)
	dn.flags.dm_scanned = {_key(r): (r.serial_no, r.serial_and_batch_bundle) for r in rows}
	if dn.get("_action") != "submit":
		return
	serialised = set(frappe.get_all("Item", filters={"name": ["in", [r.item_code for r in rows] or [""]],
		"has_serial_no": 1}, pluck="name"))
	missing = [_("Row {0}: {1} needs {2} serials, {3} scanned.").format(r.idx, r.item_code, cint(qty), count)
		for r in rows if r.item_code in serialised
		for qty in [flt(r.get("stock_qty") or r.qty)]
		for count in [len(_scanned(r))] if count != cint(qty)]
	if missing:
		frappe.throw("<br>".join(missing), title=_("Scan the Serial Numbers"))


def restore_scanned(dn, method=None):
	"""Delivery Note ``validate``: drop the serials ERPNext picked, keep the scanned ones."""
	scanned = dn.flags.get("dm_scanned") or {}
	for row in _order_rows(dn):
		row.serial_no, row.serial_and_batch_bundle = scanned.get(_key(row), (None, None))


def _scanned(row) -> list[str]:
	if row.get("serial_and_batch_bundle"):
		return frappe.get_all("Serial and Batch Entry", filters={"parent": row.serial_and_batch_bundle},
			pluck="serial_no")
	return get_serial_nos(row.serial_no or "")
