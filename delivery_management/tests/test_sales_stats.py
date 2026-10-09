"""Sales statistics per salesman: paid Sales Invoices, by creation date, with exact money checks.

Money is asserted against hand-computed amounts (rates x qty), not re-read from the
database, so a wrong query cannot agree with itself.
"""

import frappe
from frappe.utils import add_days, flt, get_datetime, getdate, nowdate

from delivery_management.api import sales_stats as api
from delivery_management.orders import service
from delivery_management.stats import sales
from delivery_management.tests.test_orders import DATA, OrdersTestCase

SECOND = "_dm_salesman@example.com"


def make_user(email, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "DM Salesman", "send_welcome_email": 0,
			"roles": [{"role": r} for r in roles]}).insert(ignore_permissions=True)
	return email


class SalesStatsCase(OrdersTestCase):
	def setUp(self):
		super().setUp()
		self.today = getdate(nowdate())
		self.company = self.w.company.name
		make_user(SECOND, ["Sales User"])

	# ---------------------------------------------------------------- helpers

	def sell(self, lines, owner="Administrator", paid=True):
		"""A POS branch invoice. ``paid=False``: submitted with no payment (Unpaid)."""
		invoice = self.w.draft_invoice(lines)
		if not paid:
			for p in invoice["payments"]:
				p["amount"] = 0
		name = service.submit_pos_invoice(invoice, dict(DATA), {})["name"]
		self.set_owner(name, owner)
		return name

	def order(self, lines, owner="Administrator", unpaid=False):
		invoice = self.w.draft_invoice(lines)
		ac = any(code == self.w.ac for code, _q, _r in lines)
		other = any(code != self.w.ac for code, _q, _r in lines)
		delivery = {**self.delivery, "ac_date": self.delivery["ac_date"] if ac else None}
		if not other:
			delivery.update(delivery_date=None, delivery_time=None)
		name = service.submit_pos_order(invoice, dict(DATA), delivery, unpaid=unpaid)["name"]
		self.set_owner(name, owner)
		return name

	def set_owner(self, name, owner):
		"""``owner`` placed the sale: logged in and verified as the POS cashier."""
		frappe.db.set_value("Sales Invoice", name, {"owner": owner, "posa_cashier": owner}, update_modified=False)

	def set_created(self, name, when):
		frappe.db.set_value("Sales Invoice", name, "creation", get_datetime(when), update_modified=False)

	def credit_note(self, against, item_code, owner="Administrator"):
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		ret = make_return_doc("Sales Invoice", against)
		ret.items = [i for i in ret.items if i.item_code == item_code]
		ret.is_pos = 0
		ret.set("payments", [])
		ret.insert()
		ret.submit()
		self.set_owner(ret.name, owner)
		return ret.name

	def stats(self, from_date=None, to_date=None, user=None, company=None):
		return sales.compute(from_date or self.today, to_date or self.today, company or self.company, user)

	def row(self, result, user):
		return next((r for r in result["rows"] if r["user"] == user), None)

	def names(self, result, user=None):
		return sorted(i["name"] for i in result["invoices"] if not user or i["user"] == user)


class TestSalesStatistics(SalesStatsCase):
	def test_exact_totals_counts_and_units_per_salesman(self):
		a = self.sell([(self.w.tv, 1, 100), (self.w.service, 1, 5)])  # 105; the service is not a unit
		b = self.order([(self.w.ac, 2, 300), (self.w.tv, 1, 100), (self.w.pipe, 3, 2)], owner=SECOND)  # 706
		c = self.sell([(self.w.pipe, 2, 2)], owner=SECOND)  # 4; an accessory is non-AC

		result = self.stats()
		admin, second = self.row(result, "Administrator"), self.row(result, SECOND)
		self.assertEqual((admin["invoices"], admin["orders"], admin["returns"]), (1, 0, 0))
		self.assertEqual(admin["total"], 105.0)
		self.assertEqual(admin["net_total"], 105.0)
		self.assertEqual((admin["ac_units"], admin["non_ac_units"]), (0, 1))

		self.assertEqual((second["invoices"], second["orders"], second["returns"]), (2, 1, 0))
		self.assertEqual(second["total"], 710.0)
		self.assertEqual((second["ac_units"], second["non_ac_units"]), (2, 6))  # TV + 3 pipes + 2 pipes
		self.assertEqual(second["full_name"], "DM Salesman")

		self.assertEqual(result["totals"]["total"], 815.0)
		self.assertEqual(result["totals"]["invoices"], 3)
		self.assertEqual((result["totals"]["ac_units"], result["totals"]["non_ac_units"]), (2, 7))
		self.assertEqual(self.names(result), sorted([a, b, c]))

	def test_salesman_is_the_pos_cashier_on_a_shared_login(self):
		# One login (Administrator), the sale made by the cashier who verified a PIN (SECOND).
		shared = self.sell([(self.w.tv, 1, 100)])
		frappe.db.set_value("Sales Invoice", shared, "posa_cashier", SECOND, update_modified=False)
		# A credit note made in the desk has no POS cashier: it goes to whoever created it.
		desk = self.sell([(self.w.tv, 1, 120)])
		frappe.db.set_value("Sales Invoice", desk, "posa_cashier", None, update_modified=False)

		result = self.stats()
		self.assertEqual(self.names(result, SECOND), [shared])
		self.assertEqual(self.names(result, "Administrator"), [desk])
		self.assertEqual(self.row(result, SECOND)["total"], 100.0)
		self.assertEqual(self.row(result, "Administrator")["total"], 120.0)
		self.assertEqual(self.names(self.stats(user=SECOND)), [shared])

	def test_invoice_numbers_listed_with_their_own_numbers(self):
		b = self.order([(self.w.ac, 1, 300), (self.w.tv, 1, 100)], owner=SECOND)
		inv = next(i for i in self.stats()["invoices"] if i["name"] == b)
		self.assertEqual((inv["user"], inv["type"], inv["total"], inv["ac_units"], inv["non_ac_units"]),
			(SECOND, "Order", 400.0, 1, 1))
		self.assertEqual(inv["customer"], self.w.customer)

	def test_rows_always_equal_the_sum_of_their_invoices(self):
		self.sell([(self.w.tv, 1, 100)])
		self.order([(self.w.ac, 1, 300)], owner=SECOND)
		paid = self.sell([(self.w.tv, 1, 120)], owner=SECOND)
		self.credit_note(paid, self.w.tv, owner=SECOND)
		result = self.stats()
		for row in result["rows"]:
			mine = [i for i in result["invoices"] if i["user"] == row["user"]]
			for key in ("total", "net_total", "ac_units", "non_ac_units"):
				self.assertAlmostEqual(row[key], sum(i[key] for i in mine), places=3)
		for key in sales.SUM_KEYS:
			self.assertAlmostEqual(result["totals"][key], sum(r[key] for r in result["rows"]), places=3)

	# ------------------------------------------------------------- period

	def test_period_is_creation_date_not_posting_date(self):
		# The POS lets the cashier move the Posting Date (ACC-SINV-2026-00035 had the delivery day).
		name = self.sell([(self.w.tv, 1, 100)])
		frappe.db.set_value("Sales Invoice", name, "posting_date", add_days(self.today, 5), update_modified=False)
		self.assertIn(name, self.names(self.stats()))
		self.assertNotIn(name, self.names(self.stats(add_days(self.today, 5), add_days(self.today, 5))))

	def test_day_boundaries_are_inclusive(self):
		yesterday = add_days(self.today, -1)
		late = self.sell([(self.w.tv, 1, 100)])
		self.set_created(late, f"{yesterday} 23:59:59.999999")
		early = self.sell([(self.w.tv, 1, 100)])
		self.set_created(early, f"{self.today} 00:00:00")
		end = self.sell([(self.w.pipe, 1, 2)])
		self.set_created(end, f"{self.today} 23:59:59.999999")

		self.assertEqual(self.names(self.stats()), sorted([early, end]))
		self.assertEqual(self.names(self.stats(yesterday, yesterday)), [late])
		self.assertEqual(self.names(self.stats(yesterday, self.today)), sorted([late, early, end]))

	def test_long_period(self):
		old = self.sell([(self.w.tv, 1, 100)])
		self.set_created(old, f"{add_days(self.today, -200)} 10:00:00")
		self.assertIn(old, self.names(self.stats(add_days(self.today, -365), self.today)))
		self.assertNotIn(old, self.names(self.stats(add_days(self.today, -30), self.today)))

	# ------------------------------------------------------------- paid only

	def test_unpaid_invoice_not_counted_until_fully_paid(self):
		unpaid = self.order([(self.w.tv, 1, 100)], owner=SECOND, unpaid=True)
		self.assertIsNone(self.row(self.stats(), SECOND))

		frappe.db.set_value("Sales Invoice", unpaid, "outstanding_amount", 40)  # partly paid
		self.assertIsNone(self.row(self.stats(), SECOND))

		frappe.db.set_value("Sales Invoice", unpaid, "outstanding_amount", 0)  # fully paid
		second = self.row(self.stats(), SECOND)
		self.assertEqual((second["invoices"], second["total"]), (1, 100.0))

	def test_real_payment_entry_makes_it_count(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		unpaid = self.sell([(self.w.tv, 1, 100)], paid=False)
		self.assertEqual(frappe.db.get_value("Sales Invoice", unpaid, "outstanding_amount"), 100)
		self.assertNotIn(unpaid, self.names(self.stats()))
		pe = get_payment_entry("Sales Invoice", unpaid)
		pe.reference_no, pe.reference_date = "DM-TEST", nowdate()
		pe.insert()
		pe.submit()
		self.assertEqual(frappe.db.get_value("Sales Invoice", unpaid, "status"), "Paid")
		self.assertIn(unpaid, self.names(self.stats()))

	def test_draft_and_cancelled_never_count(self):
		draft = self.w.draft_invoice([(self.w.tv, 1, 100)])["name"]
		cancelled = self.sell([(self.w.tv, 1, 100)])
		frappe.get_doc("Sales Invoice", cancelled).cancel()
		names = self.names(self.stats())
		self.assertNotIn(draft, names)
		self.assertNotIn(cancelled, names)

	# ------------------------------------------------------------- returns

	def test_return_is_subtracted_from_money_and_units(self):
		sold = self.sell([(self.w.tv, 1, 100), (self.w.pipe, 2, 2)])  # 104
		ret = self.credit_note(sold, self.w.tv)  # -100
		result = self.stats()
		admin = self.row(result, "Administrator")
		self.assertEqual((admin["invoices"], admin["returns"]), (1, 1))
		self.assertEqual(admin["total"], 4.0)
		self.assertEqual(admin["non_ac_units"], 2)  # 1 TV + 2 pipes - 1 TV
		inv = next(i for i in result["invoices"] if i["name"] == ret)
		self.assertEqual((inv["type"], inv["total"], inv["return_against"]), ("Return", -100.0, sold))

	def test_return_against_unpaid_invoice_is_ignored(self):
		unpaid = self.sell([(self.w.tv, 1, 100), (self.w.pipe, 1, 2)], paid=False)
		ret = self.credit_note(unpaid, self.w.pipe)  # leaves 100 outstanding: still unpaid
		names = self.names(self.stats())
		self.assertNotIn(unpaid, names)
		self.assertNotIn(ret, names)

	# ------------------------------------------------------------- money details

	def test_total_is_the_rounded_amount_the_customer_pays(self):
		name = self.sell([(self.w.tv, 1, 100.4)])
		doc = frappe.get_doc("Sales Invoice", name)
		inv = next(i for i in self.stats()["invoices"] if i["name"] == name)
		expected = doc.base_grand_total if doc.disable_rounded_total else doc.base_rounded_total
		self.assertEqual(inv["total"], flt(expected, 3))
		self.assertEqual(inv["net_total"], 100.4)

	def test_amounts_are_in_company_currency(self):
		self.assertEqual(self.stats()["currency"], self.w.company.default_currency)

	# ------------------------------------------------------------- filters

	def test_filters_user_and_company(self):
		self.sell([(self.w.tv, 1, 100)])
		mine = self.sell([(self.w.tv, 1, 100)], owner=SECOND)
		only = self.stats(user=SECOND)
		self.assertEqual([r["user"] for r in only["rows"]], [SECOND])
		self.assertEqual(self.names(only), [mine])
		self.assertEqual(self.stats(company="_DM no such company")["rows"], [])
		self.assertEqual(self.stats(add_days(self.today, 1), add_days(self.today, 3))["rows"], [])


class TestSalesStatisticsApi(SalesStatsCase):
	def test_every_run_is_fresh(self):
		first = api.start(str(self.today), str(self.today), self.company)
		self.assertEqual(first["status"], "done")
		before = first["result"]["totals"]["invoices"]
		self.sell([(self.w.tv, 1, 100)])
		second = api.start(str(self.today), str(self.today), self.company)
		self.assertNotEqual(second["key"], first["key"])
		self.assertEqual(second["result"]["totals"]["invoices"], before + 1)
		self.assertEqual(api.status(second["key"])["status"], "done")

	def test_salesman_sees_only_own_numbers(self):
		self.sell([(self.w.tv, 1, 100)])
		mine = self.sell([(self.w.pipe, 1, 2)], owner=SECOND)
		admins_run = api.start(str(self.today), str(self.today), self.company)["key"]
		frappe.set_user(SECOND)
		try:
			result = api.start(str(self.today), str(self.today), self.company, user="Administrator")["result"]
			self.assertEqual([r["user"] for r in result["rows"]], [SECOND])
			self.assertEqual(self.names(result), [mine])
			self.assertRaises(frappe.PermissionError, api.status, admins_run)
		finally:
			frappe.set_user("Administrator")

	def test_bad_dates_refused(self):
		self.assertRaises(frappe.ValidationError, api.start, str(self.today), str(add_days(self.today, -1)), self.company)
		self.assertRaises(frappe.ValidationError, api.start, None, str(self.today), self.company)

	def test_expired_run(self):
		self.assertEqual(api.status("no-such-run")["status"], "expired")
