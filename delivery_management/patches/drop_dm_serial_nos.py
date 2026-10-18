"""Serials are reserved by ERPNext's Stock Reservation now; the app's own field goes."""

import frappe


def execute():
	frappe.delete_doc_if_exists("Custom Field", "Sales Order Item-dm_serial_nos")
