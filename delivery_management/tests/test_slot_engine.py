"""Unit tests for the delivery slot engine.

Run: bench --site v14.local run-tests --app delivery_management
"""

import datetime
from unittest.mock import patch

import frappe

from delivery_management.slots import (
	SlotUnavailableError,
	get_ac_calendar,
	get_ac_day_status,
	get_delivery_slots,
	get_reservations,
	rebuild_from_ledger,
	release,
	release_reservation,
	reschedule,
	reserve,
	set_ac_day_capacity,
	set_ac_day_manual_block,
	set_slot_manual_block,
)
from delivery_management.slots import engine
from delivery_management.slots.timeutils import is_too_soon, to_time_str
from delivery_management.tests.utils import DAY, NOW, TOMORROW, DeliveryTestCase

REF = "ToDo"  # any real document works as a booking owner


class TestTimeUtils(DeliveryTestCase):
	def test_time_normalisation(self):
		self.assertEqual(to_time_str("9:5"), "09:05:00")
		self.assertEqual(to_time_str("16:00"), "16:00:00")
		self.assertEqual(to_time_str("16:00:00.000000"), "16:00:00")
		self.assertEqual(to_time_str(datetime.timedelta(hours=16, minutes=30)), "16:30:00")
		self.assertEqual(to_time_str(datetime.time(7, 0)), "07:00:00")
		self.assertRaises(frappe.ValidationError, to_time_str, "25:00")
		self.assertRaises(frappe.ValidationError, to_time_str, "abc")

	def test_lead_time_user_example(self):
		"""Lead 2h at 15:00: 16:00 and 17:00 are disabled, 18:00 is the first open slot."""
		self.assertTrue(is_too_soon(DAY, "14:00", 2, NOW))  # passed
		self.assertTrue(is_too_soon(DAY, "16:00", 2, NOW))
		self.assertTrue(is_too_soon(DAY, "17:00", 2, NOW))  # boundary is inclusive
		self.assertFalse(is_too_soon(DAY, "18:00", 2, NOW))
		self.assertFalse(is_too_soon(TOMORROW, "08:00", 2, NOW))

	def test_lead_time_crosses_midnight(self):
		late = datetime.datetime.combine(DAY, datetime.time(23, 0))
		self.assertTrue(is_too_soon(TOMORROW, "00:30", 2, late))
		self.assertFalse(is_too_soon(TOMORROW, "01:30", 2, late))

	def test_zero_lead_time_still_blocks_past(self):
		self.assertTrue(is_too_soon(DAY, "15:00", 0, NOW))
		self.assertFalse(is_too_soon(DAY, "15:01", 0, NOW))


class TestDeliverySlots(DeliveryTestCase):
	def test_reserve_increments_counter_and_writes_ledger(self):
		area = self.make_area(cars=2)
		names = reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)

		self.assertEqual(len(names), 1)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 1)
		row = frappe.get_doc(engine.LEDGER, names[0])
		self.assertEqual((row.slot_type, row.status, row.qty, row.area), ("Delivery", "Reserved", 1, area))
		self.assertEqual(to_time_str(row.delivery_time), "10:00:00")
		slot = self.slot(area, TOMORROW, "10:00")
		self.assertEqual((slot["reserved"], slot["remaining"], slot["available"]), (1, 1, True))

	def test_capacity_reached_disables_slot_and_rejects_next(self):
		area = self.make_area(cars=2)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		self.assertFalse(self.capacity_block(area, TOMORROW, "10:00"))
		reserve(REF, self.ref("SO-2"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)

		self.assertTrue(self.capacity_block(area, TOMORROW, "10:00"))
		slot = self.slot(area, TOMORROW, "10:00")
		self.assertFalse(slot["available"])
		self.assertIn(engine.REASON_FULL, slot["reasons"])
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-3"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 2)
		# Other slots of the same day are untouched.
		self.assertTrue(self.slot(area, TOMORROW, "16:00")["available"])

	def test_release_reopens_full_slot(self):
		area = self.make_area(cars=1)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		self.assertTrue(self.capacity_block(area, TOMORROW, "10:00"))

		self.assertEqual(release(REF, self.ref("SO-1")), 1)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 0)
		self.assertFalse(self.capacity_block(area, TOMORROW, "10:00"))
		self.assertTrue(self.slot(area, TOMORROW, "10:00")["available"])
		self.assertEqual(get_reservations(REF, self.ref("SO-1")), [])
		self.assertEqual(get_reservations(REF, self.ref("SO-1"), status="Released")[0].slot_type, "Delivery")

	def test_release_is_idempotent_and_never_negative(self):
		area = self.make_area(cars=2)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		self.assertEqual(release(REF, self.ref("SO-1")), 1)
		self.assertEqual(release(REF, self.ref("SO-1")), 0)
		self.assertEqual(release(REF, "UNKNOWN"), 0)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 0)

	def test_same_document_cannot_reserve_twice(self):
		area = self.make_area(cars=5)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="16:00", now=NOW)
		self.assertEqual(self.counter(area, TOMORROW, "16:00"), None)

	def test_reschedule_moves_booking(self):
		area = self.make_area(cars=1)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		reschedule(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="16:00", now=NOW)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 0)
		self.assertEqual(self.counter(area, TOMORROW, "16:00"), 1)
		self.assertFalse(self.capacity_block(area, TOMORROW, "10:00"))
		self.assertTrue(self.capacity_block(area, TOMORROW, "16:00"))

	def test_reschedule_into_own_full_slot_works(self):
		area = self.make_area(cars=1)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		reschedule(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 1)

	def test_lead_time_blocks_listing_and_reserve(self):
		area = self.make_area(cars=3)
		slots = {s["time"]: s for s in get_delivery_slots(area, DAY, now=NOW)}
		self.assertIn(engine.REASON_TOO_SOON, slots["10:00"]["reasons"])
		self.assertIn(engine.REASON_TOO_SOON, slots["16:00"]["reasons"])
		self.assertIn(engine.REASON_TOO_SOON, slots["17:00"]["reasons"])
		self.assertTrue(slots["18:00"]["available"])
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=DAY, delivery_time="17:00", now=NOW)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=DAY, delivery_time="18:00", now=NOW)

	def test_lead_time_follows_settings(self):
		area = self.make_area(cars=3)
		self.settings(slot_lead_time_hours=4)
		self.assertIn(engine.REASON_TOO_SOON, self.slot(area, DAY, "18:00")["reasons"])
		self.settings(slot_lead_time_hours=0)
		self.assertTrue(self.slot(area, DAY, "16:00")["available"])

	def test_past_date_rejected(self):
		area = self.make_area(cars=3)
		yesterday = DAY - datetime.timedelta(days=1)
		self.assertFalse(any(s["available"] for s in get_delivery_slots(area, yesterday, now=NOW)))
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=yesterday, delivery_time="18:00", now=NOW)

	def test_weekday_without_work_hours(self):
		# Only Tuesday is a working day; DAY is a Monday.
		area = self.make_area(cars=3, days=("tuesday",))
		self.assertEqual(get_delivery_slots(area, DAY + datetime.timedelta(days=7), now=NOW), [])
		self.assertEqual(len(get_delivery_slots(area, TOMORROW, now=NOW)), 4)
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=DAY + datetime.timedelta(days=7), delivery_time="10:00", now=NOW)

	def test_unknown_hour_rejected(self):
		area = self.make_area(cars=3)
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="11:00", now=NOW)

	def test_disabled_area(self):
		area = self.make_area(cars=3, enabled=0)
		slot = self.slot(area, TOMORROW, "10:00")
		self.assertIn(engine.REASON_AREA_DISABLED, slot["reasons"])
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)

	def test_zero_cars_means_closed(self):
		area = self.make_area(cars=0)
		self.assertIn(engine.REASON_FULL, self.slot(area, TOMORROW, "10:00")["reasons"])
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)

	def test_unknown_area(self):
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area="_DM Nowhere", delivery_date=TOMORROW, delivery_time="10:00", now=NOW)

	def test_incomplete_request_rejected(self):
		area = self.make_area()
		self.assertRaises(frappe.ValidationError, reserve, REF, self.ref("SO-1"), now=NOW)
		self.assertRaises(frappe.ValidationError, reserve, REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, now=NOW)
		self.assertRaises(frappe.ValidationError, reserve, REF, None, area=area, delivery_date=TOMORROW,
			delivery_time="10:00", now=NOW)

	def test_cars_change_resyncs_future_slots(self):
		area = self.make_area(cars=1)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		self.assertTrue(self.capacity_block(area, TOMORROW, "10:00"))

		doc = frappe.get_doc("Delivery Area", area)
		doc.cars_number = 2
		doc.save()
		self.assertFalse(self.capacity_block(area, TOMORROW, "10:00"))
		self.assertTrue(self.slot(area, TOMORROW, "10:00")["available"])

		doc.cars_number = 1
		doc.save()
		self.assertTrue(self.capacity_block(area, TOMORROW, "10:00"))

	def test_lowering_cars_below_bookings_keeps_bookings(self):
		area = self.make_area(cars=3)
		for so in ("SO-1", "SO-2", "SO-3"):
			reserve(REF, self.ref(so), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		doc = frappe.get_doc("Delivery Area", area)
		doc.cars_number = 1
		doc.save()
		release(REF, self.ref("SO-1"))
		# 2 booked against 1 car: still full after one cancellation.
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 2)
		self.assertTrue(self.capacity_block(area, TOMORROW, "10:00"))
		release(REF, self.ref("SO-2"))
		self.assertTrue(self.capacity_block(area, TOMORROW, "10:00"))
		release(REF, self.ref("SO-3"))
		self.assertFalse(self.capacity_block(area, TOMORROW, "10:00"))

	def test_duplicate_work_hour_rejected(self):
		area = self.make_area(hours=("10:00",))
		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc({"doctype": "Delivery Work Hour", "area": area, "hour": "10:00:00"}).insert()


class TestManualBlocks(DeliveryTestCase):
	def test_manual_block_rejects_reserve(self):
		area = self.make_area(cars=3)
		set_slot_manual_block(area, TOMORROW, "10:00", True, "Driver off")
		slot = self.slot(area, TOMORROW, "10:00")
		self.assertTrue(slot["manually_disabled"])
		self.assertIn(engine.REASON_MANUAL, slot["reasons"])
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)

	def test_manual_block_survives_release_and_recompute(self):
		"""Old-system bug: a cancellation wiped the admin's manual disable."""
		area = self.make_area(cars=1)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		set_slot_manual_block(area, TOMORROW, "10:00", True)
		release(REF, self.ref("SO-1"))
		rebuild_from_ledger(DAY)
		self.assertTrue(self.capacity_block(area, TOMORROW, "10:00", source="Manual"))
		self.assertFalse(self.slot(area, TOMORROW, "10:00")["available"])

	def test_unblocking_full_slot_keeps_it_full(self):
		area = self.make_area(cars=1)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		set_slot_manual_block(area, TOMORROW, "10:00", True)
		set_slot_manual_block(area, TOMORROW, "10:00", False)
		slot = self.slot(area, TOMORROW, "10:00")
		self.assertEqual(slot["reasons"], [engine.REASON_FULL])

	def test_block_is_idempotent(self):
		area = self.make_area()
		set_slot_manual_block(area, TOMORROW, "10:00", True)
		set_slot_manual_block(area, TOMORROW, "10:00", True)
		set_slot_manual_block(area, TOMORROW, "10:00", False)
		set_slot_manual_block(area, TOMORROW, "10:00", False)
		self.assertTrue(self.slot(area, TOMORROW, "10:00")["available"])

	def test_force_bypasses_rules_and_flags_row(self):
		area = self.make_area(cars=1)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		set_slot_manual_block(area, TOMORROW, "10:00", True)
		names = reserve(area=area, delivery_date=TOMORROW, delivery_time="10:00", force=True, now=NOW,
			remarks="VIP", contact_phone="33334444")
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 2)
		self.assertEqual(frappe.db.get_value(engine.LEDGER, names[0], "forced"), 1)
		self.assertTrue(release_reservation(names[0]))
		self.assertFalse(release_reservation(names[0]))
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 1)


class TestAcCapacity(DeliveryTestCase):
	def test_units_not_orders_are_counted(self):
		self.settings(global_ac_daily_capacity=5)
		reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=3, now=NOW)
		self.assertEqual(self.ac_reserved(TOMORROW), 3)
		status = get_ac_day_status(TOMORROW, units=2, now=NOW)
		self.assertEqual((status["remaining"], status["available"]), (2, True))
		self.assertFalse(get_ac_day_status(TOMORROW, units=3, now=NOW)["available"])

	def test_order_bigger_than_remaining_is_rejected_whole(self):
		self.settings(global_ac_daily_capacity=20)
		reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=18, now=NOW)
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-2"), ac_date=TOMORROW, ac_units=3, now=NOW)
		self.assertEqual(self.ac_reserved(TOMORROW), 18)
		reserve(REF, self.ref("SO-3"), ac_date=TOMORROW, ac_units=2, now=NOW)
		status = get_ac_day_status(TOMORROW, now=NOW)
		self.assertEqual((status["reserved"], status["available"]), (20, False))
		self.assertIn(engine.REASON_FULL, status["reasons"])
		self.assertEqual(frappe.db.get_value(engine.AC_DAY, engine.ac_day_key(TOMORROW), "is_full"), 1)

	def test_single_order_with_many_units(self):
		self.settings(global_ac_daily_capacity=20)
		reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=20, now=NOW)
		self.assertFalse(get_ac_day_status(TOMORROW, now=NOW)["available"])

	def test_release_frees_exact_units_from_ledger(self):
		self.settings(global_ac_daily_capacity=5)
		reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=3, now=NOW)
		reserve(REF, self.ref("SO-2"), ac_date=TOMORROW, ac_units=2, now=NOW)
		release(REF, self.ref("SO-1"))
		self.assertEqual(self.ac_reserved(TOMORROW), 2)
		self.assertEqual(frappe.db.get_value(engine.AC_DAY, engine.ac_day_key(TOMORROW), "is_full"), 0)

	def test_date_override_beats_global(self):
		self.settings(global_ac_daily_capacity=5)
		set_ac_day_capacity(TOMORROW, 1)
		self.assertEqual(get_ac_day_status(TOMORROW, now=NOW)["capacity"], 1)
		reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=1, now=NOW)
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-2"), ac_date=TOMORROW, ac_units=1, now=NOW)
		# Other days still use the global value.
		self.assertEqual(get_ac_day_status(TOMORROW + datetime.timedelta(days=1), now=NOW)["capacity"], 5)
		set_ac_day_capacity(TOMORROW, None)
		self.assertEqual(get_ac_day_status(TOMORROW, now=NOW)["capacity"], 5)
		reserve(REF, self.ref("SO-2"), ac_date=TOMORROW, ac_units=1, now=NOW)

	def test_zero_capacity_closes_day(self):
		set_ac_day_capacity(TOMORROW, 0)
		self.assertFalse(get_ac_day_status(TOMORROW, units=1, now=NOW)["available"])
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=1, now=NOW)
		self.settings(global_ac_daily_capacity=0)
		self.assertFalse(get_ac_day_status(TOMORROW + datetime.timedelta(days=1), units=1, now=NOW)["available"])

	def test_negative_override_rejected(self):
		self.assertRaises(frappe.ValidationError, set_ac_day_capacity, TOMORROW, -1)

	def test_manual_block_survives_release(self):
		reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=1, now=NOW)
		set_ac_day_manual_block(TOMORROW, True)
		release(REF, self.ref("SO-1"))
		status = get_ac_day_status(TOMORROW, units=1, now=NOW)
		self.assertIn(engine.REASON_MANUAL, status["reasons"])
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-2"), ac_date=TOMORROW, ac_units=1, now=NOW)
		set_ac_day_manual_block(TOMORROW, False)
		reserve(REF, self.ref("SO-2"), ac_date=TOMORROW, ac_units=1, now=NOW)

	def test_global_capacity_change_resyncs(self):
		self.settings(global_ac_daily_capacity=2)
		reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=2, now=NOW)
		key = engine.ac_day_key(TOMORROW)
		self.assertEqual(frappe.db.get_value(engine.AC_DAY, key, "is_full"), 1)
		settings = frappe.get_single("Delivery Settings")
		settings.global_ac_daily_capacity = 4
		settings.flags.ignore_mandatory = True
		settings.save()
		self.assertEqual(frappe.db.get_value(engine.AC_DAY, key, ["is_full", "effective_capacity"]), (0, 4))

	def test_past_ac_day_rejected(self):
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-1"), ac_date=DAY - datetime.timedelta(days=1), ac_units=1, now=NOW)
		self.assertIn(engine.REASON_PAST, get_ac_day_status(DAY - datetime.timedelta(days=1), now=NOW)["reasons"])

	def test_zero_units_rejected(self):
		self.assertRaises(frappe.ValidationError, reserve, REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=0, now=NOW)

	def test_calendar(self):
		self.settings(global_ac_daily_capacity=1)
		reserve(REF, self.ref("SO-1"), ac_date=TOMORROW, ac_units=1, now=NOW)
		cal = get_ac_calendar(DAY, 3, units=1, now=NOW)
		self.assertEqual([d["available"] for d in cal], [True, False, True])


class TestMixedAndConsistency(DeliveryTestCase):
	def test_mixed_order_books_both(self):
		area = self.make_area(cars=2)
		names = reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00",
			ac_date=TOMORROW + datetime.timedelta(days=1), ac_units=2, now=NOW)
		self.assertEqual(len(names), 2)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 1)
		self.assertEqual(self.ac_reserved(TOMORROW + datetime.timedelta(days=1)), 2)
		self.assertEqual(release(REF, self.ref("SO-1")), 2)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 0)
		self.assertEqual(self.ac_reserved(TOMORROW + datetime.timedelta(days=1)), 0)

	def test_mixed_order_is_all_or_nothing(self):
		"""The delivery slot is full: the transaction rollback must also undo the AC booking."""
		area = self.make_area(cars=1)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		frappe.db.savepoint("before_mixed")
		with self.assertRaises(SlotUnavailableError):
			reserve(REF, self.ref("SO-2"), area=area, delivery_date=TOMORROW, delivery_time="10:00",
				ac_date=TOMORROW, ac_units=2, now=NOW)
		frappe.db.rollback(save_point="before_mixed")
		self.assertIn(self.ac_reserved(TOMORROW), (None, 0))
		self.assertEqual(get_reservations(REF, self.ref("SO-2")), [])

	def test_move_one_booking_of_a_mixed_order(self):
		area = self.make_area(cars=1)
		owner = self.ref("SO-1")
		reserve(REF, owner, area=area, delivery_date=TOMORROW, delivery_time="10:00", ac_date=TOMORROW, ac_units=2, now=NOW)
		ac_row = next(r for r in get_reservations(REF, owner) if r.slot_type == "AC")
		later = TOMORROW + datetime.timedelta(days=1)
		engine.move_reservation(ac_row.name, ac_date=later, now=NOW)
		self.assertEqual((self.ac_reserved(TOMORROW), self.ac_reserved(later)), (0, 2))
		# The delivery slot of the same order is untouched.
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 1)
		delivery_row = next(r for r in get_reservations(REF, owner) if r.slot_type == "Delivery")
		engine.move_reservation(delivery_row.name, area=area, delivery_date=TOMORROW, delivery_time="16:00", now=NOW)
		self.assertEqual((self.counter(area, TOMORROW, "10:00"), self.counter(area, TOMORROW, "16:00")), (0, 1))
		self.assertEqual(len(get_reservations(REF, owner)), 2)

	def test_failed_move_keeps_nothing_half_done(self):
		area = self.make_area(cars=1)
		owner, other = self.ref("SO-1"), self.ref("SO-2")
		reserve(REF, owner, area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		reserve(REF, other, area=area, delivery_date=TOMORROW, delivery_time="16:00", now=NOW)
		row = get_reservations(REF, owner)[0]
		frappe.db.savepoint("before_move")
		with self.assertRaises(SlotUnavailableError):
			engine.move_reservation(row.name, area=area, delivery_date=TOMORROW, delivery_time="16:00", now=NOW)
		frappe.db.rollback(save_point="before_move")
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 1)
		self.assertEqual(get_reservations(REF, owner)[0].name, row.name)

	def test_rebuild_from_ledger_repairs_counters(self):
		area = self.make_area(cars=2)
		reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", ac_date=TOMORROW,
			ac_units=3, now=NOW)
		reserve(REF, self.ref("SO-2"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		release(REF, self.ref("SO-2"))
		# Corrupt the caches.
		frappe.db.set_value(engine.COUNTER, engine.slot_key(area, TOMORROW, "10:00"), "reserved_count", 9)
		frappe.db.set_value(engine.AC_DAY, engine.ac_day_key(TOMORROW), "reserved_units", 0)
		rebuild_from_ledger(DAY)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 1)
		self.assertEqual(self.ac_reserved(TOMORROW), 3)
		self.assertFalse(self.capacity_block(area, TOMORROW, "10:00"))

	def test_concurrent_first_insert_is_handled(self):
		"""Simulate losing the race: our snapshot says "no row", but another transaction
		has already inserted it. The duplicate insert must be absorbed and the
		booking must count on top of the winner's."""
		area = self.make_area(cars=2)
		engine._insert(engine.COUNTER, engine.slot_key(area, TOMORROW, "10:00"), area=area,
			delivery_date=TOMORROW, delivery_time="10:00:00", reserved_count=1)

		with patch.object(engine, "_row_exists", return_value=False):
			reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)
		self.assertEqual(self.counter(area, TOMORROW, "10:00"), 2)
		self.assertEqual(frappe.db.count(engine.COUNTER, {"area": area}), 1)
		self.assertTrue(self.capacity_block(area, TOMORROW, "10:00"))

		# Same for the block row itself: writing it twice is harmless.
		engine._set_block_row("Capacity", area, TOMORROW, "10:00", True)
		self.assertEqual(frappe.db.count(engine.DISABLED, {"area": area, "source": "Capacity"}), 1)

	def test_ledger_row_cannot_be_deleted_while_reserved(self):
		area = self.make_area()
		name = reserve(REF, self.ref("SO-1"), area=area, delivery_date=TOMORROW, delivery_time="10:00", now=NOW)[0]
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc(engine.LEDGER, name)
