"""Stock Settings copies some of its values to the global defaults the browser reads; a
site set up by writing Stock Settings straight to the database kept old copies (serial
number fields hidden on the Delivery Note with serial numbers on). Copy them again."""

import frappe


def execute():
	settings = frappe.get_single("Stock Settings")
	for key in ("item_naming_by", "item_group", "stock_uom", "allow_negative_stock", "default_warehouse", "set_qty_in_transactions_based_on_serial_no_input",
			"use_serial_batch_fields", "enable_serial_and_batch_no_for_item",
			"set_serial_and_batch_bundle_naming_based_on_naming_series"):
		frappe.db.set_default(key, settings.get(key, ""))
