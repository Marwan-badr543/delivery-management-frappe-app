"""Stock reservation of a Sales Order, done the ERPNext way (Stock Reservation Entries).

ERPNext reserves only the order's own stock lines, and only the oldest free serials. This
module adds what it cannot do:

* **Bundles:** the stock items inside a Product Bundle (its Packed Items) are reserved
  against the bundle's order row.
* **Chosen serials:** an order placed at the POS is reserved here, line by line
  (ERPNext's own reservation is switched off on its rows): the serials the cashier chose
  first, then the rest, which ERPNext picks (oldest first, skipping held serials).
* **Delivery Note validate:** the reserved serials are written into the note's empty
  lines (item rows and packed rows), so exactly those serials leave the store.
* **Delivery Note submit / cancel:** the bundle entries are marked delivered / reserved
  again, *before* ERPNext posts the stock ledger (which refuses to take reserved stock)
  and before ERPNext updates entries of the bundle row (which expects the row to have
  serials). Item rows are updated by ERPNext itself.
* **Sales Order cancel:** ERPNext cancels every entry of the order, these included.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint, flt

from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
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
	"""``{(order row, item, warehouse): [qty, chosen serials]}`` of what this module reserves,
	and the stock items involved."""
	chosen = so.flags.get("dm_chosen_serials")
	row_ids = {r.name: r.get("posa_row_id") for r in so.items}
	rows = [(r.name, r.item_code, r.warehouse, flt(r.stock_qty)) for r in so.items] if chosen is not None else []
	rows += [(p.parent_detail_docname, p.item_code, p.warehouse, flt(p.qty)) for p in so.get("packed_items") or []]
	items = _stock_items(code for _r, code, _w, _q in rows)
	lines: dict = {}
	for so_detail, code, warehouse, qty in rows:
		if code not in items or not warehouse:
			continue
		line = lines.setdefault((so_detail, code, warehouse), [0.0, []])
		line[0] += qty
		line[1] = ((chosen or {}).get(row_ids.get(so_detail)) or {}).get(code) or line[1]
	return lines, items


def reserve(so, method=None):
	"""Sales Order ``on_submit``: reserve the bundle components, and every line of a POS order."""
	if not so.get("reserve_stock") or not _enabled():
		return
	lines, items = _lines(so)
	partial = cint(frappe.get_single_value("Stock Settings", "allow_partial_reservation"))
	# Chosen serials first, so the automatic picks of the other lines cannot take them.
	for (so_detail, code, warehouse), (qty, serials) in lines.items():
		if serials:
			_make_entry(so, so_detail, items[code], warehouse, qty, len(serials), serials)
	for (so_detail, code, warehouse), (qty, serials) in lines.items():
		left = qty - len(serials)
		available = get_available_qty_to_reserve(code, warehouse)
		if left <= 0 or (available < left and not partial):
			continue
		if min(left, available) > 0:
			_make_entry(so, so_detail, items[code], warehouse, qty, min(left, available))


def _make_entry(so, so_detail, item, warehouse, voucher_qty, qty, serials=None) -> None:
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
		"reservation_based_on": "Serial and Batch" if item.has_serial_no or item.has_batch_no else "Qty",
		# Given serials are reserved as they are; without them ERPNext picks (oldest free first).
		"sb_entries": [{"serial_no": sn, "qty": 1, "warehouse": warehouse} for sn in serials or []],
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


def fill_serials(dn, method=None):
	"""Delivery Note ``validate``: put the order's reserved serials into empty lines."""
	if dn.docstatus != 0 or cint(dn.get("is_return")):
		return
	links = _order_rows(dn)
	rows = [(r, links.get(r.name), r.stock_qty) for r in dn.items]
	rows += [(p, links.get(p.parent_detail_docname), p.qty) for p in dn.get("packed_items") or []]
	taken = {sn for r, _l, _q in rows for sn in get_serial_nos(r.serial_no or "")}
	for row, link, qty in rows:
		if not link or row.serial_no or row.get("serial_and_batch_bundle"):
			continue
		free = [e.serial_no for sre in _entries(*link, row.item_code, row.warehouse)
			if sre.has_serial_no for e in sre.sb_entries
			if e.serial_no and not flt(e.delivered_qty) and e.serial_no not in taken]
		picked = free[: cint(qty)]
		if picked:
			row.serial_no = "\n".join(picked)
			row.use_serial_batch_fields = 1
			taken.update(picked)


def _packed_serials(row) -> list[str]:
	if row.get("serial_and_batch_bundle"):
		return frappe.get_all("Serial and Batch Entry", filters={"parent": row.serial_and_batch_bundle},
			pluck="serial_no")
	return get_serial_nos(row.serial_no or "")


def _update_delivered(dn, deliver: bool) -> None:
	if cint(dn.get("is_return")):
		return
	links = _order_rows(dn)
	for row in dn.get("packed_items") or []:
		link = links.get(row.parent_detail_docname)
		if not link:
			continue
		qty = flt(row.qty)
		serials = _packed_serials(row)
		for sre in _entries(*link, row.item_code, row.warehouse, delivered=not deliver):
			if qty <= 0:
				break
			moved = 0
			if sre.has_serial_no:
				for entry in sre.sb_entries:
					if entry.serial_no in serials and bool(entry.delivered_qty) != deliver:
						entry.delivered_qty = 1 if deliver else 0
						entry.db_update()
						moved += 1
			else:
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
