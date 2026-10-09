# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class DeliveryArea(Document):
	def validate(self):
		self.area_name = (self.area_name or "").strip()
		if cint(self.cars_number) < 0:
			frappe.throw(_("Number of Cars cannot be negative."))

	def on_update(self):
		# Capacity changed: re-evaluate which booked slots are now full or free.
		if self.has_value_changed("cars_number") or self.has_value_changed("enabled"):
			from delivery_management.slots import resync_area

			resync_area(self.name)
