import frappe

from delivery_management.drivers.assignment import ROLE_AC
from delivery_management.tests.test_drivers import user
from delivery_management.tests.utils import DeliveryTestCase


class TestACSiteCheck(DeliveryTestCase):
	def setUp(self):
		super().setUp()
		self.area = self.make_area("_DM Check Area")
		self.tech = user("_dm.ac1@example.com", ROLE_AC)

	def check(self, phone, name="_DM Site Customer", **extra):
		return frappe.get_doc({"doctype": "AC Site Check", "customer_name": name, "customer_phone": phone,
			"ac_tech": self.tech, "area": self.area, "block_no": "١٠٣٤", "road_no": "3479", "building_no": "3105",
			"rooms": [{"room": "Living room", "size": "5 x 6 m", "recommendations": "2 ton split"}], **extra}).insert()

	def test_new_phone_creates_customer(self):
		doc = self.check("39990001")
		self.assertEqual(frappe.db.get_value("Customer", doc.customer, ["customer_name", "mobile_no"]),
			("_DM Site Customer", "39990001"))
		self.assertEqual(frappe.db.get_value("Address", doc.address, "dm_block_no"), "1034")
		self.assertEqual(len(doc.rooms), 1)

	def test_existing_phone_reuses_customer(self):
		first = self.check("39990002")
		second = self.check("39990002", name="Other name")
		self.assertEqual(first.customer, second.customer)
		self.assertEqual(frappe.db.count("Customer", {"mobile_no": "39990002"}), 1)

	def test_tech_needs_ac_tech_role(self):
		other = user("_dm.tv1@example.com", "TV Driver")
		self.assertRaises(frappe.ValidationError, self.check, "39990003", ac_tech=other)

	def test_ac_tech_sees_only_his_checks(self):
		mine = self.check("39990004")
		other_tech = user("_dm.ac2@example.com", ROLE_AC)
		theirs = self.check("39990005", ac_tech=other_tech)
		frappe.set_user(self.tech)
		try:
			names = frappe.get_list("AC Site Check", pluck="name")
			self.assertIn(mine.name, names)
			self.assertNotIn(theirs.name, names)
			self.assertFalse(frappe.has_permission("AC Site Check", "read", theirs.name))
		finally:
			frappe.set_user("Administrator")
		self.assertIn(theirs.name, frappe.get_list("AC Site Check", pluck="name"))

	def test_office_reads_status_and_tech_cannot_change_it(self):
		from frappe.desk.doctype.number_card.number_card import get_result

		doc = self.check("39990006")
		frappe.set_user(user("_dm.manager@example.com", "Delivery Manager"))
		# The workspace card "Open AC Site Checks" filters on the level-1 field status.
		self.assertGreaterEqual(get_result(frappe.get_doc("Number Card", "Open AC Site Checks"), []), 1)
		frappe.set_user(self.tech)
		mine = frappe.get_doc("AC Site Check", doc.name)
		self.assertTrue(mine.has_permlevel_access_to("status", permission_type="read"))
		self.assertFalse(mine.has_permlevel_access_to("status", permission_type="write"))
