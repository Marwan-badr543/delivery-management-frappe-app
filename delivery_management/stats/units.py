"""Units of each Sales Invoice line, frozen on the line when the invoice is saved.

Sales Statistics sums these fields; it never re-reads items or bundles, so a sale
keeps the units it was made with even if an item or a Product Bundle changes later.

For a line of item X, ``qty`` = its stock qty (negative on a credit note):

* **Normal item:** AC units = qty if X uses AC capacity (the slot engine's rule);
  non-AC units = qty if X is another stock item; small units = qty if X is a
  *Small Item*.
* **Product Bundle:** each component C (``n`` per bundle) counts ``qty × n`` with
  C's own rules. If the bundle item itself is a *Small Item*, all its components
  count as small items.
* **Credit note line** made from a sale: the sold line's units per unit × qty, so
  a return takes off exactly what the sale added (even if the bundle changed).
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from delivery_management.slots.cart import get_ac_item_codes

FIELDS = ("dm_ac_units", "dm_non_ac_units", "dm_small_units")


def set_line_units(doc, method=None):
	"""Sales Invoice ``validate``: fill ``FIELDS`` on every line (a few queries per invoice)."""
	if doc.docstatus == 2:
		return
	lines = [row for row in doc.items if row.item_code]
	sold = _sold_lines(lines) if doc.is_return else {}
	per_unit = _per_unit_units({row.item_code for row in lines if id(row) not in sold})
	for row in doc.items:
		qty = flt(row.stock_qty) or flt(row.qty) * flt(row.conversion_factor or 1)
		unit = sold.get(id(row)) or per_unit.get(row.item_code) or (0, 0, 0)
		for field, value in zip(FIELDS, unit):
			row.set(field, flt(qty * value, 6))


def _per_unit_units(codes: set[str]) -> dict[str, tuple]:
	"""``{item_code: (ac, non_ac, small)}`` for one unit of each item, bundles expanded."""
	if not codes:
		return {}
	active = frappe.get_all("Product Bundle", filters={"name": ["in", list(codes)], "disabled": 0}, pluck="name")
	bundles = defaultdict(list)
	for c in frappe.get_all("Product Bundle Item", filters={"parenttype": "Product Bundle", "parent": ["in", active or [""]]},
			fields=["parent", "item_code", "qty"]):
		bundles[c.parent].append(c)
	every = codes | {c.item_code for parts in bundles.values() for c in parts}
	items = {i.name: i for i in frappe.get_all("Item", filters={"name": ["in", list(every)]},
		fields=["name", "is_stock_item", "dm_small_item"])}
	ac = get_ac_item_codes(every)

	def own(code: str) -> tuple:
		item = items.get(code) or frappe._dict()
		is_ac = code in ac
		return (1 if is_ac else 0, 1 if item.is_stock_item and not is_ac else 0, 1 if item.dm_small_item else 0)

	result = {}
	for code in codes:
		if code not in bundles:
			result[code] = own(code)
			continue
		all_small = bool((items.get(code) or {}).get("dm_small_item"))
		ac_units = non_ac = small = 0.0
		for part in bundles[code]:
			n = flt(part.qty)
			part_ac, part_non_ac, part_small = own(part.item_code)
			ac_units += n * part_ac
			non_ac += n * part_non_ac
			small += n * (1 if all_small else part_small)
		result[code] = (ac_units, non_ac, small)
	return result


def _sold_lines(lines) -> dict[str, tuple]:
	"""``{id(credit note line): per-unit units of the sold line it returns}``."""
	links = {id(row): row.sales_invoice_item for row in lines if row.get("sales_invoice_item")}
	if not links:
		return {}
	sold = {r.name: r for r in frappe.get_all("Sales Invoice Item", filters={"name": ["in", list(links.values())]},
		fields=["name", "stock_qty", *FIELDS])}
	result = {}
	for line, original in links.items():
		r = sold.get(original)
		if r and flt(r.stock_qty):
			result[line] = tuple(flt(r.get(f)) / flt(r.stock_qty) for f in FIELDS)
	return result
