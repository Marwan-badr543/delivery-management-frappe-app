"""Install/migrate setup. Idempotent and safe on production: it adds a role, custom
fields and default settings only. It never creates business data."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

ROLE = "Delivery Manager"


def _delivery_fields(insert_after: str, editable: bool) -> list[dict]:
	"""Schedule + address fields. ``editable``: on the Sales Order they can be filled by
	hand (booked on submit, see orders/sales_order.py); on the Sales Invoice they only
	mirror the order."""
	ro = {"read_only": 1, "no_copy": 1}
	rw = {"read_only": 0, "no_copy": 1} if editable else ro
	return [
		{"fieldname": "dm_delivery_section", "fieldtype": "Section Break", "label": "Delivery Schedule",
			"insert_after": insert_after, "collapsible": 0},
		{"fieldname": "dm_order_type", "fieldtype": "Select", "label": "POS Order Type",
			"options": "\nInvoice\nOrder", "insert_after": "dm_delivery_section", "in_standard_filter": 1, **ro},
		{"fieldname": "dm_delivery_area", "fieldtype": "Link", "label": "Delivery Area", "options": "Delivery Area",
			"insert_after": "dm_order_type", "in_standard_filter": 1, **rw},
		{"fieldname": "dm_delivery_date", "fieldtype": "Date", "label": "Delivery Slot Date",
			"insert_after": "dm_delivery_area", "in_standard_filter": 1, **rw,
			**({"description": "Use <b>Choose Delivery Slot</b> (top right) to pick the time and the AC "
				"installation day; only free slots are listed. Booked when the order is submitted."}
				if editable else {})},
		# Set through the "Choose Delivery Slot" picker (read-only in the form script).
		{"fieldname": "dm_delivery_time", "fieldtype": "Time", "label": "Delivery Slot Time",
			"insert_after": "dm_delivery_date", **rw},
		{"fieldname": "dm_column_break_1", "fieldtype": "Column Break", "insert_after": "dm_delivery_time"},
		{"fieldname": "dm_ac_installation_date", "fieldtype": "Date", "label": "AC Installation Date",
			"insert_after": "dm_column_break_1", "in_standard_filter": 1, **rw},
		{"fieldname": "dm_ac_units", "fieldtype": "Int", "label": "AC Units", "insert_after": "dm_ac_installation_date",
			"description": "Counted from the AC items.", **ro},
		{"fieldname": "dm_column_break_2", "fieldtype": "Column Break", "insert_after": "dm_ac_units"},
		{"fieldname": "dm_block_no", "fieldtype": "Data", "label": "Block No", "insert_after": "dm_column_break_2", **rw},
		{"fieldname": "dm_road_no", "fieldtype": "Data", "label": "Road No", "insert_after": "dm_block_no", **rw},
		{"fieldname": "dm_building_no", "fieldtype": "Data", "label": "Building No", "insert_after": "dm_road_no", **rw},
	]


def get_custom_fields() -> dict:
	return {
		"Address": [
			{"fieldname": "dm_delivery_area", "fieldtype": "Link", "label": "Delivery Area",
				"options": "Delivery Area", "insert_after": "address_line2"},
			{"fieldname": "dm_block_no", "fieldtype": "Data", "label": "Block No", "insert_after": "dm_delivery_area"},
			{"fieldname": "dm_road_no", "fieldtype": "Data", "label": "Road No", "insert_after": "dm_block_no"},
			{"fieldname": "dm_building_no", "fieldtype": "Data", "label": "Building No", "insert_after": "dm_road_no"},
		],
		# On the Details tab, just above the items.
		"Sales Order": _delivery_fields("ignore_pricing_rule", editable=True),
		"Sales Invoice": _delivery_fields("ignore_pricing_rule", editable=False),
		"Sales Order Item": [
			{"fieldname": "dm_serial_nos", "fieldtype": "Small Text", "label": "Allocated Serial Nos",
				"insert_after": "warehouse", "read_only": 1, "no_copy": 1,
				"description": "Serials picked from the main store when the POS order was placed."},
		],
	}


def ensure_role():
	if not frappe.db.exists("Role", ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(ignore_permissions=True)


def ensure_default_settings():
	defaults = {"global_ac_daily_capacity": 20, "slot_lead_time_hours": 2, "booking_window_days": 30}
	for field, value in defaults.items():
		# get_single_value returns 0 for an unset Int, so check that the row exists instead.
		if not frappe.db.sql(
			"select 1 from `tabSingles` where doctype = 'Delivery Settings' and field = %s", (field,)
		):
			frappe.db.set_single_value("Delivery Settings", field, value)
	_ensure_ac_item_groups()
	if not frappe.db.get_single_value("Delivery Settings", "main_warehouse") and frappe.db.exists(
		"Warehouse", "Stores - SHD"
	):
		frappe.db.set_single_value("Delivery Settings", "main_warehouse", "Stores - SHD")


def _ensure_ac_item_groups():
	"""AC groups became a list. Carry over the old single ``ac_item_group`` value,
	or default to "Air Conditioner"."""
	if frappe.db.count("Delivery AC Item Group", {"parent": "Delivery Settings"}):
		return
	old = frappe.db.sql(
		"select value from `tabSingles` where doctype = 'Delivery Settings' and field = 'ac_item_group'"
	)
	group = (old and old[0][0]) or "Air Conditioner"
	if not frappe.db.exists("Item Group", group):
		return
	settings = frappe.get_single("Delivery Settings")
	settings.append("ac_item_groups", {"item_group": group})
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)
	frappe.db.delete("Singles", {"doctype": "Delivery Settings", "field": "ac_item_group"})


def after_install():
	after_migrate()


def ensure_indexes():
	"""Sales Statistics filters invoices by creation date; index it (no-op if present)."""
	frappe.db.add_index("Sales Invoice", ["creation"], index_name="dm_creation_index")


def after_migrate():
	ensure_role()
	create_custom_fields(get_custom_fields(), ignore_validate=True, update=True)
	ensure_default_settings()
	ensure_indexes()
