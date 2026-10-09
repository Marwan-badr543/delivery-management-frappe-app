"""Customer delivery address: title, area, block, road, building.

The POS and the Sales Order keep the address in structured fields
(``dm_delivery_area``, ``dm_block_no``, ``dm_road_no``, ``dm_building_no``).
Addresses created by the old ordering site only have text, e.g.
``address_line1 = "MALKIYA, block 1034, road 3479, building 3105"``; those are
parsed so the POS can still pre-fill them, and are upgraded in place (the
structured fields are filled) the first time they are used again.
"""

from __future__ import annotations

import re

import frappe
from frappe import _

FIELDS = ("dm_delivery_area", "dm_block_no", "dm_road_no", "dm_building_no")
NUMBER_FIELDS = {"dm_block_no": "Block No", "dm_road_no": "Road No", "dm_building_no": "Building No"}

# Arabic-Indic (U+0660..) and Persian (U+06F0..) digits typed on an Arabic keyboard.
_TO_ASCII_DIGITS = {**{0x0660 + i: str(i) for i in range(10)}, **{0x06F0 + i: str(i) for i in range(10)}}
_ASCII_NUMBER = re.compile(r"[0-9]+")

_LEGACY_LINE = re.compile(
	r"^\s*(?P<area>[^,]*?)\s*,\s*block\s*(?P<block>\d+)\s*,\s*road\s*(?P<road>\d+)\s*,\s*building\s*(?P<building>\d+)",
	re.IGNORECASE,
)


def normalise(delivery: dict | None) -> dict:
	"""POS/API payload -> address field values. Does not validate."""
	delivery = delivery or {}
	return {
		"dm_delivery_area": (delivery.get("area") or "").strip() or None,
		"dm_block_no": _clean(delivery.get("block_no")),
		"dm_road_no": _clean(delivery.get("road_no")),
		"dm_building_no": _clean(delivery.get("building_no")),
	}


def _clean(value) -> str:
	return str(value if value is not None else "").strip().translate(_TO_ASCII_DIGITS)


def validate_numbers(values: dict) -> None:
	"""Block, road and building are numbers only (Bahrain addressing)."""
	for field, label in NUMBER_FIELDS.items():
		value = values.get(field)
		if value and not _ASCII_NUMBER.fullmatch(str(value)):
			frappe.throw(
				_("{0} must contain digits only, not {1}.").format(_(label), frappe.bold(value)),
				title=_("Delivery Address"),
			)


def clean_numbers(doc) -> None:
	"""Normalise Block/Road/Building on a document (Arabic digits -> 0-9) and validate them."""
	for field in NUMBER_FIELDS:
		if doc.get(field):
			doc.set(field, _clean(doc.get(field)))
	validate_numbers({f: doc.get(f) for f in NUMBER_FIELDS})


def is_complete(values: dict) -> bool:
	return all(values.get(f) for f in FIELDS)


def is_empty(values: dict) -> bool:
	return not any(values.get(f) for f in FIELDS)


# ---------------------------------------------------------------------------
# Pre-fill
# ---------------------------------------------------------------------------


def get_customer_delivery_address(customer: str) -> dict | None:
	"""The customer's delivery address to pre-fill the POS with.

	Order of preference: the address of the customer's latest Sales Order, then
	the newest structured address, then the newest address whose text can be
	parsed (old ordering site).
	"""
	addresses = _customer_addresses(customer)
	if not addresses:
		return None
	by_name = {a.name: a for a in addresses}

	last_order_address = frappe.db.get_value(
		"Sales Order",
		{"customer": customer, "docstatus": 1, "shipping_address_name": ["is", "set"]},
		"shipping_address_name",
		order_by="creation desc",
	)
	candidates = ([by_name[last_order_address]] if last_order_address in by_name else []) + addresses
	for address in candidates:
		values = structured_values(address)
		if not is_empty(values):
			return _as_payload(address, values)
	return None


def structured_values(address) -> dict:
	"""Structured values of an Address row, parsed from its text when the fields are empty."""
	values = {f: (address.get(f) or "") for f in FIELDS}
	values["dm_delivery_area"] = values["dm_delivery_area"] or None
	if not is_empty(values):
		return values
	match = _LEGACY_LINE.match(address.get("address_line1") or "")
	if not match:
		return values
	return {
		"dm_delivery_area": match_area(match.group("area") or address.get("city")),
		"dm_block_no": match.group("block"),
		"dm_road_no": match.group("road"),
		"dm_building_no": match.group("building"),
	}


def match_area(text: str | None) -> str | None:
	"""The Delivery Area whose name equals ``text`` ignoring case, if any."""
	text = (text or "").strip()
	if not text:
		return None
	return frappe.db.get_value("Delivery Area", {"area_name": text}, "name") or frappe.db.get_value(
		"Delivery Area", {"name": text}, "name"
	)


def _as_payload(address, values: dict) -> dict:
	return {
		"address_name": address.name,
		"address_title": address.address_title,
		"area": values["dm_delivery_area"],
		"block_no": values["dm_block_no"],
		"road_no": values["dm_road_no"],
		"building_no": values["dm_building_no"],
	}


def _customer_addresses(customer: str) -> list:
	return frappe.db.sql(
		"""select a.name, a.address_title, a.address_line1, a.city, a.is_shipping_address,
			a.dm_delivery_area, a.dm_block_no, a.dm_road_no, a.dm_building_no
		from `tabAddress` a join `tabDynamic Link` l on l.parent = a.name and l.parenttype = 'Address'
		where l.link_doctype = 'Customer' and l.link_name = %s and a.disabled = 0
		order by a.is_shipping_address desc, a.modified desc""",
		(customer,),
		as_dict=True,
	)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def default_title(customer: str) -> str:
	return frappe.db.get_value("Customer", customer, "customer_name") or customer


def upsert_customer_address(customer: str, delivery: dict | None) -> str | None:
	"""Return the customer's Address matching ``delivery``, creating it only when new.

	An existing address is reused when its title, area, block, road and building
	all match (an old text-only address counts when its text parses to the same
	values; its structured fields are then filled in). Otherwise a new Address
	is added and the old ones are kept, so earlier documents still point at what
	was true then.
	"""
	values = normalise(delivery)
	if is_empty(values):
		return None
	validate_numbers(values)
	title = ((delivery or {}).get("address_title") or "").strip() or default_title(customer)

	addresses = _customer_addresses(customer)
	for address in addresses:
		if (address.address_title or "").strip().lower() != title.lower():
			continue
		if structured_values(address) != values:
			continue
		if is_empty({f: address.get(f) for f in FIELDS}):
			# Old site address: store the structured fields so the next lookup is direct.
			frappe.db.set_value("Address", address.name, values, update_modified=False)
		return address.name

	has_shipping = any(a.is_shipping_address for a in addresses)
	address = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": title,
			"address_type": "Shipping",
			"address_line1": format_line(values),
			"city": values["dm_delivery_area"] or _("Bahrain"),
			"country": _default_country(),
			"is_shipping_address": 0 if has_shipping else 1,
			"is_primary_address": 0 if has_shipping else 1,
			**values,
			"links": [{"link_doctype": "Customer", "link_name": customer}],
		}
	)
	address.insert(ignore_permissions=True)
	return address.name


def format_line(values: dict) -> str:
	"""Same text layout as the old ordering site: ``AREA, block 1, road 2, building 3``."""
	parts = [values.get("dm_delivery_area")]
	for field, word in (("dm_block_no", "block"), ("dm_road_no", "road"), ("dm_building_no", "building")):
		if values.get(field):
			parts.append(f"{word} {values[field]}")
	return ", ".join(p for p in parts if p) or _("Delivery address")


def _default_country() -> str:
	company = frappe.defaults.get_global_default("company")
	return (company and frappe.get_cached_value("Company", company, "country")) or frappe.db.get_default(
		"country"
	) or "Bahrain"
