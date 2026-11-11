"""Split delivery: part of an order on another day as its own Delivery Note."""

import frappe
from frappe.utils import add_days, cint

from delivery_management.api import admin, drivers as api
from delivery_management.drivers import assignment as asg
from delivery_management.orders import split
from delivery_management.slots import engine
from delivery_management.orders.stock import held_for_split_notes
from delivery_management.tests.fixtures import send_out
from delivery_management.tests.test_orders import reserved_qty
from delivery_management.tests.test_transit import TransitTestCase

DN = "Delivery Note"


class SplitTestCase(TransitTestCase):
	def qty(self, name):
		qty = {}
		for r in frappe.get_doc(DN, name).items:
			qty[r.item_code] = qty.get(r.item_code, 0) + r.qty
		return qty

	def line(self, name, item_code):
		return next(r.name for r in frappe.get_doc(DN, name).items if r.item_code == item_code)

	def bookings(self, doctype, name):
		return {r.slot_type: r for r in engine.get_reservations(doctype, name)}

	def split_tv(self, name, qty=1, time="16:00"):
		return split.split(name, {self.line(name, self.w.tv): qty}, delivery_date=self.slot_date, delivery_time=time)


class TestSplitDelivery(SplitTestCase):
	def test_part_goes_to_a_new_note_of_the_same_order(self):
		_si, so = self.order([(self.w.tv, 2, 100), (self.w.pipe, 3, 5)])
		first = self.note(so)
		part = self.split_tv(first)

		self.assertEqual(self.qty(first), {self.w.tv: 1, self.w.pipe: 3})
		self.assertEqual(self.qty(part), {self.w.tv: 1})
		self.assertEqual(sorted(self.notes(so)), sorted([first, part]))  # the order lists both
		doc = frappe.get_doc(DN, part)
		self.assertEqual((doc.dm_split_from, doc.dm_delivery_type, doc.dm_driver), (first, "Non AC", None))
		self.assertEqual(str(doc.dm_delivery_time)[:5], "16:00")
		# Its own slot; the order keeps the first note's.
		self.assertEqual(str(self.bookings(DN, part)["Delivery"].delivery_time)[:5], "16:00")
		self.assertIn("Delivery", self.bookings("Sales Order", so))
		# The goods stay reserved for the order.
		self.assertEqual(reserved_qty(so, self.w.tv), 2)
		self.assertIn("Delivery split", frappe.get_all("Order Log", filters={"sales_order": so}, pluck="event"))

	def test_sending_one_part_keeps_the_rest_reserved(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		first = self.note(so)
		part = self.split_tv(first)

		send_out(first, {self.w.tv: ["_DMTV-M-2"]})
		# ERPNext keeps no reservation on the line while part 1 is out: the POS holds part 2.
		self.assertEqual(held_for_split_notes([self.w.tv], self.w.main), {self.w.tv: 1})
		api.mark_delivered_by_office(first)
		self.assertEqual(reserved_qty(so, self.w.tv), 1)
		self.assertEqual(held_for_split_notes([self.w.tv], self.w.main), {})

		send_out(part, {self.w.tv: ["_DMTV-M-3"]})
		self.assertEqual(reserved_qty(so, self.w.tv), 0)
		api.mark_delivered_by_office(part)
		self.assertEqual(frappe.db.get_value("Sales Order", so, "per_delivered"), 100)

	def test_part_coming_back_is_reserved_again_without_double(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		first = self.note(so)
		self.split_tv(first)
		send_out(first, {self.w.tv: ["_DMTV-M-2"]})
		api.bring_back_all(first)
		self.receive(first, {self.w.tv: ["_DMTV-M-2"]}, reserve_again=1)
		self.assertEqual(reserved_qty(so, self.w.tv), 2)

	def test_both_parts_out_one_comes_back(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		first = self.note(so)
		part = self.split_tv(first)
		send_out(first, {self.w.tv: ["_DMTV-M-2"]})
		send_out(part, {self.w.tv: ["_DMTV-M-3"]})
		api.bring_back_all(first)
		self.receive(first, {self.w.tv: ["_DMTV-M-2"]}, reserve_again=1)
		self.assertEqual(reserved_qty(so, self.w.tv), 0)  # part 2 is still out: not yet
		api.mark_delivered_by_office(part)  # would fail on a reservation in the Main Store
		self.assertEqual(reserved_qty(so, self.w.tv), 1)  # the part back in the store

	def test_part_goes_to_another_area(self):
		other = self.make_area("_DM Split Area", cars=1, hours=("10:00",))
		_si, so = self.order([(self.w.tv, 3, 100)])
		first = self.note(so)
		part = split.split(first, {self.line(first, self.w.tv): 1}, delivery_date=self.slot_date, delivery_time="10:00",
			address={"dm_delivery_area": other, "dm_block_no": "9", "dm_road_no": "", "dm_building_no": "77"})

		doc = frappe.get_doc(DN, part)
		self.assertEqual((doc.dm_delivery_area, doc.dm_block_no, doc.dm_road_no, doc.dm_building_no), (other, "9", None, "77"))
		self.assertEqual(frappe.db.get_value(DN, first, "dm_delivery_area"), self.area)  # the first note stays
		self.assertEqual(self.bookings(DN, part)["Delivery"].area, other)
		self.assertEqual(self.counter(other, self.slot_date, "10:00"), 1)
		# That area's only car is now taken.
		self.assertRaises(engine.SlotUnavailableError, split.split, first, {self.line(first, self.w.tv): 1},
			delivery_date=self.slot_date, delivery_time="10:00", address={"dm_delivery_area": other})

	def test_ac_units_move_to_the_new_day(self):
		_si, so = self.order([(self.w.ac, 2, 300)])
		first = self.note(so)
		ac_day = add_days(self.slot_date, 2)
		part = split.split(first, {self.line(first, self.w.ac): 1}, ac_date=ac_day)

		self.assertEqual(cint(self.bookings("Sales Order", so)["AC"].qty), 1)
		mine = self.bookings(DN, part)["AC"]
		self.assertEqual((cint(mine.qty), str(mine.delivery_date)), (1, str(ac_day)))
		self.assertEqual((frappe.db.get_value(DN, first, "dm_ac_units"), frappe.db.get_value(DN, part, "dm_ac_units")),
			(1, 1))
		self.assertEqual(str(frappe.db.get_value(DN, part, "dm_ac_installation_date")), str(ac_day))

	def test_merge_back(self):
		_si, so = self.order([(self.w.ac, 2, 300)])
		first = self.note(so)
		part = split.split(first, {self.line(first, self.w.ac): 1}, ac_date=add_days(self.slot_date, 2))
		self.assertEqual(split.merge(part), first)

		self.assertFalse(frappe.db.exists(DN, part))
		self.assertEqual(self.qty(first), {self.w.ac: 2})
		self.assertEqual(cint(self.bookings("Sales Order", so)["AC"].qty), 2)
		self.assertEqual(self.bookings(DN, part), {})

	def test_moving_the_order_slot_leaves_the_part_alone(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		first = self.note(so)
		part = self.split_tv(first)
		order_slot = self.bookings("Sales Order", so)["Delivery"]
		admin.move_reservation(order_slot.name, area=self.area, date=add_days(self.slot_date, 1), time="10:00")
		self.assertEqual(str(frappe.db.get_value(DN, first, "dm_delivery_date")), str(add_days(self.slot_date, 1)))
		self.assertEqual(str(frappe.db.get_value(DN, part, "dm_delivery_date")), str(self.slot_date))
		# The part's Move button moves its own booking.
		booking = admin.get_delivery_note_booking(part)
		self.assertEqual(booking["reservation"], self.bookings(DN, part)["Delivery"].name)
		admin.move_reservation(booking["reservation"], area=self.area, date=add_days(self.slot_date, 3), time="16:00")
		self.assertEqual(str(frappe.db.get_value(DN, part, "dm_delivery_date")), str(add_days(self.slot_date, 3)))

	def test_order_with_a_driver_cancelled_before_delivery(self):
		si, so = self.order([(self.w.tv, 1, 100)])
		name = self.note(so)
		driver = "_dm.split.driver@example.com"
		if not frappe.db.exists("User", driver):
			frappe.get_doc({"doctype": "User", "email": driver, "first_name": "Split", "send_welcome_email": 0}).insert()
		frappe.get_doc("User", driver).add_roles(asg.ROLE_TV)
		asg.assign(name, driver)
		self.give_back(si, {self.w.tv: (0, 1)})  # the Order Log row of the assignment does not block it
		self.assertFalse(frappe.db.exists(DN, name))

	def test_cancelled_before_delivery_from_both_notes(self):
		si, so = self.order([(self.w.tv, 2, 100)])
		first = self.note(so)
		part = self.split_tv(first)
		self.give_back(si, {self.w.tv: (0, 2)})
		self.assertFalse(frappe.db.exists(DN, part))
		self.assertEqual(self.bookings(DN, part), {})  # its slot is freed with it


class TestSplitRules(SplitTestCase):
	def test_what_cannot_be_split(self):
		_si, so = self.order([(self.w.tv, 2, 100), (self.w.service, 1, 5)])
		first = self.note(so)
		tv, fee = self.line(first, self.w.tv), self.line(first, self.w.service)
		when = {"delivery_date": self.slot_date, "delivery_time": "16:00"}
		self.assertRaisesRegex(frappe.ValidationError, "at least one", split.split, first, {tv: 2}, **when)
		self.assertRaisesRegex(frappe.ValidationError, "only goods", split.split, first, {fee: 1}, **when)
		self.assertRaisesRegex(frappe.ValidationError, "at most", split.split, first, {tv: 3}, **when)
		self.assertRaisesRegex(frappe.ValidationError, "whole", split.split, first, {tv: 0.5}, **when)
		self.assertRaisesRegex(frappe.ValidationError, "day and time", split.split, first, {tv: 1})

	def test_not_once_the_goods_left(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		first = self.note(so)
		send_out(first, {self.w.tv: ["_DMTV-M-2", "_DMTV-M-3"]})
		self.assertRaisesRegex(frappe.ValidationError, "already left", self.split_tv, first)

	def test_full_slot_is_refused(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		first = self.note(so)
		for _i in range(2):  # the area has 2 cars
			engine.reserve(area=self.area, delivery_date=self.slot_date, delivery_time="16:00")
		self.assertRaises(engine.SlotUnavailableError, self.split_tv, first)
		self.assertEqual(self.qty(first), {self.w.tv: 2})  # nothing changed

	def test_office_only(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		first = self.note(so)
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.PermissionError, self.split_tv, first)
		finally:
			frappe.set_user("Administrator")
