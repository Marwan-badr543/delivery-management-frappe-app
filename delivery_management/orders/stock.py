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
	bins = {b.item_code: max(flt(b.actual_qty) - flt(b.reserved_stock), 0) for b in frappe.get_all("Bin",
		filters={"item_code": ["in", item_codes], "warehouse": warehouse}, fields=["item_code", "actual_qty", "reserved_stock"])}
	return {code: bins.get(code, 0) if code in stock_items else None for code in item_codes}


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
