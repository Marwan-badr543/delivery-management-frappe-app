"""Stock reservation for the stock items inside a Product Bundle.

ERPNext reserves stock only for Sales Order item rows of stock items, so the components of a
bundle (its Packed Items) are never reserved. This module does it the ERPNext way:

* **Sales Order submit:** one Stock Reservation Entry per component, against the bundle's
  order row (``voucher_detail_no``). ERPNext picks the serials (FIFO, skipping serials other
  entries hold) and adds the qty to ``Bin.reserved_stock``, so the POS counts it as gone.
* **Delivery Note validate:** ERPNext's serial picking does not know packed rows belong to the
  order, so the reserved serials are written into the packed rows.
* **Delivery Note submit / cancel:** the entries are marked delivered / reserved again. This
  runs *before* ERPNext posts the stock ledger (which refuses to take reserved stock) and
  before ERPNext updates entries of the bundle row (which expects the row to have serials).
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


def reserve(so, method=None):
	"""Sales Order ``on_submit``: reserve the stock components of its bundles."""
	if not so.get("reserve_stock") or not so.get("packed_items") or not _enabled():
		return
	items = _stock_items(r.item_code for r in so.packed_items)
	needed = defaultdict(float)  # (order row, item, warehouse) -> qty
	for row in so.packed_items:
		if row.item_code in items and row.warehouse:
			needed[(row.parent_detail_docname, row.item_code, row.warehouse)] += flt(row.qty)
	partial = cint(frappe.get_single_value("Stock Settings", "allow_partial_reservation"))
	for (so_detail, item_code, warehouse), qty in needed.items():
		available = get_available_qty_to_reserve(item_code, warehouse)
		if available < qty and not partial:
			continue
		qty_to_reserve = min(qty, available)
		if qty_to_reserve <= 0:
			continue
		item = items[item_code]
		sre = frappe.get_doc({
			"doctype": SRE,
			"item_code": item_code,
			"warehouse": warehouse,
			"has_serial_no": item.has_serial_no,
			"has_batch_no": item.has_batch_no,
			"voucher_type": so.doctype,
			"voucher_no": so.name,
			"voucher_detail_no": so_detail,
			"available_qty": available,
			"voucher_qty": qty,
			"reserved_qty": qty_to_reserve,
			"company": so.company,
			"stock_uom": item.stock_uom,
			"project": so.get("project"),
			"reservation_based_on": "Serial and Batch" if item.has_serial_no or item.has_batch_no else "Qty",
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
	"""Delivery Note ``validate``: put the order's reserved serials into empty packed rows."""
	if dn.docstatus != 0 or cint(dn.get("is_return")) or not dn.get("packed_items"):
		return
	links = _order_rows(dn)
	taken = {sn for r in dn.packed_items for sn in get_serial_nos(r.serial_no or "")}
	for row in dn.packed_items:
		link = links.get(row.parent_detail_docname)
		if not link or row.serial_no or row.serial_and_batch_bundle:
			continue
		free = [e.serial_no for sre in _entries(*link, row.item_code, row.warehouse)
			if sre.has_serial_no for e in sre.sb_entries
			if e.serial_no and not flt(e.delivered_qty) and e.serial_no not in taken]
		picked = free[: cint(row.qty)]
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
