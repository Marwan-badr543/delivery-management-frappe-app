"""AC Tech Payment: pays delivered AC installations once, posts the expense, and the
Installation Statistics show only what is still unpaid."""

import frappe
from frappe.utils import add_days

from delivery_management.api import drivers as api
from delivery_management.delivery_management.doctype.ac_tech_payment.ac_tech_payment import (
	expense_account_of,
	get_unpaid_installations,
)
from delivery_management.drivers import assignment as asg
from delivery_management.drivers import stats
from delivery_management.tests.fixtures import company
from delivery_management.tests.test_drivers import DAY, DriversTestCase


class TestACTechPayment(DriversTestCase):
	def setUp(self):
		super().setUp()
		self.company = company().name
		self.cash = frappe.db.get_value("Account", {"company": self.company, "account_type": "Cash", "is_group": 0})
		frappe.db.set_value("User", self.ac1, "dm_ac_unit_cost", 10)
		self.installed = [self.install(units, add_days(DAY, i)) for i, units in enumerate((1, 2, 3))]

	def install(self, units, day):
		dn = self.dn([(self.ac, units)], ac_day=day)
		asg.assign(dn.name, self.ac1)
		self.send(dn)
		api.mark_delivered_by_office(dn.name)
		return dn.name

	def unpaid(self):
		rows = stats.installation_stats(DAY, add_days(DAY, 2), self.ac1)
		return rows[0]["unpaid"]

	def pay(self, notes, submit=True):
		doc = frappe.get_doc({"doctype": "AC Tech Payment", "company": self.company, "technician": self.ac1,
			"paid_from": self.cash, "items": [{"delivery_note": n} for n in notes]})
		doc.insert()
		if submit:
			doc.submit()
		return doc

	def test_pay_some_installations_posts_the_expense(self):
		self.assertEqual(self.unpaid(), 60)
		doc = self.pay(self.installed[:2])
		self.assertEqual((doc.total_units, doc.total_amount, doc.expense_account),
			(3, 30, expense_account_of(self.company)))
		gl = frappe.get_all("GL Entry", filters={"voucher_no": doc.journal_entry, "is_cancelled": 0},
			fields=["account", "debit", "credit"])
		self.assertEqual({(g.account, g.debit, g.credit) for g in gl},
			{(doc.expense_account, 30, 0), (self.cash, 0, 30)})
		self.assertEqual(self.unpaid(), 30)
		self.assertEqual([r["delivery_note"] for r in get_unpaid_installations(self.ac1)][-1:], self.installed[2:])

	def test_paid_keeps_its_cost_unpaid_follows_the_new_one(self):
		doc = self.pay(self.installed[:1])
		frappe.db.set_value("User", self.ac1, "dm_ac_unit_cost", 20)
		doc.reload()
		self.assertEqual(doc.total_amount, 10)
		self.assertEqual(self.unpaid(), 100)

	def test_an_installation_is_paid_once(self):
		self.pay(self.installed[:1])
		self.assertRaises(frappe.ValidationError, self.pay, self.installed[:1])
		draft = self.pay(self.installed[1:2], submit=False)
		self.pay(self.installed[1:2])
		self.assertRaises(frappe.ValidationError, draft.submit)

	def test_only_delivered_ac_installations_of_the_tech(self):
		other = self.dn([(self.ac, 1)], ac_day=DAY)
		asg.assign(other.name, self.ac2)
		self.send(other)
		api.mark_delivered_by_office(other.name)
		self.assertRaises(frappe.ValidationError, self.pay, [other.name])
		on_the_way = self.dn([(self.ac, 1)], ac_day=add_days(DAY, 3))
		asg.assign(on_the_way.name, self.ac1)
		self.send(on_the_way)
		self.assertRaises(frappe.ValidationError, self.pay, [on_the_way.name])

	def test_cancel_unpays_and_reverses_the_expense(self):
		doc = self.pay(self.installed)
		self.assertEqual(self.unpaid(), 0)
		doc.cancel()
		self.assertEqual(frappe.db.get_value("Journal Entry", doc.journal_entry, "docstatus"), 2)
		self.assertEqual(self.unpaid(), 60)

	def test_a_paid_installation_note_cannot_be_cancelled(self):
		self.pay(self.installed[:1])
		dn = frappe.get_doc("Delivery Note", self.installed[0])
		self.assertRaises(frappe.LinkExistsError, dn.cancel)
