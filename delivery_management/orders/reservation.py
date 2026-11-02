"""Stock reservation of a Sales Order, done the ERPNext way (Stock Reservation Entries).

Reservations hold a quantity, never a serial (Stock Settings "Auto Reserve Serial and
Batch Nos" is off): the stock keeper scans the serials of the boxes that actually leave on
the Delivery Note (see :mod:`serial_choice`).

ERPNext reserves only the order's own stock lines. This module adds the stock items inside
a Product Bundle (its Packed Items), reserved against the bundle's order row:

* **Sales Order submit:** the components are reserved.
* **Delivery Note submit / cancel:** their entries are marked delivered / reserved again,
  *before* ERPNext posts the stock ledger (which refuses to take reserved stock).
* **Sales Order cancel:** ERPNext cancels every entry of the order, these included.
* **Sales Order closed** (``settle_order``, or Close on the form / list): ERPNext keeps the
  entries, so stock no one will deliver stays reserved for good. What they still hold is
  released (``release_undelivered``).
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint, flt

from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_available_qty_to_reserve,
)

SRE = "Stock Reservation Entry"


def _enabled() -> bool:
	return bool(frappe.get_single_value("Stock Settings", "enable_stock_reservation"))


def _stock_items(item_codes) -> dict:
	"""``{item_code: {has_serial_no, has_batch_no, stock_uom}}`` of the stock items."""
	rows = frappe.get_all("Item", filters={"name": ["in", list(set(item_codes)) or [""]], "is_stock_item": 1},
		fields=["name", "has_serial_no", "has_batch_no", "stock_uom"])
	return {r.name: r for r in rows}


def _lines(so) -> tuple[dict, dict]:
	"""``{(order row, item, warehouse): qty}`` of the bundle components, and their stock items."""
	rows = [(p.parent_detail_docname, p.item_code, p.warehouse, flt(p.qty)) for p in so.get("packed_items") or []]
	items = _stock_items(code for _r, code, _w, _q in rows)
	lines: dict = defaultdict(float)
	for so_detail, code, warehouse, qty in rows:
		if code in items and warehouse:
			lines[(so_detail, code, warehouse)] += qty
	return lines, items


def reserve(so, method=None):
	"""Sales Order ``on_submit``: reserve the bundle components."""
	if not so.get("reserve_stock") or not _enabled():
		return
	lines, items = _lines(so)
	partial = cint(frappe.get_single_value("Stock Settings", "allow_partial_reservation"))
	for (so_detail, code, warehouse), qty in lines.items():
		available = get_available_qty_to_reserve(code, warehouse)
		if available < qty and not partial:
			continue
		if min(qty, available) > 0:
			_make_entry(so, so_detail, items[code], warehouse, qty, min(qty, available))


def _make_entry(so, so_detail, item, warehouse, voucher_qty, qty) -> None:
	sre = frappe.get_doc({
		"doctype": SRE,
		"item_code": item.name,
		"warehouse": warehouse,
		"has_serial_no": item.has_serial_no,
		"has_batch_no": item.has_batch_no,
		"voucher_type": so.doctype,
		"voucher_no": so.name,
		"voucher_detail_no": so_detail,
		"available_qty": get_available_qty_to_reserve(item.name, warehouse),
		"voucher_qty": voucher_qty,
		"reserved_qty": qty,
		"company": so.company,
		"stock_uom": item.stock_uom,
		"project": so.get("project"),
		"reservation_based_on": "Qty",
	})
	sre.flags.ignore_permissions = True
	sre.insert()
	sre.submit()


def _order_rows(dn) -> dict:
	"""``{DN item row name: (sales order, order row)}`` for rows made from an order."""
	return {r.name: (r.against_sales_order, r.so_detail) for r in dn.items
		if r.get("against_sales_order") and r.get("so_detail")}


def _entries(sales_order, so_detail, item_code, warehouse=None, delivered=False) -> list:
	filters = {"docstatus": 1, "voucher_type": "Sales Order", "voucher_no": sales_order,
		"voucher_detail_no": so_detail, "item_code": item_code}
	if warehouse:
		filters["warehouse"] = warehouse
	names = frappe.get_all(SRE, filters=filters, order_by="creation asc", pluck="name")
	docs = [frappe.get_doc(SRE, n) for n in names]
	if delivered:
		return [d for d in docs if flt(d.delivered_qty) > 0]
	return [d for d in docs if flt(d.delivered_qty) < flt(d.reserved_qty)]


def _update_delivered(dn, deliver: bool) -> None:
	if cint(dn.get("is_return")):
		return
	links = _order_rows(dn)
	for row in dn.get("packed_items") or []:
		link = links.get(row.parent_detail_docname)
		if not link:
			continue
		qty = flt(row.qty)
		for sre in _entries(*link, row.item_code, row.warehouse, delivered=not deliver):
			if qty <= 0:
				break
			moved = min(flt(sre.reserved_qty) - flt(sre.delivered_qty) if deliver else flt(sre.delivered_qty), qty)
			sre.delivered_qty = flt(sre.delivered_qty) + (moved if deliver else -moved)
			sre.db_update()
			sre.update_reserved_qty_in_voucher()
			sre.update_status()
			sre.update_reserved_stock_in_bin()
			qty -= moved


def deliver(dn, method=None):
	"""Delivery Note ``before_submit``: the reserved components are delivered."""
	_update_delivered(dn, deliver=True)


def undeliver(dn, method=None):
	"""Delivery Note ``before_cancel``: the components are reserved for the order again."""
	_update_delivered(dn, deliver=False)


def release(sre, qty: float) -> None:
	"""Stop ``sre`` holding ``qty`` (stock UOM): cancelled when nothing of it was delivered and
	nothing stays held, otherwise its reserved quantity goes down."""
	sre.flags.ignore_permissions = True
	if not flt(sre.delivered_qty) and flt(sre.reserved_qty) - qty <= 1e-6:
		sre.cancel()
		return
	sre.reserved_qty = flt(sre.reserved_qty) - qty
	sre.db_update()
	sre.update_reserved_qty_in_voucher()
	sre.update_status()
	sre.update_reserved_stock_in_bin()


def release_undelivered(sales_order: str) -> None:
	"""A closed order delivers nothing more: free what its entries still hold."""
	for name in frappe.get_all(SRE, filters={"voucher_type": "Sales Order", "voucher_no": sales_order,
			"docstatus": 1}, pluck="name"):
		sre = frappe.get_doc(SRE, name)
		held = flt(sre.reserved_qty) - flt(sre.delivered_qty)
		if held > 1e-6:
			release(sre, held)


def reserve_again(sales_order: str, warehouse: str, lines: dict) -> None:
	"""Goods of the order that came back to ``warehouse`` are held for it again. ``lines``:
	``{(order row, item, is bundle component): stock qty}``."""
	if not sales_order or not _enabled():
		return
	so = frappe.get_doc("Sales Order", sales_order)
	plain = [{"sales_order_item": so_detail, "warehouse": warehouse, "qty_to_reserve": qty, "conversion_factor": 1}
		for (so_detail, _code, component), qty in lines.items() if not component and qty > 0]
	if plain:
		so._create_stock_reservation_entries(plain, notify=False)
	items = _stock_items(code for (_d, code, component) in lines if component)
	ordered, _items = _lines(so)
	for (so_detail, code, component), qty in lines.items():
		if component and code in items and qty > 0:
			_make_entry(so, so_detail, items[code], warehouse, ordered.get((so_detail, code, warehouse), qty), qty)
