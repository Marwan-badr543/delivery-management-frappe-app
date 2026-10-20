"""Endpoint of the Sales Dashboard page (managers only); every call reads the database."""

import frappe
from frappe import _
from frappe.utils import getdate

from delivery_management.stats import dashboard

PAGE_ROLES = ("System Manager", "Sales Manager", "Accounts Manager")


@frappe.whitelist()
def get_dashboard(start_date, end_date, area=None):
	frappe.only_for(PAGE_ROLES)
	start, end = getdate(start_date), getdate(end_date)
	if start > end:
		frappe.throw(_("Start date must be on or before the end date."))
	return {
		"summary": dashboard.summary(),
		"status": dashboard.status_stats(start, end),
		"areas": dashboard.area_stats(start, end),
		"top_products": dashboard.top_products(start, end, area or None),
	}
