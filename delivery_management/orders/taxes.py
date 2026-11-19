"""Default sales tax from Delivery Settings.

POS prices already include VAT, so the template must be "included in basic rate":
the grand total stays the same and ERPNext only splits it into net + VAT.
"""

import frappe

TEMPLATE = "Sales Taxes and Charges Template"


def apply_default(doc, method=None):
	"""before_validate on Sales Order / Sales Invoice / Delivery Note (drafts only)."""
	if doc.docstatus != 0 or doc.get("is_return"):
		return
	if doc.get("taxes_and_charges") or doc.get("taxes"):
		return  # chosen by hand or copied from the source document
	template = frappe.db.get_single_value("Delivery Settings", "sales_taxes_template")
	if not template:
		return
	if frappe.db.get_value(TEMPLATE, template, "company") != doc.company:
		return
	from erpnext.controllers.accounts_controller import get_taxes_and_charges

	doc.taxes_and_charges = template
	for row in get_taxes_and_charges(TEMPLATE, template) or []:
		doc.append("taxes", row)
