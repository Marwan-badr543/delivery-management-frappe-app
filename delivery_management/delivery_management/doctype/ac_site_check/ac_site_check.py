"""AC Site Check: an AC Tech visits the customer's house before a sale to see which AC
type and capacity each room needs.

On save the customer is found by phone (same lookup as support tickets); when no
customer has that phone, a new one is created from the name and phone. The address is
stored on the customer the same way the POS does (area, block, road, building).
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr

from delivery_management.customer_support.tickets import find_customer
from delivery_management.drivers.assignment import ROLE_AC
from delivery_management.orders import address as address_utils


class ACSiteCheck(Document):
	def validate(self):
		self.customer_name = cstr(self.customer_name).strip()
		self.customer_phone = address_utils._clean(self.customer_phone)
		for field in ("block_no", "road_no", "building_no"):
			self.set(field, address_utils._clean(self.get(field)))
		address_utils.validate_numbers({"dm_block_no": self.block_no, "dm_road_no": self.road_no,
			"dm_building_no": self.building_no})
		if self.ac_tech and ROLE_AC not in frappe.get_roles(self.ac_tech):
			frappe.throw(_("{0} does not have the {1} role.").format(self.ac_tech, ROLE_AC))
		if not self.customer or self.has_value_changed("customer_phone"):
			self.customer = find_customer(self.customer_phone) or make_customer(self.customer_name, self.customer_phone)
		self.address = address_utils.upsert_customer_address(self.customer, {
			"area": self.area, "block_no": self.block_no, "road_no": self.road_no, "building_no": self.building_no})


def make_customer(name: str, phone: str) -> str:
	customer = frappe.get_doc({
		"doctype": "Customer",
		"customer_name": name or phone,
		"customer_type": "Individual",
		"mobile_no": phone,
		"customer_group": frappe.db.get_single_value("Selling Settings", "customer_group")
			or frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
		"territory": frappe.db.get_single_value("Selling Settings", "territory")
			or frappe.db.get_value("Territory", {"is_group": 0}, "name"),
	})
	customer.insert(ignore_permissions=True)
	frappe.msgprint(_("New customer {0} created.").format(frappe.bold(customer.name)), alert=True, indicator="green")
	return customer.name


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def ac_tech_query(doctype, txt, searchfield, start, page_len, filters):
	return frappe.db.sql(
		"""select u.name, u.full_name from `tabUser` u
		join `tabHas Role` r on r.parent = u.name and r.parenttype = 'User' and r.role = %(role)s
		where u.enabled = 1 and (u.name like %(txt)s or u.full_name like %(txt)s)
		order by u.full_name limit %(start)s, %(page_len)s""",
		{"role": ROLE_AC, "txt": f"%{txt}%", "start": start, "page_len": page_len},
	)
