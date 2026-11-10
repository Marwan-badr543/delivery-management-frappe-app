"""Cash with the drivers: cash taken on delivery stays on the driver until the office receives it."""

import frappe
from frappe.utils import flt

from delivery_management.api import control, drivers as api, stock_keeper
from delivery_management.tests.fixtures import send_out
from delivery_management.tests.test_transit import TransitTestCase
from delivery_management.transit import cash

CASH = "Driver Cash"


def account_balance(account):
	return flt(frappe.db.sql("""select sum(debit) - sum(credit) from `tabGL Entry`
		where account = %s and is_cancelled = 0""", account)[0][0])


class TestDriverCash(TransitTestCase):
	def setUp(self):
		super().setUp()
		self.si, self.so = self.order([(self.w.tv, 1, 100)], unpaid=True)
		self.dn = self.note(self.so)
		send_out(self.dn, {self.w.tv: ["_DMTV-M-2"]})
		self.driver = frappe.db.get_value("Delivery Note", self.dn, "dm_driver")
		self.company = frappe.db.get_value("Sales Invoice", self.si, "company")
		self.drivers_account = cash.drivers_account(self.company)
		self.store_cash = cash.store_cash_account("Cash", self.company)

	def held(self):
		return frappe.get_all(CASH, filters={"delivery_note": self.dn}, fields=["name", "driver", "amount", "payment_entry"])

	def test_cash_stays_with_the_driver_until_received(self):
		drivers_before, store_before = account_balance(self.drivers_account), account_balance(self.store_cash)
		out = api.record_payment(self.dn, 60, "Cash")
		self.assertTrue(out["held_by_driver"])
		self.assertEqual(frappe.db.get_value("Payment Entry", out["payment_entry"], "paid_to"), self.drivers_account)
		[row] = self.held()
		self.assertEqual((row.driver, row.amount, row.payment_entry), (self.driver, 60, out["payment_entry"]))
		self.assertEqual(account_balance(self.drivers_account) - drivers_before, 60)
		self.assertEqual(account_balance(self.store_cash), store_before)

		listed = next(r for r in stock_keeper.get_cash() if r.name == row.name)
		self.assertEqual(listed.goods, "With the driver")

		[je] = stock_keeper.receive_cash([row.name])["journal_entries"]
		self.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 1)
		self.assertEqual(self.held(), [])
		self.assertEqual(account_balance(self.drivers_account), drivers_before)
		self.assertEqual(account_balance(self.store_cash) - store_before, 60)
		handed = frappe.get_all("Order Log", filters={"sales_order": self.so, "event": "Cash handed over"},
			fields=["amount", "driver", "reference_name"])
		self.assertEqual([(h.amount, h.driver, h.reference_name) for h in handed], [(60, self.driver, je)])

	def test_cash_cannot_be_received_twice(self):
		api.record_payment(self.dn, 30, "Cash")
		[row] = self.held()
		stock_keeper.receive_cash([row.name])
		self.assertRaisesRegex(frappe.ValidationError, "already received", stock_keeper.receive_cash, [row.name])

	def test_other_modes_go_to_their_account(self):
		card = frappe.get_doc("Mode of Payment", "Credit Card")
		if not any(a.company == self.company for a in card.accounts):
			card.append("accounts", {"company": self.company, "default_account": self.store_cash})
			card.save()
		out = api.record_payment(self.dn, 40, "Credit Card")
		self.assertFalse(out["held_by_driver"])
		self.assertEqual(self.held(), [])
		self.assertNotEqual(frappe.db.get_value("Payment Entry", out["payment_entry"], "paid_to"), self.drivers_account)

	def test_cancelled_payment_is_no_longer_with_the_driver(self):
		out = api.record_payment(self.dn, 25, "Cash")
		frappe.get_doc("Payment Entry", out["payment_entry"]).cancel()
		self.assertEqual(self.held(), [])

	def test_goods_status_follows_the_order(self):
		api.record_payment(self.dn, 100, "Cash")
		api.mark_delivered_by_office(self.dn)
		[row] = [r for r in stock_keeper.get_cash(search=self.dn)]
		self.assertEqual(row.goods, "Delivered")

	def test_only_the_office_handles_cash(self):
		api.record_payment(self.dn, 10, "Cash")
		frappe.set_user(self.driver)
		try:
			self.assertRaises(frappe.PermissionError, stock_keeper.get_cash)
			self.assertRaises(frappe.PermissionError, stock_keeper.receive_cash, [self.held()[0].name])
		finally:
			frappe.set_user("Administrator")


class TestOrderControlType(TransitTestCase):
	def test_filter_orders_or_store_sales(self):
		_si, so = self.order([(self.w.pipe, 1, 5)])
		sale = self.branch_sale([(self.w.pipe, 1, 5)])
		orders = {o.name for o in control.get_orders(kind="order")}
		sales = {o.name for o in control.get_orders(kind="sale")}
		self.assertIn(so, orders)
		self.assertNotIn(sale, orders)
		self.assertIn(sale, sales)
		self.assertNotIn(so, sales)
