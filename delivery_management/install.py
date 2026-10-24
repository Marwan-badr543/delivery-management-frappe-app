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


def _delivery_note_fields() -> list[dict]:
	"""Delivery Schedule section of the Delivery Note: the order's schedule (read-only,
	copied from the Sales Order), its Order Type and the driver (set from the Driver
	Assignment page)."""
	fields = [f for f in _delivery_fields("ignore_pricing_rule", editable=False) if f["fieldname"] != "dm_order_type"]
	for f in fields:
		if f["fieldname"] == "dm_delivery_area":
			f["insert_after"] = "dm_driver"
		if f["fieldname"] == "dm_delivery_date":
			f["in_list_view"] = 1
	return fields[:1] + [
		{"fieldname": "dm_delivery_type", "fieldtype": "Select", "label": "Order Type", "options": "\nAC\nNon AC",
			"insert_after": "dm_delivery_section", "in_list_view": 1, "in_standard_filter": 1, "bold": 1,
			"no_copy": 1, "search_index": 1,
			"description": "Set from the items (AC item groups, like the POS). An order with AC and non-AC "
				"items has no type until you choose one."},
		{"fieldname": "dm_driver", "fieldtype": "Link", "label": "Driver / AC Tech", "options": "User",
			"insert_after": "dm_delivery_type", "read_only": 1, "no_copy": 1, "in_list_view": 1,
			"in_standard_filter": 1, "search_index": 1,
			"description": "Assigned on the <a href='/desk/driver-assignment'>Driver Assignment</a> page."},
	] + fields[1:]


def _issue_fields() -> list[dict]:
	"""Support ticket fields on Issue (customer_support/tickets.py). Set from the Support
	pages, so read-only here."""
	ro = {"read_only": 1, "no_copy": 1}
	return [
		{"fieldname": "sm_stage", "fieldtype": "Select", "label": "Support Stage", "insert_after": "status",
			"options": "\nCustomer Service\nTechnician\nMaintenance Technician", "in_list_view": 1,
			"in_standard_filter": 1, "search_index": 1, "bold": 1, **ro,
			"description": "Moved on the <a href='/desk/support-tickets'>Support Tickets</a> pages."},
		{"fieldname": "sm_section", "fieldtype": "Section Break", "label": "Support Ticket", "insert_after": "description",
			"collapsible": 0, "depends_on": "sm_stage"},
		{"fieldname": "sm_customer_phone", "fieldtype": "Data", "options": "Phone", "label": "Customer Phone",
			"insert_after": "sm_section", "in_standard_filter": 1, **ro},
		{"fieldname": "sm_sales_invoice", "fieldtype": "Link", "options": "Sales Invoice", "label": "Sales Invoice",
			"insert_after": "sm_customer_phone", **ro},
		{"fieldname": "sm_delivery_note", "fieldtype": "Link", "options": "Delivery Note", "label": "Delivery Note",
			"insert_after": "sm_sales_invoice", **ro},
		{"fieldname": "sm_item_code", "fieldtype": "Link", "options": "Item", "label": "Item", "insert_after": "sm_delivery_note", **ro},
		{"fieldname": "sm_serial_no", "fieldtype": "Link", "options": "Serial No", "label": "Serial No",
			"insert_after": "sm_item_code", **ro},
		{"fieldname": "sm_column_1", "fieldtype": "Column Break", "insert_after": "sm_serial_no"},
		{"fieldname": "sm_address", "fieldtype": "Small Text", "label": "Visit Address", "insert_after": "sm_column_1", **ro},
		{"fieldname": "sm_visit_datetime", "fieldtype": "Datetime", "label": "Visit Date and Time",
			"insert_after": "sm_address", "in_list_view": 1, **ro},
		{"fieldname": "sm_cs_user", "fieldtype": "Link", "options": "User", "label": "Customer Service",
			"insert_after": "sm_visit_datetime", "in_standard_filter": 1, "search_index": 1, **ro},
		{"fieldname": "sm_technician", "fieldtype": "Link", "options": "User", "label": "Technician",
			"insert_after": "sm_cs_user", "in_standard_filter": 1, "search_index": 1, **ro},
		{"fieldname": "sm_maint_technician", "fieldtype": "Link", "options": "User", "label": "Maintenance Technician",
			"insert_after": "sm_technician", "in_standard_filter": 1, "search_index": 1, **ro},
		{"fieldname": "sm_reports_section", "fieldtype": "Section Break", "label": "Support Reports",
			"insert_after": "sm_maint_technician", "depends_on": "sm_stage"},
		{"fieldname": "sm_cs_report", "fieldtype": "Small Text", "label": "Customer Service Report",
			"insert_after": "sm_reports_section", **ro},
		{"fieldname": "sm_tech_visits", "fieldtype": "Table", "options": "Support Technician Visit",
			"label": "Technician Visits", "insert_after": "sm_cs_report", **ro},
		{"fieldname": "sm_maint_report", "fieldtype": "Small Text", "label": "Maintenance Technician Report",
			"insert_after": "sm_tech_visits", **ro},
		{"fieldname": "sm_maint_report_by", "fieldtype": "Link", "options": "User", "label": "Maintenance Report By",
			"insert_after": "sm_maint_report", **ro},
		{"fieldname": "sm_pickup_section", "fieldtype": "Section Break", "label": "Workshop Pickup",
			"insert_after": "sm_maint_report_by", "depends_on": "sm_pickup_date", "collapsible": 1},
		{"fieldname": "sm_pickup_area", "fieldtype": "Link", "options": "Delivery Area", "label": "Pickup Area",
			"insert_after": "sm_pickup_section", **ro},
		{"fieldname": "sm_pickup_date", "fieldtype": "Date", "label": "Pickup Date", "insert_after": "sm_pickup_area", **ro},
		{"fieldname": "sm_pickup_time", "fieldtype": "Time", "label": "Pickup Time", "insert_after": "sm_pickup_date", **ro},
		{"fieldname": "sm_resolved_by", "fieldtype": "Link", "options": "User", "label": "Resolved By",
			"insert_after": "sm_pickup_time", **ro},
	]


def get_custom_fields() -> dict:
	return {
		"Issue": _issue_fields(),
		"Item": [
			{"fieldname": "dm_has_delivery_charge", "fieldtype": "Check", "label": "Has Delivery Charge",
				"insert_after": "is_sales_item",
				"description": "In the POS, adding this item also adds the Delivery Charge Item (Delivery Settings), "
					"once per order."},
		],
		"Delivery Note": _delivery_note_fields(),
		"Address": [
			{"fieldname": "dm_delivery_area", "fieldtype": "Link", "label": "Delivery Area",
				"options": "Delivery Area", "insert_after": "address_line2"},
			{"fieldname": "dm_block_no", "fieldtype": "Data", "label": "Block No", "insert_after": "dm_delivery_area"},
			{"fieldname": "dm_road_no", "fieldtype": "Data", "label": "Road No", "insert_after": "dm_block_no"},
			{"fieldname": "dm_building_no", "fieldtype": "Data", "label": "Building No", "insert_after": "dm_road_no"},
		],
		# On the Details tab, just above the items.
		"Sales Order": _delivery_fields("ignore_pricing_rule", editable=True),
		"Sales Invoice": [
			*_delivery_fields("ignore_pricing_rule", editable=False),
			{"fieldname": "dm_n8n_sent", "fieldtype": "Check", "label": "Sent to n8n", "insert_after": "dm_building_no",
				"read_only": 1, "no_copy": 1},
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


DRIVER_ROLES = ("AC Tech", "TV Driver")


def ensure_driver_roles():
	"""The roles also ship as fixtures; this covers a fresh install before fixtures sync,
	and gives them read/print on Delivery Note (rows are limited to their own orders by
	``drivers.delivery_note.has_permission``)."""
	from frappe.permissions import add_permission, update_permission_property

	for role in DRIVER_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)
		if not frappe.db.exists("Custom DocPerm", {"parent": "Delivery Note", "role": role, "permlevel": 0}):
			add_permission("Delivery Note", role, 0)
			update_permission_property("Delivery Note", role, 0, "print", 1)


def ensure_support_roles():
	"""Technician / Maintenance Technician (fixtures too). They read Issue (only their own
	tickets, ``customer_support.permissions``); customer service uses ERPNext's Support Team."""
	from frappe.permissions import add_permission

	from delivery_management.customer_support.tickets import ROLE_MAINT, ROLE_TECH

	for role in (ROLE_TECH, ROLE_MAINT):
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)
		if not frappe.db.exists("Custom DocPerm", {"parent": "Issue", "role": role}):
			add_permission("Issue", role, 0)


def after_migrate():
	ensure_role()
	ensure_driver_roles()
	ensure_support_roles()
	create_custom_fields(get_custom_fields(), ignore_validate=True, update=True)
	ensure_default_settings()
	ensure_indexes()
