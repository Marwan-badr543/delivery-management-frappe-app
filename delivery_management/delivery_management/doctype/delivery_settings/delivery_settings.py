# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, today


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
		self.validate_delivery_charge_item()
		self.validate_day_limits()
		self.validate_sales_taxes()
		self.validate_customer_stores()

	def validate_customer_stores(self):
		"""Customer items move at zero value: their stores must never hold our own goods."""
		ours = {self.main_warehouse, self.transit_warehouse} - {None, ""}
		mine = [self.support_workshop_warehouse, self.support_transit_warehouse, self.support_store_warehouse]
		for store in mine:
			if store and store in ours:
				frappe.throw(_("{0} holds our own goods: choose a store only for customer items.").format(store))
		if len([s for s in mine if s]) != len({s for s in mine if s}):
			frappe.throw(_("The Workshop, Customer Items Transit and Customer Items stores must be three different stores."))
		spare = self.get("spare_parts_warehouse")
		if spare and spare in ours | set(mine):
			frappe.throw(_("The Spare Parts Store must be its own store, not {0}.").format(spare))

	def validate_sales_taxes(self):
		template = self.get("sales_taxes_template")
		if not template:
			return
		rows = frappe.get_all("Sales Taxes and Charges", fields=["idx", "included_in_print_rate"],
			filters={"parent": template, "parenttype": "Sales Taxes and Charges Template"})
		if not rows:
			frappe.throw(_("The tax template {0} has no tax rows.").format(frappe.bold(template)))
		for row in rows:
			if not cint(row.included_in_print_rate):
				frappe.throw(_("Row {0} of {1}: tick \"Is this Tax included in Basic Rate?\". POS prices already include VAT.")
					.format(row.idx, frappe.bold(template)))

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
		transit = self.get("transit_warehouse")
		if not transit:
			return
		if cint(frappe.db.get_value("Warehouse", transit, "is_group")):
			frappe.throw(_("Transit Store must be a ledger warehouse, not a group."))
		if transit == self.main_warehouse:
			frappe.throw(_("Transit Store must be another store than the Main Store."))
		if self.main_warehouse and frappe.db.get_value("Warehouse", transit, "company") != \
				frappe.db.get_value("Warehouse", self.main_warehouse, "company"):
			frappe.throw(_("Transit Store and Main Store must belong to the same company."))
		workshop = self.get("support_workshop_warehouse")
		if workshop:
			if cint(frappe.db.get_value("Warehouse", workshop, "is_group")):
				frappe.throw(_("Workshop Store must be a ledger warehouse, not a group."))
			if workshop in (transit, self.main_warehouse):
				frappe.throw(_("Workshop Store must be another store than the Main and Transit Stores."))
			if frappe.db.get_value("Warehouse", workshop, "company") != frappe.db.get_value("Warehouse", transit, "company"):
				frappe.throw(_("Workshop Store and Transit Store must belong to the same company."))

	def validate_delivery_charge_item(self):
		if not self.get("delivery_charge_item"):
			return
		is_stock, is_sales = frappe.db.get_value("Item", self.delivery_charge_item, ["is_stock_item", "is_sales_item"])
		if cint(is_stock):
			frappe.throw(_("The Delivery Charge Item must be a non-stock item (a service)."))
		if not cint(is_sales):
			frappe.throw(_("The Delivery Charge Item must be a sales item."))

	def validate_day_limits(self):
		from delivery_management.drivers.assignment import ROLE_AC

		seen = set()
		for row in self.get("ac_tech_day_limits") or []:
			if cint(row.max_orders) < 0:
				frappe.throw(_("Row {0}: Max Orders cannot be negative.").format(row.idx))
			key = (row.driver, str(row.work_date))
			if key in seen:
				frappe.throw(_("Row {0}: {1} already has a limit on {2}.").format(
					row.idx, row.driver, frappe.format(row.work_date, "Date")))
			seen.add(key)
			# Past rows are history: a technician who left later must not block saving the settings.
			if getdate(row.work_date) >= getdate(today()) and ROLE_AC not in frappe.get_roles(row.driver):
				frappe.throw(_("Row {0}: {1} does not have the {2} role.").format(row.idx, row.driver, ROLE_AC))
		self.ac_tech_day_limits.sort(key=lambda r: (str(r.work_date), r.driver or ""))
		for i, row in enumerate(self.ac_tech_day_limits, 1):
			row.idx = i

	def on_update(self):
		if self.has_value_changed("global_ac_daily_capacity"):
			from delivery_management.slots import resync_ac_days

			# The engine reads settings through the document cache; drop the stale copy first.
			frappe.clear_document_cache(self.doctype, self.name)
			resync_ac_days()
