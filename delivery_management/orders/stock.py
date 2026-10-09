"""Stock availability and serial number allocation for POS sales.

The POS no longer lets the cashier choose serials. They are picked here
(oldest first) from the warehouse that physically serves the sale:

* **Invoice** (customer takes the goods at the branch): the POS profile's branch warehouse.
* **Order** (delivery): the main store from Delivery Settings.

Business convention, shared with the ``stock_solution`` app and the production
server scripts: a serial with status ``Inactive`` that is still in a warehouse
is *reserved* for an open order and must not be sold again. An order marks its
serials Inactive as soon as they are picked; cancelling the order makes them
Active again.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

from delivery_management.slots.cart import stock_qty

MODE_INVOICE = "Invoice"
MODE_ORDER = "Order"

# A serial that can still be sold: in stock and not reserved (Inactive). In v16 a sold or
# delivered serial leaves its warehouse (status Delivered), so the warehouse filter covers that.
_PICKABLE = "status = 'Active' and ifnull(warehouse, '') != ''"


def get_sellable_qty(item_codes, warehouse: str, mode: str = MODE_INVOICE) -> dict[str, float | None]:
	"""Quantity that can still be sold from ``warehouse``, per item.

	* Serialised items: the number of pickable serials, capped by the ledger qty.
	* Other stock items: ``actual_qty``, minus open Sales Order reservations for Orders.
	* Non-stock items: ``None`` (no limit).
	"""
	item_codes = list({c for c in item_codes if c})
	if not item_codes or not warehouse:
		return {}

	items = {
		r.name: r
		for r in frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "is_stock_item", "has_serial_no"],
		)
	}
	bins = {
		r.item_code: r
		for r in frappe.get_all(
			"Bin",
			filters={"item_code": ["in", item_codes], "warehouse": warehouse},
			fields=["item_code", "actual_qty", "reserved_qty"],
		)
	}
	serial_items = [c for c, r in items.items() if cint(r.has_serial_no)]
	serial_counts = {}
	if serial_items:
		serial_counts = dict(
			frappe.db.sql(
				f"""select item_code, count(*) from `tabSerial No`
				where item_code in %(items)s and warehouse = %(wh)s and {_PICKABLE}
				group by item_code""",
				{"items": tuple(serial_items), "wh": warehouse},
			)
		)

	result = {}
	for code in item_codes:
		item = items.get(code)
		if not item or not cint(item.is_stock_item):
			result[code] = None
			continue
		b = bins.get(code) or frappe._dict(actual_qty=0, reserved_qty=0)
		qty = flt(b.actual_qty)
		if cint(item.has_serial_no):
			# Picked serials are already Inactive, so the count already excludes open orders.
			qty = min(qty, flt(serial_counts.get(code, 0)))
		elif mode == MODE_ORDER:
			qty -= flt(b.reserved_qty)
		result[code] = max(qty, 0)
	return result


def pick_serials(item_code: str, warehouse: str, qty, exclude=()) -> list[str]:
	"""Lock and return ``qty`` pickable serials, oldest first. Raises if short.

	``FOR UPDATE`` makes a concurrent sale of the same item wait for this
	transaction, and then see these serials as taken.
	"""
	raw = flt(qty)
	qty = cint(raw)
	if qty != raw or qty <= 0:
		frappe.throw(_("Quantity of serialised item {0} must be a positive whole number.").format(item_code))

	exclude = tuple(exclude) or ("",)
	serials = frappe.db.sql_list(
		f"""select name from `tabSerial No`
		where item_code = %(item)s and warehouse = %(wh)s and {_PICKABLE} and name not in %(exclude)s
		order by posting_date asc, creation asc, name asc
		limit %(qty)s for update""",
		{"item": item_code, "wh": warehouse, "exclude": exclude, "qty": qty},
	)
	if len(serials) < qty:
		frappe.throw(
			_("Not enough stock of {0} in {1}: {2} available, {3} required.").format(
				frappe.bold(item_code), frappe.bold(warehouse), len(serials), qty
			),
			title=_("Out of Stock"),
		)
	return serials


def allocate_serials(rows, warehouse: str, target_field: str = "serial_no") -> list[str]:
	"""Fill ``target_field`` on every serialised row that has none yet.

	``rows`` are child docs or dicts with ``item_code`` and ``stock_qty``/``qty``.
	Rows that already carry serials are kept as they are.
	"""
	codes = [r.get("item_code") for r in rows]
	serialised = set(
		frappe.get_all("Item", filters={"name": ["in", codes or [""]], "has_serial_no": 1}, pluck="name")
	)
	taken: list[str] = []
	for row in rows:
		if row.get("item_code") not in serialised:
			continue
		existing = get_serial_nos(row.get(target_field) or "")
		if existing:
			taken.extend(existing)
			continue
		qty = stock_qty(row)
		serials = pick_serials(row.get("item_code"), warehouse, qty, exclude=taken)
		taken.extend(serials)
		_set(row, target_field, "\n".join(serials))
	return taken


def assert_in_stock(rows, warehouse: str, mode: str) -> None:
	"""Server-side twin of the POS out-of-stock check (never trust the UI)."""
	needed: dict[str, float] = {}
	for row in rows:
		qty = stock_qty(row)
		needed[row.get("item_code")] = needed.get(row.get("item_code"), 0) + flt(qty)

	available = get_sellable_qty(needed.keys(), warehouse, mode)
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


def set_serial_status(serials, status: str) -> None:
	for sn in set(serials):
		frappe.db.set_value("Serial No", sn, "status", status, update_modified=False)


def _set(row, field, value):
	if isinstance(row, dict):
		row[field] = value
	else:
		row.set(field, value)
