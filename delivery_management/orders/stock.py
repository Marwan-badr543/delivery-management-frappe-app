"""Stock the POS can sell: Invoice from the branch warehouse, Order from the main store.

Serials are left to ERPNext (Stock Reservation reserves them for the Sales Order and
picks them on outward documents).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from delivery_management.slots.cart import component_rows, stock_qty


def get_sellable_qty(item_codes, warehouse: str) -> dict[str, float | None]:
	"""Quantity that can still be sold from ``warehouse``: ``actual_qty - reserved_stock``
	(Stock Reservation). Non-stock items: ``None`` (no limit)."""
	item_codes = list({c for c in item_codes if c})
	if not item_codes or not warehouse:
		return {}
	stock_items = set(frappe.get_all("Item", filters={"name": ["in", item_codes], "is_stock_item": 1}, pluck="name"))
	held = held_for_split_notes(item_codes, warehouse)
	bins = {b.item_code: max(flt(b.actual_qty) - flt(b.reserved_stock) - held.get(b.item_code, 0), 0)
		for b in frappe.get_all("Bin", filters={"item_code": ["in", item_codes], "warehouse": warehouse},
			fields=["item_code", "actual_qty", "reserved_stock"])}
	return {code: bins.get(code, 0) if code in stock_items else None for code in item_codes}


# A draft note still in the store whose order line has another note out with a driver: ERPNext
# keeps no reservation on that line while part of it is out (transit.service.dispatch), so the
# POS holds the waiting part itself. Bundles are held by their components (packed items).
_SPLIT_WAITING = """dn.docstatus = 0 and dn.is_return = 0 and ifnull(dn.dm_delivery_status, '') = ''
	and exists (select 1 from `tabDelivery Note Item` o join `tabDelivery Note` od on od.name = o.parent
		where o.so_detail = dni.so_detail and od.docstatus = 0 and od.is_return = 0
		and ifnull(od.dm_delivery_status, '') != '')"""


def held_for_split_notes(item_codes, warehouse: str) -> dict[str, float]:
	"""``{item: stock qty}`` waiting in ``warehouse`` on such notes."""
	rows = frappe.db.sql(
		f"""select dni.item_code, sum(dni.stock_qty) as qty from `tabDelivery Note Item` dni
			join `tabDelivery Note` dn on dn.name = dni.parent
			where {_SPLIT_WAITING} and dni.warehouse = %(warehouse)s and dni.item_code in %(codes)s
			and not exists (select 1 from `tabPacked Item` p where p.parent = dn.name and p.parent_detail_docname = dni.name)
			group by dni.item_code
		union all
		select p.item_code, sum(p.qty) as qty from `tabPacked Item` p
			join `tabDelivery Note Item` dni on dni.name = p.parent_detail_docname
			join `tabDelivery Note` dn on dn.name = dni.parent
			where p.parenttype = 'Delivery Note' and {_SPLIT_WAITING} and p.warehouse = %(warehouse)s
			and p.item_code in %(codes)s
			group by p.item_code""",
		{"warehouse": warehouse, "codes": tuple(item_codes)},
		as_dict=True,
	)
	held: dict[str, float] = {}
	for r in rows:
		held[r.item_code] = held.get(r.item_code, 0) + flt(r.qty)
	return held


def assert_in_stock(rows, warehouse: str) -> None:
	"""Server-side twin of the POS out-of-stock check (never trust the UI).
	A Product Bundle is checked by its components."""
	needed: dict[str, float] = {}
	for row in component_rows(rows):
		qty = stock_qty(row)
		needed[row.get("item_code")] = needed.get(row.get("item_code"), 0) + flt(qty)

	available = get_sellable_qty(needed.keys(), warehouse)
	short = [
		_("{0}: {1} available, {2} required").format(code, available.get(code) or 0, qty)
		for code, qty in needed.items()
		if available.get(code) is not None and flt(available[code]) < qty
	]
	if short:
		frappe.throw(
			_("Out of stock in {0}:").format(frappe.bold(warehouse)) + "<br>" + "<br>".join(short),
			title=_("Out of Stock"),
		)
