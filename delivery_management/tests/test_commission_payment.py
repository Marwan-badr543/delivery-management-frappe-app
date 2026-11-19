"""Sales Commission Payment: pays each invoice's commission once, month by month, with the
same numbers as the Sales Statistics page."""

import frappe
from frappe.utils import add_months, flt, getdate, nowdate

from delivery_management.delivery_management.doctype.sales_commission_payment import (
	sales_commission_payment as scp,
)
from delivery_management.orders import payments
from delivery_management.stats import sales
from delivery_management.tests.test_sales_stats import SECOND, SalesStatsCase


class TestSalesCommissionPayment(SalesStatsCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_value("Item", self.w.pipe, "dm_small_item", 1)
		# Months that have ended (only those can be paid); SECOND sells nothing else there.
		self.m1 = sales.month_start(add_months(nowdate(), -30))
		self.m2 = getdate(add_months(self.m1, 1))
		self.rule(SECOND, self.m1, small_items_target=4, min_percent=1, max_percent=3)
		self.cash = frappe.db.get_value("Account", {"company": self.company, "account_type": "Cash", "is_group": 0})

	def sale(self, lines, month, paid=True):
		name = self.sell(lines, owner=SECOND, paid=paid)
		self.set_created(name, f"{month} 10:00:00")
		return name

	def unpaid(self, to_month, from_month=None, basis="Total (incl. VAT)"):
		state = scp.start_unpaid(SECOND, self.company, str(to_month), 1, basis, str(from_month) if from_month else None)
		self.assertEqual(state["status"], "done")
		return state["result"]

	def pay(self, to_month, from_month=None, submit=True, names=None):
		result = self.unpaid(to_month, from_month)
		doc = frappe.get_doc({"doctype": "Sales Commission Payment", "company": self.company, "salesman": SECOND,
			"to_month": to_month, "from_month": from_month, "paid_from": self.cash,
			"basis": "Total (incl. VAT)", "exclude_services": 1,
			"invoices": [{"sales_invoice": n} for n in (names or [r["sales_invoice"] for r in result["invoices"]])]})
		doc.insert()
		if submit:
			doc.submit()
		return doc

	def test_pays_the_month_like_the_page(self):
		a = self.sale([(self.w.pipe, 4, 25)], self.m1)  # 100, 4 of 4 small items: Max 3%
		b = self.sale([(self.w.tv, 1, 100)], self.m1)
		doc = self.pay(self.m1)
		self.assertEqual(sorted(r.sales_invoice for r in doc.invoices), sorted([a, b]))
		[month] = doc.months
		self.assertEqual((flt(month.percent), flt(month.unpaid_sales)), (3, 200))
		self.assertEqual(flt(doc.total_amount), 4.5)  # 200 × 3% × 75% (no evaluation)
		page = self.row(sales.compute(self.m1, sales.month_end(self.m1), self.company, SECOND), SECOND)
		self.assertEqual(page["commission"]["total"]["amount"], flt(doc.total_amount))
		# The expense is posted: Sales Commission to the cash account.
		je = frappe.get_doc("Journal Entry", doc.journal_entry)
		lines = {r.account: (flt(r.debit), flt(r.credit)) for r in je.accounts}
		self.assertEqual(lines[scp.commission_account_of(self.company)], (4.5, 0))
		self.assertEqual(lines[self.cash], (0, 4.5))

	def test_an_invoice_is_paid_once(self):
		a = self.sale([(self.w.tv, 1, 100)], self.m1)
		self.pay(self.m1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", a, "dm_commission_payment") is not None, True)
		self.assertEqual(self.unpaid(self.m1)["invoices"], [])  # never fetched again
		self.assertRaises(frappe.ValidationError, self.pay, self.m1, names=[a])

	def test_two_drafts_cannot_both_be_paid(self):
		self.sale([(self.w.tv, 1, 100)], self.m1)
		first = self.pay(self.m1, submit=False)
		second = self.pay(self.m1, submit=False)
		first.submit()
		self.assertRaises(frappe.ValidationError, second.submit)

	def test_cancel_unpays_and_reverses_the_expense(self):
		a = self.sale([(self.w.tv, 1, 100)], self.m1)
		doc = self.pay(self.m1)
		doc.cancel()
		self.assertIsNone(frappe.db.get_value("Sales Invoice", a, "dm_commission_payment"))
		self.assertEqual(frappe.db.get_value("Journal Entry", doc.journal_entry, "docstatus"), 2)
		self.assertEqual([r["sales_invoice"] for r in self.unpaid(self.m1)["invoices"]], [a])

	def test_late_paid_sale_comes_in_a_later_payment_at_its_own_month(self):
		self.sale([(self.w.pipe, 2, 25)], self.m1)  # 50, 2 small items
		late = self.sale([(self.w.pipe, 2, 25)], self.m1, paid=False)  # the customer pays later
		self.assertEqual(flt(self.pay(self.m1).months[0].percent), 2)  # 2 of 4: 1 + 0.5 × 2

		payments.collect(late, 50, "Cash", "test", "paid late")
		doc = self.pay(self.m2, from_month=self.m2)
		self.assertEqual([(r.sales_invoice, r.late) for r in doc.invoices], [(late, 1)])
		[month] = doc.months
		# The first month again, now with all 4 small items: Max 3%.
		self.assertEqual((str(month.month), month.late, flt(month.percent)), (str(self.m1), 1, 3))
		self.assertEqual(flt(doc.total_amount), 1.125)  # 50 × 3% × 75%

	def test_return_after_the_commission_was_paid_is_not_subtracted(self):
		sold = self.sale([(self.w.tv, 1, 100)], self.m1)
		self.pay(self.m1)
		ret = self.credit_note(sold, self.w.tv, owner=SECOND)
		self.set_created(ret, f"{self.m2} 10:00:00")
		self.assertEqual(self.unpaid(self.m2)["invoices"], [])
		# The page still shows the return in the sales, but it takes nothing off the commission.
		page = sales.compute(self.m2, sales.month_end(self.m2), self.company, SECOND)
		self.assertIn(ret, self.names(page))
		self.assertEqual(self.row(page, SECOND)["total"], -100)
		self.assertEqual(self.row(page, SECOND)["months"][0]["commission"]["total"]["amount"], 0)

	def test_return_before_payment_is_subtracted(self):
		kept = self.sale([(self.w.tv, 1, 100)], self.m1)
		sold = self.sale([(self.w.tv, 1, 100)], self.m1)
		ret = self.credit_note(sold, self.w.tv, owner=SECOND)
		self.set_created(ret, f"{self.m1} 11:00:00")
		doc = self.pay(self.m1)
		self.assertEqual(sorted(r.sales_invoice for r in doc.invoices), sorted([kept, sold, ret]))
		self.assertEqual(flt(doc.months[0].unpaid_sales), 100)  # 200 sold, 100 returned
		self.assertEqual(flt(doc.total_amount), 0.75)  # 100 × 1% (no small items: Min) × 75%

	def test_fully_returned_sale_is_closed_with_nothing_to_pay(self):
		sold = self.sale([(self.w.tv, 2, 100)], self.m1)
		ret = self.credit_note(sold, self.w.tv, owner=SECOND)
		self.set_created(ret, f"{self.m1} 11:00:00")
		doc = self.pay(self.m1)
		self.assertEqual((flt(doc.total_amount), doc.journal_entry), (0, None))
		self.assertEqual(self.unpaid(self.m2)["invoices"], [])  # never brought back as Late

	def test_month_not_ended_cannot_be_paid(self):
		self.sale([(self.w.tv, 1, 100)], self.m1)
		self.assertRaises(frappe.ValidationError, self.pay, sales.month_start(nowdate()))

	def test_next_month_after_the_last_payment(self):
		self.assertIsNone(scp.next_month(SECOND))
		self.sale([(self.w.tv, 1, 100)], self.m1)
		self.pay(self.m1)
		self.assertEqual(scp.next_month(SECOND), str(self.m2))
