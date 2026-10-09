# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

from delivery_management.slots.timeutils import WEEKDAY_FIELDS, to_time_str


class DeliveryWorkHour(Document):
	def validate(self):
		self.hour = to_time_str(self.hour)
		duplicate = frappe.db.exists(
			"Delivery Work Hour", {"area": self.area, "hour": self.hour, "name": ["!=", self.name]}
		)
		if duplicate:
			frappe.throw(
				_("Slot {0} already exists for area {1} ({2}).").format(self.hour[:5], self.area, duplicate),
				frappe.DuplicateEntryError,
			)
		self.guard_removed_days()

	def on_trash(self):
		self.throw_if_booked(self.area, self.hour, WEEKDAY_FIELDS)

	def guard_removed_days(self):
		"""Removing a slot that still has future bookings would hide those bookings."""
		old = self.get_doc_before_save()
		if not old:
			return
		if old.area != self.area or to_time_str(old.hour) != self.hour:
			removed = [d for d in WEEKDAY_FIELDS if old.get(d)]
		else:
			removed = [d for d in WEEKDAY_FIELDS if old.get(d) and not self.get(d)]
		if removed:
			self.throw_if_booked(old.area, to_time_str(old.hour), removed)

	@staticmethod
	def throw_if_booked(area, hour, weekdays):
		hour = to_time_str(hour)
		dates = frappe.get_all(
			"Delivery Slot Reservation",
			filters={
				"slot_type": "Delivery",
				"status": "Reserved",
				"area": area,
				"delivery_time": hour,
				"delivery_date": [">=", nowdate()],
			},
			pluck="delivery_date",
			distinct=True,
		)
		booked = sorted({d for d in dates if WEEKDAY_FIELDS[getdate(d).weekday()] in weekdays})
		if booked:
			frappe.throw(
				_("Slot {0} in {1} still has bookings on {2}. Reschedule or cancel them first.").format(
					hour[:5], area, ", ".join(str(d) for d in booked)
				),
				title=_("Slot In Use"),
			)
