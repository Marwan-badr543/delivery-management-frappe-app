"""Classify order lines into AC installations and normal delivery goods.

Every line falls in one of three kinds:

``ac``
    The Item Group is one of the configured AC groups or any group under one, and the
    line is not excluded. Each unit (in stock UOM) uses one unit of the day's AC
    installation capacity.
``ac_accessory``
    Under the AC group but excluded, either by group (e.g. ``Air Conditioner
    Accessory``) or by item (e.g. gas refill). These never use AC capacity.
    They travel with the installation when the order has AC units, and need a
    normal delivery slot only when the order has none.
``other``
    Everything else. A stock item needs a normal delivery slot; a non-stock line
    (service, charge) needs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import frappe
from frappe.utils import cint, flt

from delivery_management.slots.settings import get_settings

KIND_AC = "ac"
KIND_ACCESSORY = "ac_accessory"
KIND_OTHER = "other"


@dataclass
class CartProfile:
	ac_units: int = 0
	ac_items: list[str] = field(default_factory=list)
	accessory_items: list[str] = field(default_factory=list)
	other_stock_items: list[str] = field(default_factory=list)

	@property
	def has_ac(self) -> bool:
		return self.ac_units > 0

	@property
	def non_ac_items(self) -> list[str]:
		"""Lines that need a normal delivery slot."""
		return self.other_stock_items + ([] if self.has_ac else self.accessory_items)

	@property
	def has_non_ac(self) -> bool:
		return bool(self.non_ac_items)

	def as_dict(self) -> dict:
		return {
			"ac_units": self.ac_units,
			"has_ac": self.has_ac,
			"has_non_ac": self.has_non_ac,
			"ac_items": self.ac_items,
			"accessory_items": self.accessory_items,
			"non_ac_items": self.non_ac_items,
		}


def _group_range(group: str):
	return frappe.db.get_value("Item Group", group, ["lft", "rgt"])


def classify_items(item_codes) -> dict[str, str]:
	"""Map each item code to ``ac``, ``ac_accessory`` or ``other``."""
	item_codes = list({c for c in item_codes if c})
	kinds = {code: KIND_OTHER for code in item_codes}
	settings = get_settings()
	roots = [r for r in (_group_range(g) for g in settings.ac_item_groups) if r]
	if not item_codes or not roots:
		return kinds

	excluded_ranges = [r for r in (_group_range(g) for g in settings.ac_excluded_item_groups) if r]
	excluded_items = set(settings.ac_excluded_items)
	rows = frappe.db.sql(
		"""select i.name, g.lft, g.rgt
		from `tabItem` i join `tabItem Group` g on g.name = i.item_group
		where i.name in %(codes)s""",
		{"codes": tuple(item_codes)},
		as_dict=True,
	)
	for r in rows:
		if not any(r.lft >= lft and r.rgt <= rgt for lft, rgt in roots):
			continue
		excluded = r.name in excluded_items or any(
			r.lft >= ex_lft and r.rgt <= ex_rgt for ex_lft, ex_rgt in excluded_ranges
		)
		kinds[r.name] = KIND_ACCESSORY if excluded else KIND_AC
	return kinds


def get_ac_item_codes(item_codes) -> set[str]:
	"""The subset of ``item_codes`` that consume AC installation capacity."""
	return {code for code, kind in classify_items(item_codes).items() if kind == KIND_AC}


def is_ac_item(item_code: str) -> bool:
	return item_code in get_ac_item_codes([item_code])


def stock_qty(line) -> float:
	"""Quantity of a line in stock UOM (POS rows may have only ``qty`` and ``conversion_factor``)."""
	if line.get("stock_qty"):
		return flt(line.get("stock_qty"))
	return flt(line.get("qty")) * flt(line.get("conversion_factor") or 1)


def expand_bundles(lines: list[tuple[str, float]]) -> list[tuple[str, float]]:
	"""Replace each Product Bundle line by its component items.

	A bundle parent is a non-stock item, so without this a bundle of a TV and a
	bracket would book no delivery slot and an AC bundle no AC capacity.
	"""
	codes = list({code for code, _qty in lines})
	if not codes:
		return lines
	bundles = dict(frappe.get_all("Product Bundle", filters={"new_item_code": ["in", codes], "disabled": 0},
		fields=["new_item_code", "name"], as_list=True))
	if not bundles:
		return lines
	components: dict[str, list] = {}
	for row in frappe.get_all("Product Bundle Item", filters={"parent": ["in", list(bundles.values())],
			"parenttype": "Product Bundle"}, fields=["parent", "item_code", "qty"], order_by="idx asc"):
		components.setdefault(row.parent, []).append((row.item_code, flt(row.qty)))
	expanded = []
	for code, qty in lines:
		if code in bundles:
			expanded += [(child, qty * child_qty) for child, child_qty in components.get(bundles[code], [])]
		else:
			expanded.append((code, qty))
	return expanded


def component_rows(items) -> list[dict]:
	"""Order lines as ``{item_code, stock_qty}`` with every bundle replaced by its components
	(what actually leaves the warehouse)."""
	return [{"item_code": code, "stock_qty": qty}
		for code, qty in expand_bundles([(it.get("item_code"), stock_qty(it)) for it in items if it.get("item_code")])]


def profile_cart(items) -> CartProfile:
	"""Build a :class:`CartProfile` from order lines (dicts or child docs). Bundles count by their components."""
	lines = expand_bundles([(it.get("item_code"), stock_qty(it)) for it in items if it.get("item_code")])
	codes = [code for code, _qty in lines]
	kinds = classify_items(codes)
	stock_codes = set(
		frappe.get_all("Item", filters={"name": ["in", codes or [""]], "is_stock_item": 1}, pluck="name")
	)

	profile = CartProfile()
	for code, qty in lines:
		kind = kinds.get(code, KIND_OTHER)
		if kind == KIND_AC:
			profile.ac_units += cint(round(qty))
			_add(profile.ac_items, code)
		elif code not in stock_codes:
			continue
		elif kind == KIND_ACCESSORY:
			_add(profile.accessory_items, code)
		else:
			_add(profile.other_stock_items, code)
	return profile


def _add(bucket: list, code: str) -> None:
	if code not in bucket:
		bucket.append(code)


def unit_counts(items) -> tuple[int, float]:
	"""``(ac_units, non_ac_units)`` of order lines, bundles counted by their components.

	Non-AC units are the stock lines that need a normal delivery: AC accessories count
	only when there is no AC unit (otherwise they go with the installation). Non-stock
	lines (delivery charge, services) are not units.
	"""
	lines = expand_bundles([(it.get("item_code"), stock_qty(it)) for it in items if it.get("item_code")])
	codes = [code for code, _qty in lines]
	kinds = classify_items(codes)
	stock_codes = set(
		frappe.get_all("Item", filters={"name": ["in", codes or [""]], "is_stock_item": 1}, pluck="name")
	)
	ac = sum(qty for code, qty in lines if kinds.get(code) == KIND_AC)
	accessories = sum(qty for code, qty in lines if kinds.get(code) == KIND_ACCESSORY and code in stock_codes)
	other = sum(qty for code, qty in lines if kinds.get(code, KIND_OTHER) == KIND_OTHER and code in stock_codes)
	ac_units = cint(round(ac))
	return ac_units, flt(other + (0 if ac_units else accessories))
