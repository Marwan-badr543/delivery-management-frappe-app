"""Cart classification, stock/serial allocation and the full POS Invoice/Order flows."""

import datetime

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
from frappe.utils import add_days, getdate, nowdate

from delivery_management.orders import address as address_utils
from delivery_management.orders import service
from delivery_management.orders.stock import get_sellable_qty
from delivery_management.slots import SlotUnavailableError, get_reservations, profile_cart
from delivery_management.slots import engine
from delivery_management.tests.fixtures import PosWorld
from delivery_management.tests.utils import DeliveryTestCase

DATA = {"total_change": 0, "paid_change": 0, "credit_change": 0, "redeemed_customer_credit": 0,
	"customer_credit_dict": [], "is_cashback": 1}


def serial_status(sn):
	return frappe.db.get_value("Serial No", sn, ["status", "warehouse"])


def reserved_serials(sales_order, item_code):
	"""Serials ERPNext's Stock Reservation holds for a Sales Order item."""
	return frappe.get_all("Serial and Batch Entry", filters={"parenttype": "Stock Reservation Entry",
		"parent": ["in", frappe.get_all("Stock Reservation Entry", filters={"voucher_no": sales_order,
			"item_code": item_code, "docstatus": 1, "status": ["!=", "Cancelled"]}, pluck="name") or [""]]},
		pluck="serial_no", order_by="serial_no")


class OrdersTestCase(DeliveryTestCase):
	def setUp(self):
		super().setUp()
		self.w = PosWorld()
		# Far ahead, so bookings already on the dev site never share a day with the tests.
		self.slot_date = getdate(add_days(nowdate(), 200))
		self.area = self.make_area("_DM Area", cars=2, hours=("10:00", "16:00"))
		self.delivery = {"area": self.area, "block_no": "305", "road_no": "512", "building_no": "1102",
			"delivery_date": str(self.slot_date), "delivery_time": "10:00",
			"ac_date": str(add_days(self.slot_date, 1))}


class TestCartProfile(OrdersTestCase):
	def test_ac_units_counted_in_units(self):
		p = profile_cart([{"item_code": self.w.ac, "qty": 3}])
		self.assertEqual((p.ac_units, p.has_ac, p.has_non_ac), (3, True, False))

	def test_uom_conversion_counts_stock_units(self):
		p = profile_cart([{"item_code": self.w.ac, "qty": 2, "conversion_factor": 2}])
		self.assertEqual(p.ac_units, 4)

	def test_non_ac_only(self):
		p = profile_cart([{"item_code": self.w.tv, "qty": 1}])
		self.assertEqual((p.has_ac, p.has_non_ac), (False, True))

	def test_mixed_cart_needs_both(self):
		p = profile_cart([{"item_code": self.w.ac, "qty": 1}, {"item_code": self.w.tv, "qty": 1}])
		self.assertEqual((p.ac_units, p.has_non_ac), (1, True))

	def test_excluded_group_and_item_never_use_ac_capacity(self):
		p = profile_cart([{"item_code": self.w.pipe, "qty": 5}, {"item_code": self.w.gas, "qty": 1}])
		self.assertEqual(p.ac_units, 0)
		# Without an AC unit, accessories travel as normal goods.
		self.assertTrue(p.has_non_ac)

	def test_accessories_ride_with_ac_installation(self):
		p = profile_cart([{"item_code": self.w.ac, "qty": 1}, {"item_code": self.w.pipe, "qty": 5},
			{"item_code": self.w.gas, "qty": 1}])
		self.assertEqual((p.ac_units, p.has_non_ac), (1, False))
		self.assertEqual(sorted(p.accessory_items), sorted([self.w.pipe, self.w.gas]))

	def test_non_stock_lines_need_no_slot(self):
		p = profile_cart([{"item_code": self.w.service, "qty": 1}])
		self.assertEqual((p.has_ac, p.has_non_ac), (False, False))

	def test_no_ac_group_configured(self):
		settings = frappe.get_single("Delivery Settings")
		settings.set("ac_item_groups", [])
		settings.set("ac_excluded_item_groups", [])
		settings.flags.ignore_mandatory = True
		settings.save()
		self.assertEqual(profile_cart([{"item_code": self.w.ac, "qty": 1}]).ac_units, 0)

	def test_excluded_group_must_be_under_ac_group(self):
		settings = frappe.get_single("Delivery Settings")
		settings.set("ac_excluded_item_groups", [{"item_group": "_DM Goods"}])
		self.assertRaises(frappe.ValidationError, settings.save)


class TestStock(OrdersTestCase):
	def test_sellable_qty_per_warehouse(self):
		order = get_sellable_qty([self.w.ac, self.w.tv, self.w.pipe, self.w.service], self.w.main)
		self.assertEqual((order[self.w.ac], order[self.w.tv], order[self.w.pipe]), (4, 3, 50))
		self.assertIsNone(order[self.w.service])
		branch = get_sellable_qty([self.w.ac, self.w.tv], self.w.branch)
		self.assertEqual((branch[self.w.ac], branch[self.w.tv]), (1, 2))

	def test_reserved_stock_is_not_sellable(self):
		frappe.db.set_value("Bin", {"item_code": self.w.ac, "warehouse": self.w.main}, "reserved_stock", 1)
		self.assertEqual(get_sellable_qty([self.w.ac], self.w.main)[self.w.ac], 3)

class TestAddress(OrdersTestCase):
	def test_same_address_is_reused_changed_address_is_added(self):
		first = address_utils.upsert_customer_address(self.w.customer, self.delivery)
		self.assertEqual(address_utils.upsert_customer_address(self.w.customer, dict(self.delivery)), first)
		changed = address_utils.upsert_customer_address(self.w.customer, {**self.delivery, "building_no": "77"})
		self.assertNotEqual(changed, first)
		self.assertTrue(frappe.db.exists("Address", first))
		prefill = address_utils.get_customer_delivery_address(self.w.customer)
		self.assertEqual(prefill["area"], self.area)
		self.assertEqual(frappe.db.get_value("Address", first, "is_shipping_address"), 1)

	def test_empty_address_creates_nothing(self):
		self.assertIsNone(address_utils.upsert_customer_address(self.w.customer, {}))


class TestInvoiceFlow(OrdersTestCase):
	def test_invoice_takes_serials_from_branch_and_books_no_slot(self):
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		result = service.submit_pos_invoice(invoice, DATA, {})
		si = frappe.get_doc("Sales Invoice", result["name"])

		self.assertEqual((si.docstatus, si.update_stock, si.dm_order_type), (1, 1, "Invoice"))
		self.assertEqual(si.items[0].warehouse, self.w.branch)
		self.assertEqual(get_serial_nos(si.items[0].serial_no), ["_DMTV-B-1"])
		self.assertEqual(serial_status("_DMTV-B-1")[1], None)  # delivered out of the branch
		self.assertEqual(frappe.db.count(engine.LEDGER, {"customer": self.w.customer}), 0)

	def test_invoice_out_of_branch_stock(self):
		invoice = self.w.draft_invoice([(self.w.ac, 2, 300)])  # branch has 1, main has 4
		with self.assertRaises(frappe.ValidationError):
			service.submit_pos_invoice(invoice, DATA, {})

	def test_invoice_saves_optional_address(self):
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		result = service.submit_pos_invoice(invoice, DATA, self.delivery)
		si = frappe.get_doc("Sales Invoice", result["name"])
		self.assertTrue(si.customer_address)
		self.assertEqual(si.dm_block_no, "305")


class TestOrderFlow(OrdersTestCase):
	def place(self, lines, delivery=None, discount=0):
		invoice = self.w.draft_invoice(lines, discount_percentage=discount)
		return service.submit_pos_order(invoice, DATA, delivery or self.delivery)

	def test_mixed_order_end_to_end(self):
		result = self.place([(self.w.ac, 2, 300), (self.w.tv, 1, 100), (self.w.pipe, 3, 2)])
		so = frappe.get_doc("Sales Order", result["sales_order"])
		si = frappe.get_doc("Sales Invoice", result["name"])

		# Sales Order from the main store, schedule stamped.
		self.assertEqual(so.docstatus, 1)
		self.assertEqual({i.warehouse for i in so.items}, {self.w.main})
		self.assertEqual((so.dm_delivery_area, str(so.dm_delivery_date)), (self.area, str(self.slot_date)))
		self.assertEqual((so.dm_ac_units, so.dm_order_type), (2, "Order"))
		self.assertEqual(len(reserved_serials(so.name, self.w.ac)), 2)

		# Invoice: no stock movement, linked line by line.
		self.assertEqual((si.docstatus, si.update_stock, si.dm_order_type), (1, 0, "Order"))
		for row in si.items:
			self.assertEqual((row.sales_order, row.warehouse), (so.name, self.w.main))
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "per_billed"), 100)

		# Capacity: one delivery slot and 2 AC units, both owned by the Sales Order.
		booked = {r.slot_type: r for r in get_reservations("Sales Order", so.name)}
		self.assertEqual(booked["AC"].qty, 2)
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 1)
		self.assertEqual(self.ac_reserved(add_days(self.slot_date, 1)), 2)

		# Address saved on the customer.
		self.assertEqual(frappe.db.get_value("Address", so.customer_address, "dm_road_no"), "512")


	def test_mixed_order_gets_one_delivery_note_per_type(self):
		self.settings(auto_create_delivery_note=1, auto_delivery_note_company=None)
		result = self.place([(self.w.ac, 2, 300), (self.w.tv, 1, 100), (self.w.pipe, 3, 2)])
		names = frappe.get_all("Delivery Note Item", filters={"against_sales_order": result["sales_order"]},
			pluck="parent", distinct=True)
		notes = {d.dm_delivery_type: d for d in (frappe.get_doc("Delivery Note", n) for n in names)}
		self.assertEqual(set(notes), {"AC", "Non AC"})
		ac, other = notes["AC"], notes["Non AC"]
		# AC units and their accessories with the AC Tech; the TV with the TV Driver.
		self.assertEqual(sorted(i.item_code for i in ac.items), sorted([self.w.ac, self.w.pipe]))
		self.assertEqual([i.item_code for i in other.items], [self.w.tv])
		# Each note keeps only its own schedule.
		self.assertEqual((str(ac.dm_ac_installation_date), ac.dm_ac_units, ac.dm_delivery_date, ac.dm_delivery_time),
			(str(add_days(self.slot_date, 1)), 2, None, None))
		self.assertEqual((str(other.dm_delivery_date), other.dm_ac_installation_date, other.dm_ac_units),
			(str(self.slot_date), None, 0))
		self.assertEqual({ac.dm_delivery_area, other.dm_delivery_area}, {self.area})
		# Every order line is on exactly one note.
		so = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertEqual(sorted(i.so_detail for n in notes.values() for i in n.items), sorted(i.name for i in so.items))

	def test_ac_only_order_needs_no_delivery_slot(self):
		result = self.place([(self.w.ac, 1, 300), (self.w.pipe, 2, 2)], {**self.delivery, "delivery_date": None,
			"delivery_time": None})
		booked = get_reservations("Sales Order", result["sales_order"])
		self.assertEqual([r.slot_type for r in booked], ["AC"])

	def test_non_ac_only_order_needs_no_ac_day(self):
		result = self.place([(self.w.tv, 1, 100)], {**self.delivery, "ac_date": None})
		booked = get_reservations("Sales Order", result["sales_order"])
		self.assertEqual([r.slot_type for r in booked], ["Delivery"])

	def test_missing_schedule_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.place([(self.w.ac, 1, 300)], {**self.delivery, "ac_date": None})
		with self.assertRaises(frappe.ValidationError):
			self.place([(self.w.tv, 1, 100)], {**self.delivery, "delivery_time": None})

	def test_missing_address_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.place([(self.w.tv, 1, 100)], {**self.delivery, "building_no": ""})

	def test_order_takes_stock_from_main_not_branch(self):
		# Branch has 1 AC; the main store has 4, so 3 must work for an Order.
		result = self.place([(self.w.ac, 3, 300)])
		self.assertEqual(len(reserved_serials(result["sales_order"], self.w.ac)), 3)
		self.assertEqual(get_sellable_qty([self.w.ac], self.w.branch)[self.w.ac], 1)

	def test_order_out_of_main_stock(self):
		with self.assertRaises(frappe.ValidationError):
			self.place([(self.w.ac, 5, 300)])

	def test_second_order_gets_different_serials(self):
		first = self.place([(self.w.ac, 1, 300)])
		second = self.place([(self.w.ac, 1, 300)], {**self.delivery, "delivery_time": "16:00"})
		s1 = reserved_serials(first["sales_order"], self.w.ac)
		s2 = reserved_serials(second["sales_order"], self.w.ac)
		self.assertEqual((len(s1), len(s2)), (1, 1))
		self.assertNotEqual(s1, s2)
		# Main store has 4 ACs; 2 are reserved now.
		self.assertEqual(get_sellable_qty([self.w.ac], self.w.main)[self.w.ac], 2)

	def test_full_slot_leaves_nothing_behind(self):
		self.make_area("_DM Area", cars=0, hours=("10:00",))
		frappe.db.savepoint("before_order")
		with self.assertRaises(SlotUnavailableError):
			self.place([(self.w.tv, 1, 100)])
		frappe.db.rollback(save_point="before_order")
		self.assertEqual(frappe.db.count("Sales Order", {"customer": self.w.customer, "docstatus": 1}), 0)
		self.assertEqual(frappe.db.count(engine.LEDGER, {"customer": self.w.customer}), 0)

	def test_discount_and_tax_do_not_break_billing(self):
		result = self.place([(self.w.tv, 2, 100)], discount=10)
		si = frappe.get_doc("Sales Invoice", result["name"])
		so = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertEqual(si.grand_total, so.grand_total)
		self.assertEqual(so.per_billed, 100)

	def test_cancel_releases_capacity_and_serials(self):
		result = self.place([(self.w.ac, 1, 300), (self.w.tv, 1, 100)])
		so_name = result["sales_order"]
		# Sites running the production server script get a draft DN that holds the
		# serials; deleting it is the normal first step of cancelling an order.
		for dn in frappe.get_all("Delivery Note Item", filters={"against_sales_order": so_name}, pluck="parent"):
			frappe.delete_doc("Delivery Note", dn, force=True)
		frappe.get_doc("Sales Invoice", result["name"]).cancel()
		frappe.get_doc("Sales Order", so_name).cancel()

		self.assertEqual(get_reservations("Sales Order", so_name), [])
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 0)
		self.assertEqual(self.ac_reserved(add_days(self.slot_date, 1)), 0)
		self.assertEqual(reserved_serials(so_name, self.w.ac), [])

	def test_return_invoice_frees_capacity(self):
		"""Production cancel path: a POS return; order_cancellation then cancels the SO."""
		if "order_cancellation" not in frappe.get_installed_apps():
			self.skipTest("order_cancellation not installed")
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		result = self.place([(self.w.ac, 1, 300), (self.w.tv, 1, 100)])
		so_name = result["sales_order"]
		for dn in frappe.get_all("Delivery Note Item", filters={"against_sales_order": so_name}, pluck="parent"):
			frappe.delete_doc("Delivery Note", dn, force=True)
		ret = make_sales_return(result["name"])
		ret.update_stock = 0
		ret.insert()
		ret.submit()

		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 2)
		self.assertEqual(get_reservations("Sales Order", so_name), [])
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 0)
		self.assertEqual(self.ac_reserved(add_days(self.slot_date, 1)), 0)

	def test_background_submission_profile_still_submits_now(self):
		frappe.db.set_value("POS Profile", self.w.pos_profile, "posa_allow_submissions_in_background_job", 1)
		result = self.place([(self.w.tv, 1, 100)], {**self.delivery, "ac_date": None})
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], "docstatus"), 1)
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		res = service.submit_pos_invoice(invoice, DATA, {})
		self.assertEqual(frappe.db.get_value("Sales Invoice", res["name"], "docstatus"), 1)

	def test_order_from_other_company_profile_is_refused(self):
		"""A POS profile of another company (e.g. an old dev shift) must not create cross-company orders."""
		other = frappe.db.get_value("Warehouse", {"company": ["!=", self.w.company.name], "is_group": 0}, "name")
		if not other:
			self.skipTest("needs a second company")
		self.settings(main_warehouse=other)
		with self.assertRaisesRegex(frappe.ValidationError, "belongs to company"):
			self.place([(self.w.tv, 1, 100)])

	def test_salesman_is_the_logged_in_user(self):
		user = frappe.get_doc({"doctype": "User", "email": "_dm_cashier@example.com", "first_name": "Cashier",
			"send_welcome_email": 0, "roles": [{"role": "Sales User"}, {"role": "Accounts User"},
			{"role": "Stock User"}, {"role": "Sales Manager"}]}).insert(ignore_permissions=True)
		employee = frappe.get_doc({"doctype": "Employee", "first_name": "_DM Cashier", "company": self.w.company.name,
			"gender": frappe.db.get_value("Gender", {}, "name"), "date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01", "user_id": user.name, "status": "Active"}).insert(ignore_permissions=True)
		sales_person = frappe.get_doc({"doctype": "Sales Person", "sales_person_name": "_DM Cashier",
			"parent_sales_person": "Sales Team", "employee": employee.name}).insert(ignore_permissions=True)
		frappe.set_user(user.name)
		self.assertEqual(service.sales_team_of_current_user()[0]["sales_person"], sales_person.name)
		frappe.set_user("Administrator")
		self.assertEqual(service.sales_team_of_current_user(), [])

	def test_order_carries_sales_team(self):
		from unittest.mock import patch

		team = [{"sales_person": "Sales Team", "allocated_percentage": 100}]
		with patch.object(service, "sales_team_of_current_user", return_value=team):
			result = self.place([(self.w.tv, 1, 100)], {**self.delivery, "ac_date": None})
		self.assertEqual(frappe.db.get_value("Sales Team", {"parent": result["sales_order"]}, "sales_person"), "Sales Team")

	def test_returns_are_refused_as_orders(self):
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		invoice["is_return"] = 1
		self.assertRaises(frappe.ValidationError, service.submit_pos_order, invoice, DATA, self.delivery)


class TestWorkHourGuard(OrdersTestCase):
	def test_cannot_remove_slot_with_future_bookings(self):
		result = self.place_tv()
		wh = frappe.get_doc("Delivery Work Hour", {"area": self.area, "hour": "10:00:00"})
		weekday = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[self.slot_date.weekday()]
		wh.set(weekday, 0)
		self.assertRaises(frappe.ValidationError, wh.save)
		self.assertRaises(frappe.ValidationError, frappe.delete_doc, "Delivery Work Hour", wh.name)
		self.assertTrue(result)

	def place_tv(self):
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		return service.submit_pos_order(invoice, DATA, {**self.delivery, "ac_date": None})


class TestUnpaidOrder(OrdersTestCase):
	"""POS "Save/New" on an Order: everything submitted, nothing paid."""

	def test_save_new_submits_order_and_unpaid_invoice(self):
		invoice = self.w.draft_invoice([(self.w.ac, 1, 300), (self.w.tv, 1, 100)])
		result = service.submit_pos_order(invoice, dict(DATA), self.delivery, unpaid=True)
		so = frappe.get_doc("Sales Order", result["sales_order"])
		si = frappe.get_doc("Sales Invoice", result["name"])

		self.assertEqual(so.docstatus, 1)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(si.paid_amount, 0)
		self.assertEqual(si.outstanding_amount, si.rounded_total or si.grand_total)
		self.assertIn(si.status, ("Unpaid", "Overdue"))
		self.assertEqual({r.slot_type for r in get_reservations("Sales Order", so.name)}, {"AC", "Delivery"})


class TestClientSerialsIgnored(OrdersTestCase):
	"""POS Awesome V15 pre-fills serials in the browser; ERPNext picks them instead."""

	def test_order_ignores_serials_sent_by_the_pos(self):
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		invoice["items"][0]["serial_no"] = "_DMTV-B-1"  # a branch serial: Orders ship from the main store
		result = service.submit_pos_order(invoice, dict(DATA), self.delivery)
		self.assertTrue(reserved_serials(result["sales_order"], self.w.tv)[0].startswith("_DMTV-M-"))


class TestQuietOrderMessages(OrdersTestCase):
	"""The SO -> DN server script's msgprint must not reach the POS (it froze V15)."""

	def test_order_submit_leaves_no_popup(self):
		frappe.local.message_log = []
		frappe.get_doc({"doctype": "Server Script", "name": "_dm_test_popup", "script_type": "DocType Event",
			"reference_doctype": "Sales Order", "doctype_event": "After Submit",
			"script": "frappe.msgprint('Delivery Note has been created')"}).insert()
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		service.submit_pos_order(invoice, dict(DATA), self.delivery)
		self.assertFalse([m for m in frappe.local.message_log if "Delivery Note has been created" in str(m)])


class TestRetriedSubmit(OrdersTestCase):
	"""A POS retry (same request id) must not make a second order, booking or invoice."""

	def test_retry_returns_the_first_order(self):
		from delivery_management.api.pos import submit

		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		invoice["posa_client_request_id"] = "dm-retry-test-1"
		first = submit(frappe.as_json(invoice), frappe.as_json(DATA), "Order", frappe.as_json(self.delivery))
		again = submit(frappe.as_json(invoice), frappe.as_json(DATA), "Order", frappe.as_json(self.delivery))

		self.assertEqual((again["name"], again["sales_order"]), (first["name"], first["sales_order"]))
		self.assertEqual(frappe.db.count("Sales Order", {"customer": self.w.customer, "docstatus": 1}), 1)
		self.assertEqual(len(get_reservations("Sales Order", first["sales_order"])), 1)
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 1)


class TestSalesOrderSchedule(OrdersTestCase):
	"""Schedule fields on a Sales Order made in the Desk: optional, booked on submit."""

	def make_so(self, item=None, qty=1, **schedule):
		so = frappe.get_doc({
			"doctype": "Sales Order", "customer": self.w.customer, "company": self.w.company.name,
			"transaction_date": nowdate(), "delivery_date": add_days(nowdate(), 5),
			"set_warehouse": self.w.main,
			"items": [{"item_code": item or self.w.tv, "qty": qty, "rate": 100, "warehouse": self.w.main}],
			**schedule,
		})
		return so.insert()

	def slot(self, time="10:00"):
		return {"dm_delivery_area": self.area, "dm_delivery_date": self.slot_date, "dm_delivery_time": time}

	def test_no_schedule_books_nothing(self):
		so = self.make_so()
		so.submit()
		self.assertEqual(get_reservations("Sales Order", so.name), [])

	def test_submit_books_slot_and_cancel_releases(self):
		so = self.make_so(**self.slot())
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), None)  # nothing held while draft
		so.submit()
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 1)
		so.cancel()
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 0)

	def test_taken_slot_cannot_be_chosen(self):
		self.make_area("_DM Area", cars=1, hours=("10:00", "16:00"))
		self.make_so(**self.slot()).submit()
		with self.assertRaises(SlotUnavailableError):
			self.make_so(**self.slot())
		self.make_so(**self.slot("16:00"))  # a free slot is fine

	def test_not_a_working_hour_is_refused(self):
		with self.assertRaises(SlotUnavailableError):
			self.make_so(**self.slot("13:00"))

	def test_half_filled_schedule_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_so(dm_delivery_date=self.slot_date)
		with self.assertRaises(frappe.ValidationError):
			self.make_so(dm_delivery_date=self.slot_date, dm_delivery_time="10:00")
		self.make_so(dm_delivery_area=self.area)  # the area alone is just the address

	def test_ac_day_booked_with_units_from_items(self):
		ac_date = add_days(self.slot_date, 1)
		so = self.make_so(item=self.w.ac, qty=2, dm_ac_installation_date=ac_date, dm_ac_units=99)
		self.assertEqual(so.dm_ac_units, 2)
		so.submit()
		self.assertEqual(self.ac_reserved(ac_date), 2)

	def test_ac_day_without_ac_items_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_so(dm_ac_installation_date=add_days(self.slot_date, 1))

	def test_full_ac_day_cannot_be_chosen(self):
		self.settings(global_ac_daily_capacity=1)
		with self.assertRaises(SlotUnavailableError):
			self.make_so(item=self.w.ac, qty=2, dm_ac_installation_date=add_days(self.slot_date, 1))

	def test_slot_taken_between_save_and_submit(self):
		self.make_area("_DM Area", cars=1, hours=("10:00",))
		first, second = self.make_so(**self.slot()), self.make_so(**self.slot())
		first.submit()
		with self.assertRaises(SlotUnavailableError):
			second.submit()

	def test_block_road_building_digits_only(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_so(dm_block_no="12a")
		so = self.make_so(dm_block_no="12", dm_road_no="٥", dm_building_no="٩٠٠")  # Arabic digits
		self.assertEqual((so.dm_block_no, so.dm_road_no, so.dm_building_no), ("12", "5", "900"))

	def test_schedule_options_list_only_free_slots(self):
		from delivery_management.orders.sales_order import get_schedule_options

		self.make_area("_DM Area", cars=1, hours=("10:00", "16:00"))
		self.make_so(**self.slot()).submit()
		options = get_schedule_options(self.area, str(self.slot_date), [{"item_code": self.w.ac, "qty": 1}])
		self.assertEqual([s["time"] for s in options["slots"]], ["16:00"])
		self.assertEqual(options["ac_units"], 1)
		self.assertTrue(all(d["available"] for d in options["ac_days"]))


class TestAddressDetails(OrdersTestCase):
	def test_title_is_part_of_the_match(self):
		home = address_utils.upsert_customer_address(self.w.customer, {**self.delivery, "address_title": "Home"})
		self.assertEqual(frappe.db.get_value("Address", home, "address_title"), "Home")
		self.assertEqual(address_utils.upsert_customer_address(self.w.customer, {**self.delivery, "address_title": "home"}), home)
		office = address_utils.upsert_customer_address(self.w.customer, {**self.delivery, "address_title": "Office"})
		self.assertNotEqual(office, home)

	def test_blank_title_defaults_to_customer_name(self):
		name = address_utils.upsert_customer_address(self.w.customer, self.delivery)
		self.assertEqual(frappe.db.get_value("Address", name, "address_title"), self.w.customer)
		self.assertEqual(frappe.db.get_value("Address", name, "address_line1"), f"{self.area}, block 305, road 512, building 1102")

	def test_arabic_digits_match_ascii(self):
		ascii_name = address_utils.upsert_customer_address(self.w.customer, self.delivery)
		arabic = {**self.delivery, "block_no": "٣٠٥"}  # 305
		self.assertEqual(address_utils.upsert_customer_address(self.w.customer, arabic), ascii_name)

	def test_non_digit_numbers_refused(self):
		with self.assertRaises(frappe.ValidationError):
			address_utils.upsert_customer_address(self.w.customer, {**self.delivery, "road_no": "5b"})

	def test_old_site_text_address_prefills_and_is_reused(self):
		legacy = frappe.get_doc({
			"doctype": "Address", "address_title": "customer address", "address_type": "Shipping",
			"address_line1": f"{self.area.upper()}, block 1034, road 3479, building 3105", "city": self.area,
			"country": address_utils._default_country(), "is_shipping_address": 1,
			"links": [{"link_doctype": "Customer", "link_name": self.w.customer}],
		}).insert()
		prefill = address_utils.get_customer_delivery_address(self.w.customer)
		self.assertEqual(
			(prefill["address_name"], prefill["address_title"], prefill["area"], prefill["block_no"],
				prefill["road_no"], prefill["building_no"]),
			(legacy.name, "customer address", self.area, "1034", "3479", "3105"),
		)
		same = address_utils.upsert_customer_address(self.w.customer, {
			"address_title": "customer address", "area": self.area, "block_no": "1034", "road_no": "3479",
			"building_no": "3105"})
		self.assertEqual(same, legacy.name)
		self.assertEqual(frappe.db.get_value("Address", legacy.name, "dm_block_no"), "1034")

	def test_prefill_prefers_last_order_address(self):
		first = self.w.draft_invoice([(self.w.tv, 1, 100)])
		service.submit_pos_order(first, dict(DATA), {**self.delivery, "ac_date": None, "address_title": "Office"})
		# A newer, unused address of the customer must not win over the one last ordered to.
		address_utils.upsert_customer_address(self.w.customer, {**self.delivery, "address_title": "Other", "block_no": "9"})
		self.assertEqual(address_utils.get_customer_delivery_address(self.w.customer)["address_title"], "Office")


class TestSeveralAcGroups(OrdersTestCase):
	def test_items_of_any_ac_group_count(self):
		from delivery_management.tests.fixtures import item, item_group

		item_group("_DM AC Second")
		portable = item("_DM Portable AC", "_DM AC Second", serial=0)
		self.assertEqual(profile_cart([{"item_code": portable, "qty": 1}]).ac_units, 0)
		settings = frappe.get_single("Delivery Settings")
		settings.append("ac_item_groups", {"item_group": "_DM AC Second"})
		settings.save()
		p = profile_cart([{"item_code": portable, "qty": 2}, {"item_code": self.w.ac, "qty": 1}])
		self.assertEqual(p.ac_units, 3)


class TestProductBundle(OrdersTestCase):
	"""A bundle parent is a non-stock item: stock and the delivery are counted by its components."""

	def setUp(self):
		super().setUp()
		self.bundle = "_DM TV + Pipe Bundle"
		if not frappe.db.exists("Item", self.bundle):
			frappe.get_doc({"doctype": "Item", "item_code": self.bundle, "item_name": self.bundle,
				"item_group": "_DM Goods", "stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1,
				"include_item_in_manufacturing": 0}).insert()
		if not frappe.db.exists("Product Bundle", self.bundle):
			frappe.get_doc({"doctype": "Product Bundle", "new_item_code": self.bundle,
				"items": [{"item_code": self.w.tv, "qty": 1}, {"item_code": self.w.pipe, "qty": 2}]}).insert()

	def test_bundle_order_is_delivered_by_its_components(self):
		invoice = self.w.draft_invoice([(self.bundle, 1, 150)])
		result = service.submit_pos_order(invoice, DATA, {**self.delivery, "ac_date": None})
		so = frappe.get_doc("Sales Order", result["sales_order"])
		# The components need a delivery car.
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 1)

		dn = make_delivery_note(so.name)
		dn.insert()
		self.assertEqual(dn.dm_delivery_type, "Non AC")
		dn.submit()
		self.assertEqual(frappe.db.get_value("Serial No", "_DMTV-M-1", "warehouse"), None)

	def bundle_order(self, lines=None):
		invoice = self.w.draft_invoice(lines or [(self.bundle, 1, 150)])
		return service.submit_pos_order(invoice, DATA, {**self.delivery, "ac_date": None})["sales_order"]

	def reserved_qty(self, sales_order, item_code):
		return sum(frappe.get_all("Stock Reservation Entry", filters={"voucher_no": sales_order,
			"item_code": item_code, "docstatus": 1}, pluck="reserved_qty"))

	def test_bundle_order_reserves_its_components(self):
		before = get_sellable_qty([self.w.tv, self.w.pipe], self.w.main)
		so = self.bundle_order()
		self.assertEqual(reserved_serials(so, self.w.tv), ["_DMTV-M-1"])
		self.assertEqual(self.reserved_qty(so, self.w.pipe), 2)
		after = get_sellable_qty([self.w.tv, self.w.pipe], self.w.main)
		self.assertEqual((before[self.w.tv] - after[self.w.tv], before[self.w.pipe] - after[self.w.pipe]), (1, 2))

	def test_two_bundle_orders_never_share_a_serial(self):
		first, second = self.bundle_order(), self.bundle_order()
		self.assertEqual((reserved_serials(first, self.w.tv), reserved_serials(second, self.w.tv)),
			(["_DMTV-M-1"], ["_DMTV-M-2"]))

	def test_mixed_order_reserves_items_and_components(self):
		so = self.bundle_order([(self.w.tv, 1, 100), (self.bundle, 1, 150)])
		self.assertEqual(reserved_serials(so, self.w.tv), ["_DMTV-M-1", "_DMTV-M-2"])

	def test_delivery_note_delivers_the_reserved_serial(self):
		self.bundle_order()  # holds _DMTV-M-1
		so = self.bundle_order()
		dn = make_delivery_note(so)
		dn.insert()
		tv_row = next(r for r in dn.packed_items if r.item_code == self.w.tv)
		self.assertEqual(get_serial_nos(tv_row.serial_no), ["_DMTV-M-2"])
		reserved = frappe.db.get_value("Bin", {"item_code": self.w.tv, "warehouse": self.w.main}, "reserved_stock")

		dn.submit()
		self.assertEqual(frappe.db.get_value("Serial No", "_DMTV-M-2", "warehouse"), None)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.w.tv, "warehouse": self.w.main},
			"reserved_stock"), reserved - 1)
		self.assertEqual(set(frappe.get_all("Stock Reservation Entry", filters={"voucher_no": so, "docstatus": 1},
			pluck="status")), {"Delivered"})

		dn.cancel()
		self.assertEqual(frappe.db.get_value("Serial No", "_DMTV-M-2", "warehouse"), self.w.main)
		self.assertEqual(set(frappe.get_all("Stock Reservation Entry", filters={"voucher_no": so, "docstatus": 1},
			pluck="status")), {"Reserved"})

	def test_cancelled_bundle_order_frees_its_serial(self):
		invoice = self.w.draft_invoice([(self.bundle, 1, 150)])
		result = service.submit_pos_order(invoice, DATA, {**self.delivery, "ac_date": None})
		so = result["sales_order"]
		frappe.get_doc("Sales Invoice", result["name"]).cancel()
		frappe.get_doc("Sales Order", so).cancel()
		self.assertEqual(reserved_serials(so, self.w.tv), [])
		self.assertEqual(reserved_serials(self.bundle_order(), self.w.tv), ["_DMTV-M-1"])

	def test_bundle_branch_invoice_picks_component_serials(self):
		invoice = self.w.draft_invoice([(self.bundle, 1, 150)])
		result = service.submit_pos_invoice(invoice, DATA)
		si = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(frappe.db.get_value("Serial No", "_DMTV-B-1", "warehouse"), None)

	def test_bundle_stock_checked_by_components(self):
		# Main store has 3 TVs: 4 bundles cannot be sold.
		invoice = self.w.draft_invoice([(self.bundle, 4, 150)])
		self.assertRaises(frappe.ValidationError, service.submit_pos_order, invoice, DATA,
			{**self.delivery, "ac_date": None})


class TestDriverPayment(OrdersTestCase):
	def test_driver_collects_payment_on_delivery(self):
		from delivery_management.api import drivers as api
		from delivery_management.drivers import assignment as asg

		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		result = service.submit_pos_order(invoice, dict(DATA), {**self.delivery, "ac_date": None}, unpaid=True)
		dn = make_delivery_note(result["sales_order"])
		dn.insert()
		driver = "_dm.pay.driver@example.com"
		if not frappe.db.exists("User", driver):
			frappe.get_doc({"doctype": "User", "email": driver, "first_name": "Pay", "send_welcome_email": 0}).insert()
		frappe.get_doc("User", driver).add_roles(asg.ROLE_TV)
		asg.assign(dn.name, driver)
		mop = api.modes_of_payment(dn.company)[0]

		frappe.set_user(driver)
		self.assertRaises(frappe.ValidationError, api.record_payment, dn.name, 100.5, mop)
		out = api.record_payment(dn.name, 40, mop)
		self.assertEqual((out["payment"]["paid"], out["payment"]["outstanding"]), (40, 60))
		self.assertEqual(frappe.db.get_value("Payment Entry", out["payment_entry"], ["docstatus", "owner"]), (1, driver))
		self.assertEqual(frappe.session.user, driver)
		frappe.set_user("Administrator")

