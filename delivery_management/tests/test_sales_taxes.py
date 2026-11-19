"""Default tax template from Delivery Settings (VAT included in the price)."""

import frappe
from frappe.utils import add_days, flt, nowdate

from delivery_management.tests.fixtures import PosWorld
from delivery_management.tests.utils import DeliveryTestCase


def vat_template(company, included=1):
	name = f"_DM VAT {included} - {company.abbr}"
	if frappe.db.exists("Sales Taxes and Charges Template", name):
		return name
	parent = frappe.db.get_value("Account", {"company": company.name, "is_group": 1, "root_type": "Liability"})
	account = frappe.db.get_value("Account", {"account_name": "_DM VAT", "company": company.name}) or \
		frappe.get_doc({"doctype": "Account", "account_name": "_DM VAT", "company": company.name,
			"parent_account": parent, "account_type": "Tax"}).insert().name
	return frappe.get_doc({"doctype": "Sales Taxes and Charges Template", "title": f"_DM VAT {included}",
		"company": company.name, "taxes": [{"charge_type": "On Net Total", "account_head": account,
			"description": "VAT", "rate": 10, "included_in_print_rate": included}]}).insert().name


class TestSalesTaxes(DeliveryTestCase):
	def setUp(self):
		super().setUp()
		self.w = PosWorld()

	def order(self, **extra):
		so = frappe.new_doc("Sales Order")
		so.update({"customer": self.w.customer, "company": self.w.company.name, "transaction_date": nowdate(),
			"delivery_date": add_days(nowdate(), 3), "set_warehouse": self.w.main, **extra})
		so.append("items", {"item_code": self.w.service, "qty": 1, "rate": 11})
		return so.insert()

	def test_no_template_no_tax(self):
		self.settings(sales_taxes_template=None)
		so = self.order()
		self.assertFalse(so.taxes)
		self.assertEqual(flt(so.grand_total), 11)

	def test_template_added_and_total_unchanged(self):
		self.settings(sales_taxes_template=vat_template(self.w.company))
		so = self.order()
		self.assertEqual(len(so.taxes), 1)
		self.assertEqual(flt(so.grand_total), 11)  # VAT is inside the price
		self.assertEqual(flt(so.total_taxes_and_charges), 1)
		self.assertEqual(flt(so.net_total), 10)

	def test_hand_picked_template_is_kept(self):
		self.settings(sales_taxes_template=vat_template(self.w.company))
		so = self.order(taxes_and_charges=None, taxes=[])
		self.assertEqual(so.taxes_and_charges, vat_template(self.w.company))
		other = vat_template(self.w.company, included=1)
		self.assertEqual(self.order(taxes_and_charges=other).taxes_and_charges, other)

	def test_settings_refuse_tax_on_top(self):
		settings = frappe.get_single("Delivery Settings")
		settings.sales_taxes_template = vat_template(self.w.company, included=0)
		self.assertRaises(frappe.ValidationError, settings.validate_sales_taxes)
