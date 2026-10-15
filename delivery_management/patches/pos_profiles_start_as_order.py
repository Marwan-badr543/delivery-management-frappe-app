"""POS Awesome is no longer edited, so its own settings do what the edits did:
a new sale starts as an Order (home delivery) on profiles that allow Orders."""

import frappe


def execute():
	if frappe.db.has_column("POS Profile", "posa_default_sales_order"):
		frappe.db.sql("update `tabPOS Profile` set posa_default_sales_order = 1 where posa_allow_sales_order = 1")
