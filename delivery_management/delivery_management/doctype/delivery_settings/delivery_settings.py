# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class DeliverySettings(Document):
	def validate(self):
		if cint(self.global_ac_daily_capacity) < 0:
			frappe.throw(_("Global AC Daily Capacity cannot be negative."))
		if flt(self.slot_lead_time_hours) < 0:
			frappe.throw(_("Slot Lead Time Hours cannot be negative."))
		if cint(self.booking_window_days) <= 0:
			self.booking_window_days = 30
		self.validate_excluded_groups()
		self.validate_main_warehouse()

	def validate_excluded_groups(self):
		groups = [row.item_group for row in self.ac_item_groups if row.item_group]
		if not groups:
			return
		ranges = [frappe.db.get_value("Item Group", g, ["lft", "rgt"]) for g in groups]
		for row in self.ac_excluded_item_groups:
			g_lft, g_rgt = frappe.db.get_value("Item Group", row.item_group, ["lft", "rgt"])
			if not any(g_lft > lft and g_rgt < rgt for lft, rgt in ranges):
				frappe.throw(
					_("Excluded group {0} must be a child of one of the AC Item Groups ({1}).").format(
						frappe.bold(row.item_group), ", ".join(groups)
					)
				)

	def validate_main_warehouse(self):
		if self.main_warehouse and cint(frappe.db.get_value("Warehouse", self.main_warehouse, "is_group")):
			frappe.throw(_("Main Store must be a ledger warehouse, not a group."))

	def on_update(self):
		if self.has_value_changed("global_ac_daily_capacity"):
			from delivery_management.slots import resync_ac_days

			# The engine reads settings through the document cache; drop the stale copy first.
			frappe.clear_document_cache(self.doctype, self.name)
			resync_ac_days()
