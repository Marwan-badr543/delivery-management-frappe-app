"""Orders reserve a quantity, not serials: the serials are scanned on the Delivery Note
when the boxes leave the store (orders.reservation)."""

import frappe


def execute():
	frappe.db.set_single_value("Stock Settings", "auto_reserve_serial_and_batch", 0)
