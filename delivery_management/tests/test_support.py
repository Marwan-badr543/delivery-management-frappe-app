"""Support tickets: the old customer service -> technician -> maintenance flow on ERPNext Issue."""

import frappe
from frappe.utils import add_to_date, getdate, now_datetime

from delivery_management.api import support as api
from delivery_management.customer_support import tickets as t
from delivery_management.orders import service
from delivery_management.slots import get_reservations
from delivery_management.tests.test_orders import DATA, OrdersTestCase


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
		self.tech1 = user("_dm_tech1@example.com", "Technician")
		self.tech2 = user("_dm_tech2@example.com", "Technician")
		self.maint = user("_dm_maint@example.com", "Maintenance Technician")
		self.phone = "39990001"
		frappe.db.set_value("Customer", self.w.customer, "mobile_no", self.phone)
		frappe.db.set_value("Item", self.w.tv, "warranty_period", 365)
		if not frappe.db.exists("Sales Person", "_DM Service"):
			frappe.get_doc({"doctype": "Sales Person", "sales_person_name": "_DM Service",
				"parent_sales_person": frappe.db.get_value("Sales Person", {"is_group": 1}, "name")}).insert()
		self.settings(support_visit_gap_minutes=60, support_maint_service_person="_DM Service")
		self.visit = add_to_date(now_datetime(), days=3).replace(hour=14, minute=0, second=0, microsecond=0)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def sold_tv(self):
		"""A TV sold at the branch (Invoice): its serial leaves the store with the invoice."""
		result = service.submit_pos_invoice(self.w.draft_invoice([(self.w.tv, 1, 100)]), DATA, {})
		return result["name"]

	def open_ticket(self, as_user=None, **extra):
		frappe.set_user(as_user or self.cs)
		data = {"subject": "Black screen", "customer_phone": self.phone, "priority": "High",
			"issue_type": "_DM Black Screen", "description": "TV shows nothing", "report": "Asked to restart, still black",
			"address": "Road 1, Block 2", "visit_datetime": str(self.visit), **extra}
		name = api.create_ticket(frappe.as_json(data))["name"]
		frappe.set_user("Administrator")
		return name

	def at_technician(self, **extra):
		name = self.open_ticket(**extra)
		frappe.set_user(self.cs)
		api.move_to_technician(name, self.tech1)
		frappe.set_user("Administrator")
		return name


class TestCustomerService(SupportTestCase):
	def test_ticket_is_an_issue_in_customer_service(self):
		name = self.open_ticket()
		issue = frappe.get_doc("Issue", name)
		self.assertEqual((issue.customer, issue.sm_stage, issue.status, issue.priority),
			(self.w.customer, "Customer Service", "Replied", "High"))
		self.assertEqual(issue.sm_cs_user, self.cs)
		self.assertTrue(frappe.db.exists("Issue Type", "_DM Black Screen"))

	def test_rules_on_create(self):
		self.assertRaises(frappe.ValidationError, self.open_ticket, customer_phone="00000000")
		self.assertRaises(frappe.ValidationError, self.open_ticket, report="  ")
		self.assertRaises(frappe.ValidationError, self.open_ticket, visit_datetime=str(add_to_date(now_datetime(), hours=-1)))
		self.assertRaises(frappe.ValidationError, self.open_ticket, customer_email="not-an-email")

	def test_customer_lookup_shows_orders_with_serials(self):
		invoice = self.sold_tv()
		frappe.set_user(self.cs)
		found = api.lookup_customer(self.phone)
		self.assertEqual(found["customer"], self.w.customer)
		order = next(o for o in found["orders"] if o["name"] == invoice)
		tv = next(i for i in order["items"] if i["item_code"] == self.w.tv)
		self.assertEqual(len(tv["serials"]), 1)

	def test_cs_sees_only_own_tickets_admin_sees_all(self):
		mine = self.open_ticket()
		other = user("_dm_cs2@example.com", "Support Team")
		theirs = self.open_ticket(as_user=other)
		frappe.set_user(self.cs)
		self.assertEqual([r["name"] for r in api.get_tickets("cs")["rows"] if r["name"] in (mine, theirs)], [mine])
		frappe.set_user("Administrator")
		self.assertEqual(len([r for r in api.get_tickets("cs")["rows"] if r["name"] in (mine, theirs)]), 2)

	def test_report_editable_only_in_customer_service(self):
		name = self.at_technician()
		frappe.set_user(self.cs)
		self.assertRaises(frappe.ValidationError, api.update_cs_report, name, "new text")


class TestTechnicianStage(SupportTestCase):
	def test_move_needs_saved_address_and_time(self):
		name = self.open_ticket(address="", visit_datetime="")
		frappe.set_user(self.cs)
		self.assertRaises(frappe.ValidationError, api.move_to_technician, name, self.tech1)
		api.update_schedule(name, "Road 9", str(self.visit))
		api.move_to_technician(name, self.tech1)
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_stage", "sm_technician"]), ("Technician", self.tech1))

	def test_only_technicians_and_one_visit_per_hour(self):
		first = self.at_technician()
		second = self.open_ticket(visit_datetime=str(add_to_date(self.visit, minutes=45)))
		frappe.set_user(self.cs)
		self.assertRaises(frappe.ValidationError, api.move_to_technician, second, self.maint)  # wrong role
		options = {o["user"]: o for o in api.get_technician_options(second)["technicians"]}
		self.assertFalse(options[self.tech1]["available"])
		self.assertTrue(options[self.tech2]["available"])
		self.assertEqual(options[self.tech1]["visit_times"], ["14:00"])
		self.assertRaises(frappe.ValidationError, api.move_to_technician, second, self.tech1)
		api.move_to_technician(second, self.tech2)
		self.assertTrue(first)

	def test_visit_creates_only_a_maintenance_visit(self):
		invoice = self.sold_tv()
		name = self.at_technician(sales_invoice=invoice, item_code=self.w.tv)
		frappe.set_user(self.tech2)
		self.assertRaises(frappe.PermissionError, api.log_visit, name, "Fully Completed", "fixed")  # not his
		frappe.set_user(self.tech1)
		result = api.log_visit(name, "Partially Completed", "Replaced the board")
		frappe.set_user("Administrator")
		mv = frappe.get_doc("Maintenance Visit", result["maintenance_visit"])
		self.assertEqual((mv.customer, mv.maintenance_type, mv.completion_status, mv.docstatus),
			(self.w.customer, "Breakdown", "Partially Completed", 0))
		self.assertEqual(mv.purposes[0].item_code, self.w.tv)
		self.assertIsNone(result["warranty_claim"])
		self.assertFalse(frappe.db.exists("Warranty Claim", {"complaint": ["like", f"%{name}%"]}))

	def test_visit_without_item_code(self):
		name = self.at_technician()
		frappe.set_user(self.tech1)
		result = api.log_visit(name, "Fully Completed", "Fixed on site")
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.get_value("Maintenance Visit Purpose", {"parent": result["maintenance_visit"]}, "item_code"))

	def test_technician_sees_only_his_tickets(self):
		name = self.at_technician()
		frappe.set_user(self.tech2)
		self.assertNotIn(name, [r["name"] for r in api.get_tickets("technician")["rows"]])
		self.assertFalse(frappe.get_list("Issue", filters={"name": name}))
		frappe.set_user(self.tech1)
		self.assertIn(name, [r["name"] for r in api.get_tickets("technician")["rows"]])
		self.assertEqual(frappe.get_list("Issue", filters={"name": name}, pluck="name"), [name])


class TestMaintenanceStage(SupportTestCase):
	def escalate(self, **pickup):
		name = self.at_technician(item_code=self.w.tv)
		frappe.set_user(self.tech1)
		api.log_visit(name, "Partially Completed", "Needs workshop")
		api.move_to_maintenance(name, self.maint, **pickup)
		frappe.set_user("Administrator")
		return name

	def test_escalation_needs_a_technician_report(self):
		name = self.at_technician(item_code=self.w.tv)
		frappe.set_user(self.tech1)
		self.assertRaises(frappe.ValidationError, api.move_to_maintenance, name, self.maint)

	def test_escalation_books_the_pickup_slot(self):
		name = self.escalate(area=self.area, delivery_date=str(self.slot_date), delivery_time="10:00")
		self.assertEqual(frappe.db.get_value("Issue", name, ["sm_stage", "sm_maint_technician"]),
			("Maintenance Technician", self.maint))
		[booking] = get_reservations("Issue", name)
		self.assertEqual((booking.area, str(booking.delivery_time)[:5]), (self.area, "10:00"))
		self.assertEqual(self.counter(self.area, self.slot_date, "10:00"), 1)

	def test_maintenance_report_then_resolve_closes_claims(self):
		invoice = self.sold_tv()
		name = self.at_technician(sales_invoice=invoice, item_code=self.w.tv)
		frappe.set_user(self.tech1)
		api.log_visit(name, "Partially Completed", "Workshop")
		api.move_to_maintenance(name, self.maint)
		self.assertRaises(frappe.ValidationError, api.resolve, name)  # no maintenance report yet
		frappe.set_user(self.maint)
		api.save_maint_report(name, "Panel replaced, tested 2h")
		self.assertEqual(api.resolve(name)["warranty_claims_closed"], [])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Issue", name, "status"), "Resolved")
		frappe.set_user(self.maint)
		self.assertRaises(frappe.ValidationError, api.save_maint_report, name, "again")

	def test_resolve_at_customer_service_stage(self):
		name = self.open_ticket()
		frappe.set_user(self.cs)
		api.resolve(name)
		self.assertEqual(frappe.db.get_value("Issue", name, "status"), "Resolved")
		self.assertRaises(frappe.ValidationError, api.resolve, name)
