# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class DeliverySlotReservation(Document):
	def on_trash(self):
		# Deleting a live booking would silently desync the counters.
		if self.status == "Reserved":
			frappe.throw(_("Release the reservation before deleting it."))
