"""POS Awesome's own "Delivery Charges" picker is replaced by the automatic Delivery
Charge item (Delivery Settings); switch it off on every POS Profile so it adds nothing."""

import frappe


def execute():
	if frappe.db.has_column("POS Profile", "posa_use_delivery_charges"):
		frappe.db.sql("update `tabPOS Profile` set posa_use_delivery_charges = 0")
