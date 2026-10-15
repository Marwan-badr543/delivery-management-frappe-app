"""The app now does what these production server scripts did (see DELIVERY_SYSTEM.md §12):

* "Delivery Note From Sales Order"  -> orders.delivery_note.create_from_sales_order
* "invalidate serial after insert delivery note" / "active serial after delete delivery note"
  -> orders.serials (reserve / release serials of draft Delivery Notes)

Left enabled they would run twice (two Delivery Notes per order). They are disabled, not
deleted, so they can be looked at or switched back on by hand.
"""

import frappe

REPLACED = (
	"Delivery Note From Sales Order",
	"invalidate serial after insert delivery note",
	"active serial after delete delivery note",
)


def execute():
	# The app takes over: a site that had the script on gets automatic Delivery Notes.
	if not frappe.db.sql("""select 1 from `tabSingles` where doctype = 'Delivery Settings'
			and field = 'auto_create_delivery_note'"""):
		frappe.db.set_single_value("Delivery Settings", "auto_create_delivery_note", 1)
	for name in REPLACED:
		if frappe.db.exists("Server Script", name):
			frappe.db.set_value("Server Script", name, "disabled", 1)
	frappe.client_cache.delete_value("server_script_map")
