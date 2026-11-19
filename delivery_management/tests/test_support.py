"""Support journey: ticket -> pickup trip -> transit store -> workshop -> return trip -> customer.
The trips are made by TV Drivers. The customer's item moves at zero value and never without
its serial."""

import base64

import frappe
from frappe.utils import flt

from delivery_management.api import control
from delivery_management.api import drivers as drivers_api
from delivery_management.api import support as api
from delivery_management.customer_support import tickets as t
from delivery_management.orders import service
from delivery_management.slots import get_reservations
from delivery_management.tests.fixtures import receive, warehouse
from delivery_management.tests.test_orders import DATA, OrdersTestCase

PNG = base64.b64encode(b"\x89PNG proof").decode()


def user(email, *roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0, "user_type": "System User"}).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles(*roles)
	return email


class SupportTestCase(OrdersTestCase):
	def setUp(self):
		super().setUp()
		self.cs = user("_dm_cs@example.com", "Support Team")
		self.tech1 = user("_dm_tech1@example.com", "TV Driver")
		self.tech2 = user("_dm_tech2@example.com", "TV Driver")
		self.maint = user("_dm_maint@example.com", "Maintenance Technician")
		self.office = user("_dm_office@example.com", "Delivery Manager")
		self.phone = "39990001"
		frappe.db.set_value("Customer", self.w.customer, "mobile_no", self.phone)
		frappe.db.set_value("Item", self.w.tv, "warranty_period", 365)
		if not frappe.db.exists("Sales Person", "_DM Service"):
			frappe.get_doc({"doctype": "Sales Person", "sales_person_name": "_DM Service",
				"parent_sales_person": frappe.db.get_value("Sales Person", {"is_group": 1}, "name")}).insert()
		self.our_transit = frappe.db.get_single_value("Delivery Settings", "transit_warehouse")
		self.transit = warehouse("_DM Customer Transit")
		self.workshop = warehouse("_DM Workshop")
		self.cstore = warehouse("_DM Customer Store")
		self.settings(support_maint_service_person="_DM Service", support_workshop_warehouse=self.workshop,
			support_transit_warehouse=self.transit, support_store_warehouse=self.cstore)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def as_(self, who, fn, *args, **kwargs):
		frappe.set_user(who)
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def sold_tv(self):
		"""A TV sold at the branch: (invoice, its serial, now with the customer)."""
		name = service.submit_pos_invoice(self.w.draft_invoice([(self.w.tv, 1, 100)]), DATA, {})["name"]
		serial = t.delivered_serials("Sales Invoice", name, self.w.tv)[0]
		return name, serial

	def open_ticket(self, as_user=None, **extra):
		data = {"subject": "Black screen", "customer_phone": self.phone, "priority": "High",
			"issue_type": "_DM Black Screen", "description": "TV shows nothing", "report": "Asked to restart, still black",
			"item_code": self.w.tv, "area": self.area, "block_no": "305", "road_no": "512", "building_no": "1102",
			"address": "Flat 3, near the mosque", **extra}
		return self.as_(as_user or self.cs, api.create_ticket, frappe.as_json(data))["name"]

	def book(self, name, time="10:00"):
		self.as_(self.cs, api.book_trip, name, self.area, str(self.slot_date), time, "Called the customer, booked")

	def at_pickup(self, tech=None, **extra):
		"""Booked and a driver assigned."""
		name = self.open_ticket(**extra)
		self.book(name)
		self.as_(self.office, api.assign_driver, name, tech or self.tech1)
		return name

	def step(self, name):
		return frappe.db.get_value("Issue", name, "sm_step")

	def proof(self, name, kind, who, filename="proof.png"):
		self.as_(who, api.upload_file, name, kind, filename, PNG)

	def to_workshop(self, name, serial=None):
		self.as_(self.tech1, api.fetch_item, name, serial, "Took it")
		self.as_(self.maint, api.receive_in_workshop, name, serial)

	def ready(self, name, serial=None):
		self.to_workshop(name, serial)
		self.proof(name, "repair", self.maint)
		self.as_(self.maint, api.finish_repair, name, "Repaired", "Board replaced, tested 2h")

	def out_for_return(self, name, serial=None):
		self.ready(name, serial)
		self.book(name, "16:00")
		self.as_(self.office, api.assign_driver, name, self.tech2)
		self.as_(self.maint, api.send_out, name, serial)


class TestCustomerService(SupportTestCase):
	def test_ticket_starts_with_customer_service(self):
		name = self.open_ticket()
		issue = frappe.get_doc("Issue", name)
		self.assertEqual((issue.customer, issue.sm_step, issue.status, issue.sm_item_code, issue.sm_area),
			(self.w.customer, "Customer Service", "Replied", self.w.tv, self.area))
		self.assertEqual(api.full_address(issue), f"{self.area}, Block 305, Road 512, Building 1102, Flat 3, near the mosque")
		self.assertEqual(frappe.db.get_value("Order Log", {"support_ticket": name}, "event"), "Ticket opened")

	def test_item_and_address_required(self):
		self.assertRaises(frappe.ValidationError, self.open_ticket, item_code="")
		for part in ("area", "block_no", "road_no", "building_no", "address"):
			self.assertRaises(frappe.ValidationError, self.open_ticket, **{part: ""})

	def test_warranty_from_the_serial_or_the_invoice(self):
		invoice, serial = self.sold_tv()
		name = self.open_ticket(sales_invoice=invoice)
		row = api._row(frappe.get_doc("Issue", name), {})
		self.assertTrue(row["under_warranty"])  # invoice today + 365 days
		frappe.db.set_value("Serial No", serial, "warranty_expiry_date", "2020-01-01")
		old = self.open_ticket(sales_invoice=invoice, serial_no=serial)
		self.assertFalse(api._row(frappe.get_doc("Issue", old), {})["under_warranty"])

	def test_lookup_gives_last_address_and_serials(self):
		invoice, serial = self.sold_tv()
		frappe.db.set_value("Sales Invoice", invoice, {"dm_delivery_area": self.area, "dm_block_no": "9",
			"dm_road_no": "8", "dm_building_no": "7"})
		found = self.as_(self.cs, api.lookup_customer, self.phone)
		self.assertEqual((found["address"]["area"], found["address"]["block_no"]), (self.area, "9"))
		order = next(o for o in found["orders"] if o["name"] == invoice)
		self.assertEqual(next(i for i in order["items"] if i["item_code"] == self.w.tv)["serials"], [serial])

	def test_cs_sees_only_own_tickets(self):
		mine = self.open_ticket()
		theirs = self.open_ticket(as_user=user("_dm_cs2@example.com", "Support Team"))
		rows = self.as_(self.cs, api.get_tickets, "cs")["rows"]
		self.assertEqual([r["name"] for r in rows if r["name"] in (mine, theirs)], [mine])


class TestPickupTrip(SupportTestCase):
	def test_booking_takes_a_tv_slot(self):
		name = self.open_ticket()
		self.book(name)
		self.assertEqual(self.step(name), "Pickup")
		[booking] = get_reservations("Issue", name)
		self.assertEqual((booking.area, str(booking.delivery_time)[:5]), (self.area, "10:00"))
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 1)

	def test_trip_is_listed_with_the_tv_orders(self):
		name = self.open_ticket()
		self.book(name)
		res = self.as_(self.office, drivers_api.get_assignment_orders, "Non AC", delivery_date=str(self.slot_date))
		row = next(r for r in res["rows"] if r["name"] == name)
		self.assertEqual((row["kind"], row["order_type"], row["delivery_time"]), ("ticket", "Maintenance", "10:00"))
		self.assertEqual(res["total"], len(res["rows"]))

	def test_only_a_free_tv_driver(self):
		first = self.at_pickup()
		second = self.open_ticket()
		self.book(second)
		self.assertRaises(frappe.ValidationError, self.as_, self.office, api.assign_driver, second, self.maint)  # not a TV Driver
		options = {o["user"]: o for o in self.as_(self.office, api.get_driver_options, second)["drivers"]}
		self.assertFalse(options[self.tech1]["available"])  # same hour as the first
		self.assertTrue(options[self.tech2]["available"])
		self.assertRaises(frappe.ValidationError, self.as_, self.office, api.assign_driver, second, self.tech1)
		self.as_(self.office, api.assign_driver, second, self.tech2)
		self.assertRaises(frappe.PermissionError, self.as_, self.cs, api.assign_driver, first, self.tech2)

	def test_technician_sees_his_visits_only(self):
		name = self.at_pickup()
		self.assertNotIn(name, [r["name"] for r in self.as_(self.tech2, api.get_tickets, "technician")["rows"]])
		self.assertIn(name, [r["name"] for r in self.as_(self.tech1, api.get_tickets, "technician")["rows"]])
		self.assertRaises(frappe.PermissionError, self.as_, self.tech2, api.get_ticket_detail, name)

	def test_resolved_at_the_customer(self):
		name = self.at_pickup()
		result = self.as_(self.tech1, api.log_visit, name, "Fully Completed", "Reset the box")
		self.assertTrue(frappe.db.exists("Maintenance Visit", result["maintenance_visit"]))
		self.assertRaises(frappe.PermissionError, self.as_, self.tech2, api.resolve_ticket, name, "technician", "ok")
		self.as_(self.tech1, api.resolve_ticket, name, "technician", "Reset the box")
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "status"]), ("Closed", "Resolved"))
		self.assertEqual(len(get_reservations("Issue", name)), 1)  # the driver went: the slot stays used
		self.assertRaises(frappe.ValidationError, self.as_, self.tech1, api.fetch_item, name)

	def test_report_before_taking_the_item(self):
		name = self.at_pickup(item_code=self.w.pipe)
		self.assertRaises(frappe.ValidationError, self.as_, self.tech1, api.fetch_item, name, None, " ")
		self.as_(self.tech1, api.fetch_item, name, None, "Motor noise, take it")
		self.assertEqual(self.step(name), "To Workshop")
		issue = frappe.get_doc("Issue", name)
		self.assertEqual(issue.sm_tech_visits[-1].report, "Motor noise, take it")
		notes = frappe.get_all("Comment", filters={"reference_name": name, "comment_type": "Comment"}, pluck="content")
		self.assertTrue(any("Motor noise" in n for n in notes))

	def test_nobody_goes_without_a_driver(self):
		name = self.open_ticket(item_code=self.w.pipe)
		self.book(name)
		# Not even the System Manager can act for a trip that has no driver.
		self.assertRaises(frappe.ValidationError, api.log_visit, name, "Partially Completed", "x")
		self.assertRaises(frappe.ValidationError, api.fetch_item, name, None, "x")
		self.assertRaises(frappe.ValidationError, api.customer_not_home, name, "No answer")

	def test_each_page_shows_its_own_buttons(self):
		name = self.at_pickup()
		cs = self.as_(self.cs, api.get_ticket_detail, name, "cs")["can"]
		tech = self.as_(self.tech1, api.get_ticket_detail, name, "technician")["can"]
		office = self.as_(self.office, api.get_ticket_detail, name)["can"]
		self.assertEqual((cs["fetch_item"], cs["resolve"], cs["book_pickup"]), (False, False, False))  # out of their hands
		self.assertEqual((tech["fetch_item"], tech["resolve"], tech["book_pickup"]), (True, True, False))
		self.assertFalse(any(office.values()))
		# Booking is customer service's job.
		self.assertRaises(frappe.PermissionError, self.as_, self.tech1, api.book_trip, name, self.area,
			str(self.slot_date), "11:00")

	def test_customer_service_resolves_only_before_the_visit(self):
		name = self.open_ticket()
		self.assertRaises(frappe.ValidationError, self.as_, self.cs, api.resolve_ticket, name, "cs", " ")
		self.as_(self.cs, api.resolve_ticket, name, "cs", "Told him to reset it, works")
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "status"]), ("Closed", "Resolved"))
		booked = self.open_ticket()
		self.book(booked)
		self.assertRaises(frappe.PermissionError, self.as_, self.cs, api.resolve_ticket, booked, "cs", "x")
		self.assertRaises(frappe.ValidationError, self.as_, self.cs, api.book_trip, booked, self.area,
			str(self.slot_date), "10:00", " ")  # a note is needed to book

	def test_customer_not_home_goes_back_to_customer_service(self):
		name = self.at_pickup()
		self.as_(self.tech1, api.customer_not_home, name, "No answer")
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "sm_driver", "sm_not_home_count"]),
			("Customer Service", None, 1))
		# The driver went: the slot stays booked.
		self.assertEqual(len(get_reservations("Issue", name)), 1)
		row = next(r for r in self.as_(self.cs, api.get_tickets, "cs")["rows"] if r["name"] == name)
		self.assertEqual(row["not_home_count"], 1)
		self.assertIn("No answer", row["last_note"])
		self.book(name, "16:00")  # booked again: one more slot
		self.assertEqual((self.step(name), len(get_reservations("Issue", name))), ("Pickup", 2))


class TestSerialRule(SupportTestCase):
	def test_serialised_item_needs_its_serial(self):
		invoice, serial = self.sold_tv()
		name = self.at_pickup(sales_invoice=invoice)
		self.assertRaises(frappe.ValidationError, self.as_, self.tech1, api.fetch_item, name, None, "Took it")
		receive(self.w.tv, self.w.main, 1, serials=["_DM-SUP-OTHER"])
		self.assertRaises(frappe.ValidationError, self.as_, self.tech1, api.fetch_item, name, "_DM-SUP-OTHER", "Took it")
		self.as_(self.tech1, api.fetch_item, name, serial, "Took it")
		self.assertEqual(frappe.db.get_value("Serial No", serial, "warehouse"), self.transit)
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "sm_serial_no"]), ("To Workshop", serial))

	def test_driver_may_correct_the_serial_then_it_must_match(self):
		invoice, serial = self.sold_tv()
		name = self.at_pickup(sales_invoice=invoice, serial_no=serial)
		_other_invoice, other = self.sold_tv()
		# Filled from the ticket; the driver corrects it at the customer.
		self.as_(self.tech1, api.fetch_item, name, other, "Took it")
		self.assertEqual(frappe.db.get_value("Issue", name, "sm_serial_no"), other)
		# From now on only that serial moves.
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, api.receive_in_workshop, name, serial)
		self.as_(self.maint, api.receive_in_workshop, name)
		self.assertEqual(frappe.db.get_value("Serial No", other, "warehouse"), self.workshop)

	def test_new_serial_is_created_and_kept(self):
		name = self.at_pickup()  # bought elsewhere: no serial in our system
		self.as_(self.tech1, api.fetch_item, name, "_DM-SUP-NEW-1", "Took it")
		self.assertEqual(frappe.db.get_value("Serial No", "_DM-SUP-NEW-1", ["item_code", "warehouse"]),
			(self.w.tv, self.transit))
		# The workshop scans it again: another serial is refused.
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, api.receive_in_workshop, name, "_DM-SUP-NEW-2")
		self.as_(self.maint, api.receive_in_workshop, name, "_DM-SUP-NEW-1")
		self.assertEqual(frappe.db.get_value("Serial No", "_DM-SUP-NEW-1", "warehouse"), self.workshop)


class TestWorkshopAndReturn(SupportTestCase):
	def test_whole_journey(self):
		invoice, serial = self.sold_tv()
		name = self.at_pickup(sales_invoice=invoice)
		self.as_(self.tech1, api.fetch_item, name, serial, "Took it")
		self.assertIn(name, [r["name"] for r in self.as_(self.office, api.get_transit_items)])
		self.assertIn(name, [r["name"] for r in self.as_(self.maint, api.get_tickets, "maintenance")["rows"]])

		self.as_(self.maint, api.receive_in_workshop, name, serial)
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "sm_warehouse"]), ("Under Repair", self.workshop))
		# No proof, no repair.
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, api.finish_repair, name, "Repaired", "done")
		self.proof(name, "repair", self.maint)
		self.as_(self.maint, api.finish_repair, name, "Repaired", "Board replaced")
		self.assertIn(name, [r["name"] for r in self.as_(self.cs, api.get_tickets, "cs", tab="ready")["rows"]])

		self.book(name, "16:00")
		self.assertEqual(self.step(name), "Return")
		# The pickup used a car: its slot stays booked next to the return's.
		self.assertEqual(sorted(str(r.delivery_time)[:5] for r in get_reservations("Issue", name)), ["10:00", "16:00"])
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 1)
		# Still in the workshop until it is sent out, and only with a driver.
		self.assertIn(name, [r["name"] for r in self.as_(self.maint, api.get_tickets, "maintenance")["rows"]])
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, api.send_out, name, serial)
		self.as_(self.office, api.assign_driver, name, self.tech2)
		self.as_(self.maint, api.send_out, name, serial)
		self.assertNotIn(name, [r["name"] for r in self.as_(self.maint, api.get_tickets, "maintenance")["rows"]])
		self.assertEqual(frappe.db.get_value("Serial No", serial, "warehouse"), self.transit)
		self.assertIn(name, [r["name"] for r in self.as_(self.tech2, api.get_return_trips)["rows"]])

		# Hand-over: signature and a photo / video first.
		self.assertRaises(frappe.ValidationError, self.as_, self.tech2, api.complete_return, name)
		self.proof(name, "customer", self.tech2, "handover.mp4")
		self.assertRaises(frappe.ValidationError, self.as_, self.tech2, api.complete_return, name)
		self.proof(name, "signature", self.tech2, "sign.png")
		self.as_(self.tech2, api.complete_return, name)
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "status"]), ("Closed", "Resolved"))
		self.assertIsNone(frappe.db.get_value("Serial No", serial, "warehouse"))
		files = frappe.get_all("File", filters={"attached_to_name": name}, pluck="file_name")
		self.assertIn(f"Customer signature at return 1 - {name}.png", files)
		self.assertIn(f"Workshop repair proof 1 - {name}.png", files)

		events = [s.event for s in control.get_order(name)["steps"]]
		self.assertEqual(events, ["Ticket opened", "Visit booked", "Ticket driver assigned", "Visit logged", "Item picked up",
			"Received in workshop", "Repaired", "Return booked", "Ticket driver assigned", "Sent out from workshop",
			"Returned to customer"])
		self.assertIn(name, [o.name for o in control.get_orders(kind="ticket")])

	def test_item_without_serial_moves_in_stock_without_changing_our_value(self):
		"""Every unit shows in the stock; our own stock keeps its value (separate stores)."""
		receive(self.w.pipe, self.our_transit, 4, rate=10)
		before = self.stock_value(self.w.pipe, self.our_transit)
		qty = lambda wh: flt(frappe.db.get_value("Bin", {"item_code": self.w.pipe, "warehouse": wh}, "actual_qty"))
		name = self.at_pickup(item_code=self.w.pipe)
		self.as_(self.tech1, api.fetch_item, name, None, "Took it")
		self.assertEqual(qty(self.transit), 1)
		self.as_(self.maint, api.receive_in_workshop, name, None)
		self.assertEqual((qty(self.transit), qty(self.workshop)), (0, 1))
		self.proof(name, "repair", self.maint)
		self.as_(self.maint, api.finish_repair, name, "Repaired", "Fixed")
		self.book(name, "16:00")
		self.as_(self.office, api.assign_driver, name, self.tech2)
		self.as_(self.maint, api.send_out, name, None)
		self.assertEqual((qty(self.transit), qty(self.workshop)), (1, 0))
		self.assertEqual(frappe.db.get_value("Issue", name, "sm_warehouse"), self.transit)
		self.proof(name, "customer", self.tech2)
		self.proof(name, "signature", self.tech2, "sign.png")
		self.as_(self.tech2, api.complete_return, name)
		self.assertEqual(qty(self.transit), 0)
		self.assertEqual(self.stock_value(self.w.pipe, self.our_transit), before)
		self.assertEqual(len(frappe.get_all("Stock Entry", filters={"remarks": ["like", f"%{name}%"], "docstatus": 1})), 4)

	def test_customer_stores_never_hold_our_goods(self):
		doc = frappe.get_single("Delivery Settings")
		doc.support_transit_warehouse = doc.transit_warehouse
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_serialised_item_moves_at_zero_value(self):
		invoice, serial = self.sold_tv()
		name = self.at_pickup(sales_invoice=invoice)
		self.out_for_return(name, serial)
		self.proof(name, "customer", self.tech2)
		self.proof(name, "signature", self.tech2, "sign.png")
		self.as_(self.tech2, api.complete_return, name)
		entries = frappe.get_all("Stock Entry", filters={"remarks": ["like", f"%{name}%"], "docstatus": 1}, pluck="name")
		self.assertEqual(len(entries), 4)
		value = frappe.db.sql("""select sum(abs(stock_value_difference)) from `tabStock Ledger Entry`
			where voucher_no in %s and is_cancelled = 0""", [tuple(entries)])[0][0]
		gl = frappe.db.sql("select sum(debit) + sum(credit) from `tabGL Entry` where voucher_no in %s and is_cancelled = 0",
			[tuple(entries)])[0][0]
		self.assertEqual((flt(value), flt(gl)), (0, 0))

	def test_not_home_on_return_goes_to_the_main_store_then_out_again(self):
		invoice, serial = self.sold_tv()
		name = self.at_pickup(sales_invoice=invoice)
		self.out_for_return(name, serial)
		self.as_(self.tech2, api.customer_not_home, name, "No answer")
		# The driver brings it back: still in transit until the stock keeper scans it in.
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "sm_warehouse"]), ("Back to Store", self.transit))
		self.assertEqual(frappe.db.get_value("Serial No", serial, "warehouse"), self.transit)
		keeper = user("_dm_keeper@example.com", "Stock User")
		self.assertIn(name, [r["name"] for r in self.as_(keeper, api.get_store_items)["back"]])
		self.assertRaises(frappe.PermissionError, self.as_, self.maint, api.receive_back, name, serial)
		self.assertRaises(frappe.ValidationError, self.as_, keeper, api.receive_back, name, None)  # serial required
		self.as_(keeper, api.receive_back, name, serial)
		main = self.cstore
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "sm_warehouse", "sm_driver"]), ("Ready for Return", main, None))
		self.assertEqual(frappe.db.get_value("Serial No", serial, "warehouse"), main)
		self.assertNotIn(name, [r["name"] for r in self.as_(self.maint, api.get_tickets, "maintenance")["rows"]])
		# Customer service books again; the driver is told to collect it from the main store.
		self.book(name, "16:00")
		self.as_(self.office, api.assign_driver, name, self.tech1)
		detail = self.as_(self.cs, api.get_ticket_detail, name, "cs")
		self.assertEqual(detail["take_from"], main)
		self.assertFalse(self.as_(self.maint, api.get_ticket_detail, name, "maintenance")["can"]["send_out"])
		self.assertRaises(frappe.PermissionError, self.as_, self.maint, api.send_out, name, serial)
		self.assertIn(name, [r["name"] for r in self.as_(keeper, api.get_store_items)["send"]])
		self.as_(keeper, api.store_send_out, name, serial)
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "sm_warehouse"]), ("Out for Return", self.transit))

	def test_not_repairable_goes_back_the_same_way(self):
		name = self.at_pickup(item_code=self.w.pipe)
		self.to_workshop(name)
		self.proof(name, "repair", self.maint)
		self.as_(self.maint, api.finish_repair, name, "Not repairable", "Motor burnt")
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_step", "sm_repair_result"]),
			("Ready for Return", "Not repairable"))

	def test_workshop_cannot_resolve_it_must_repair(self):
		invoice, serial = self.sold_tv()
		name = self.at_pickup(sales_invoice=invoice)
		self.to_workshop(name, serial)
		self.assertFalse(self.as_(self.maint, api.get_ticket_detail, name, "maintenance")["can"]["resolve"])
		self.assertRaises(frappe.PermissionError, self.as_, self.maint, api.resolve_ticket, name, "maintenance", "x")
		self.assertEqual(frappe.db.get_value("Issue", name, "sm_step"), "Under Repair")

	def test_stock_keeper_buttons_inside_the_ticket(self):
		invoice, serial = self.sold_tv()
		name = self.at_pickup(sales_invoice=invoice)
		self.out_for_return(name, serial)
		self.as_(self.tech2, api.customer_not_home, name, "No answer")
		keeper = user("_dm_keeper@example.com", "Stock User")
		self.assertTrue(self.as_(keeper, api.get_ticket_detail, name, "store")["can"]["receive_back"])
		self.as_(keeper, api.receive_back, name, serial)
		self.book(name, "16:00")
		self.as_(self.office, api.assign_driver, name, self.tech1)
		self.assertTrue(self.as_(keeper, api.get_ticket_detail, name, "store")["can"]["store_send"])

	def test_full_address_from_the_erp(self):
		self.assertIn("address", t.last_address(self.w.customer))

	def stock_value(self, item_code, wh):
		return flt(frappe.db.sql("""select stock_value from `tabStock Ledger Entry` where item_code = %s and warehouse = %s
			and is_cancelled = 0 order by posting_date desc, posting_time desc, creation desc limit 1""", (item_code, wh))[0][0])
