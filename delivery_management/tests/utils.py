"""Shared fixtures. Every test runs inside a transaction that is rolled back."""

import datetime

import frappe
from frappe.tests.utils import FrappeTestCase

# A Monday far in the future, so real "now" never interferes.
DAY = datetime.date(2031, 3, 3)
NOW = datetime.datetime(2031, 3, 3, 15, 0, 0)  # Monday 15:00
TOMORROW = DAY + datetime.timedelta(days=1)


class DeliveryTestCase(FrappeTestCase):
	def setUp(self):
		frappe.db.rollback()
		self._refs = {}
		frappe.set_user("Administrator")
		self.settings(global_ac_daily_capacity=20, slot_lead_time_hours=2, booking_window_days=30)

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache("Delivery Settings", "Delivery Settings")
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------ helpers

	def ref(self, label: str) -> str:
		"""A real document to own a booking (the ledger validates its link)."""
		cache = self.__dict__.setdefault("_refs", {})
		if label not in cache:
			cache[label] = frappe.get_doc({"doctype": "ToDo", "description": label}).insert().name
		return cache[label]

	def settings(self, **values):
		for field, value in values.items():
			frappe.db.set_single_value("Delivery Settings", field, value)
		frappe.clear_document_cache("Delivery Settings", "Delivery Settings")

	def make_area(self, name="_DM Area", cars=2, enabled=1, hours=("10:00", "16:00", "17:00", "18:00"), days=None):
		if frappe.db.exists("Delivery Area", name):
			for dt in ("Delivery Slot Reservation", "Delivery Disabled Slot", "Delivery Slot Counter", "Delivery Work Hour"):
				frappe.db.delete(dt, {"area": name})
			frappe.db.delete("Delivery Area", {"name": name})
		area = frappe.get_doc(
			{"doctype": "Delivery Area", "area_name": name, "cars_number": cars, "enabled": enabled}
		).insert()
		for hour in hours:
			doc = {"doctype": "Delivery Work Hour", "area": area.name, "hour": hour}
			if days is not None:
				doc.update({d: (1 if d in days else 0) for d in
					("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")})
			frappe.get_doc(doc).insert()
		return area.name

	def counter(self, area, date, time):
		from delivery_management.slots.engine import COUNTER, slot_key

		return frappe.db.get_value(COUNTER, slot_key(area, date, time), "reserved_count")

	def capacity_block(self, area, date, time, source="Capacity"):
		from delivery_management.slots.engine import DISABLED, disabled_key

		return bool(frappe.db.exists(DISABLED, disabled_key(source, area, date, time)))

	def ac_reserved(self, date):
		from delivery_management.slots.engine import AC_DAY, ac_day_key

		return frappe.db.get_value(AC_DAY, ac_day_key(date), "reserved_units")

	def slot(self, area, date, time, now=NOW):
		from delivery_management.slots import get_delivery_slots

		return next(s for s in get_delivery_slots(area, date, now=now) if s["time"] == time)
