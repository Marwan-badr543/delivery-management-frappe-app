"""Sales statistics per salesman: paid Sales Invoices, by creation date, with exact money checks.

Money is asserted against hand-computed amounts (rates x qty), not re-read from the
database, so a wrong query cannot agree with itself.
"""

import frappe
from frappe.utils import add_days, flt, get_datetime, getdate, nowdate

from delivery_management.api import sales_stats as api
from delivery_management.delivery_management.doctype.sales_evaluation.sales_evaluation import CRITERIA
from delivery_management.orders import payments, service
from delivery_management.stats import sales, units
from delivery_management.tests.fixtures import item
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
		# Far from today: the site's own sales of today never mix into the numbers.
		self.today = getdate(add_days(nowdate(), 4000))
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
		"""``owner`` placed the sale, on ``self.today``: logged in and verified as the POS cashier."""
		frappe.db.set_value("Sales Invoice", name, {"owner": owner, "posa_cashier": owner,
			"creation": get_datetime(f"{self.today} 12:00:00")}, update_modified=False)

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

	def invoices(self, result, user=None, **kw):
		f = result["filters"]
		return sales.invoices(f["from_date"], f["to_date"], f["company"], user or f["user"], page_len=None, **kw)["invoices"]

	def names(self, result, user=None):
		return sorted(i["name"] for i in self.invoices(result, user))


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
		inv = next(i for i in self.invoices(self.stats()) if i["name"] == b)
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
			mine = self.invoices(result, row["user"])
			for key in ("total", "net_total", "ac_units", "non_ac_units"):
				self.assertAlmostEqual(row[key], sum(i[key] for i in mine), places=3)
		for key in sales.SUM_KEYS:
			self.assertAlmostEqual(result["totals"][key], sum(r[key] for r in result["rows"]), places=3)

	def test_invoice_list_is_paged_and_searchable(self):
		names = sorted(self.sell([(self.w.pipe, 1, 2)]) for _ in range(3))
		result = self.stats()
		f = result["filters"]
		first = sales.invoices(f["from_date"], f["to_date"], f["company"], page_len=2)
		second = sales.invoices(f["from_date"], f["to_date"], f["company"], page=1, page_len=2)
		self.assertEqual((first["count"], len(first["invoices"]), len(second["invoices"])), (3, 2, 1))
		self.assertEqual(sorted(i["name"] for i in first["invoices"] + second["invoices"]), names)
		self.assertEqual([i["name"] for i in self.invoices(result, search=names[1])], [names[1]])
		self.assertEqual(api.get_invoices(f["from_date"], f["to_date"], f["company"], all_pages=1)["count"], 3)

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

		payments.collect(unpaid, 60, "Cash", "test", "partly paid")
		self.assertIsNone(self.row(self.stats(), SECOND))

		payments.collect(unpaid, 40, "Cash", "test", "fully paid")
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
		inv = next(i for i in self.invoices(result) if i["name"] == ret)
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
		inv = next(i for i in self.invoices(self.stats()) if i["name"] == name)
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
		# Salesmen with a target are listed even with no sale, so check there are no sales.
		for result in (self.stats(company="_DM no such company"), self.stats(add_days(self.today, 1), add_days(self.today, 3))):
			self.assertEqual([r for r in result["rows"] if r["invoices"] or r["returns"]], [])


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


class TestCommissionRule(SalesStatsCase):
	"""Commission % = Min + (sold ÷ target) × (Max − Min), at most Max; paid 75% on it and 25% × evaluation."""

	def test_percent_between_min_and_max(self):
		self.assertEqual(sales.commission_percent(10, 20, 0, 2.5), 1.25)
		self.assertEqual(sales.commission_percent(0, 20, 1, 3), 1)
		self.assertEqual(sales.commission_percent(30, 20, 1, 3), 3)  # above the target: never over Max
		self.assertEqual(sales.commission_percent(5, 0, 1, 3), 3)  # no target: Max
		self.assertEqual(sales.commission_percent(-2, 20, 1, 3), 1)  # more returned than sold

	def test_amount_is_75_percent_plus_25_percent_of_the_score(self):
		self.assertEqual(sales.commission(10000, 1.25, 75), {"items": 93.75, "evaluation": 23.438, "amount": 117.188})
		self.assertEqual(sales.commission(10000, 2, 0)["amount"], 150.0)
		self.assertEqual(sales.commission(10000, 2, 100)["amount"], 200.0)
		self.assertEqual(sales.commission(-500, 2, 100)["amount"], 0.0)


class TestSalesTargetsAndCommission(SalesStatsCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_value("Item", self.w.pipe, "dm_small_item", 1)
		frappe.db.set_single_value("Delivery Settings", {"commission_min_percent": 1, "commission_max_percent": 3})
		frappe.db.set_value("User", SECOND, {"dm_sales_target": 1000, "dm_small_items_target": 10})

	def evaluate(self, points, from_date=None, to_date=None):
		doc = frappe.get_doc({"doctype": "Sales Evaluation", "salesman": SECOND,
			"from_date": from_date or self.today, "to_date": to_date or self.today,
			**{field: points for field in CRITERIA}}).insert()
		return doc

	def test_small_items_set_the_percent_and_evaluation_the_rest(self):
		self.sell([(self.w.pipe, 4, 25), (self.w.tv, 1, 100)], owner=SECOND)  # 200; 4 small items of 10
		self.evaluate(9)  # 81 / 90 = 90%
		row = self.row(self.stats(), SECOND)
		self.assertEqual((row["small_units"], row["small_items_target"], row["sales_target"]), (4, 10, 1000.0))
		self.assertAlmostEqual(row["commission_percent"], 1.8)  # 1 + 0.4 × (3 − 1)
		self.assertEqual(row["evaluation"]["score"], 90.0)
		# 200 × 1.8% = 3.6: 75% = 2.7, 25% × 90% = 0.81
		self.assertEqual(row["commission"]["total"], {"items": 2.7, "evaluation": 0.81, "amount": 3.51})

	def test_returned_small_items_are_taken_off(self):
		sold = self.sell([(self.w.pipe, 4, 25)], owner=SECOND)
		self.credit_note(sold, self.w.pipe, owner=SECOND)
		row = self.row(self.stats(), SECOND)
		self.assertEqual((row["small_units"], row["commission_percent"]), (0, 1))

	def test_no_evaluation_pays_only_the_75_percent(self):
		frappe.db.set_value("User", SECOND, "dm_small_items_target", 4)
		self.sell([(self.w.pipe, 4, 25)], owner=SECOND)  # 100, target reached: Max 3%
		row = self.row(self.stats(), SECOND)
		self.assertIsNone(row["evaluation"])
		self.assertEqual(row["commission"]["total"], {"items": 2.25, "evaluation": 0.0, "amount": 2.25})

	def test_latest_evaluation_overlapping_the_period(self):
		self.sell([(self.w.pipe, 1, 10)], owner=SECOND)
		self.evaluate(2, add_days(self.today, -40), add_days(self.today, -20))  # before the period: not used
		older = self.evaluate(5, add_days(self.today, -10), self.today)
		self.assertEqual(self.row(self.stats(), SECOND)["evaluation"]["name"], older.name)
		newer = self.evaluate(8, self.today, add_days(self.today, 10))
		self.assertEqual(self.row(self.stats(), SECOND)["evaluation"]["name"], newer.name)

	def test_salesman_with_a_target_is_listed_before_any_sale(self):
		row = self.row(self.stats(), SECOND)
		self.assertEqual((row["invoices"], row["total"], row["commission"]["total"]["amount"]), (0, 0.0, 0.0))

	def test_evaluation_score_and_limits(self):
		doc = self.evaluate(10)
		self.assertEqual((doc.total_points, doc.score), (90, 100.0))
		self.assertRaises(frappe.ValidationError, self.evaluate, 11)
		self.assertRaises(frappe.ValidationError, self.evaluate, 5, self.today, add_days(self.today, -1))

	def test_min_cannot_exceed_max(self):
		settings = frappe.get_single("Delivery Settings")
		settings.commission_min_percent, settings.commission_max_percent = 4, 2
		self.assertRaises(frappe.ValidationError, settings.validate_commission)


class TestLineUnits(SalesStatsCase):
	"""Units are frozen on each invoice line; a Product Bundle counts by its contents."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Item", self.w.pipe, "dm_small_item", 1)
		self.bundle = self.make_bundle("_DM AC Kit", [(self.w.ac, 1), (self.w.pipe, 3), (self.w.service, 1)])

	def make_bundle(self, code, parts):
		item(code, "_DM Goods", stock=0)
		if not frappe.db.exists("Product Bundle", code):
			frappe.get_doc({"doctype": "Product Bundle", "new_item_code": code,
				"items": [{"item_code": c, "qty": q} for c, q in parts]}).insert()
		return code

	def line_units(self, code, qty, is_return=0):
		doc = frappe.get_doc({"doctype": "Sales Invoice", "is_return": is_return,
			"items": [{"item_code": code, "qty": qty, "stock_qty": qty, "conversion_factor": 1}]})
		units.set_line_units(doc)
		row = doc.items[0]
		return (row.dm_ac_units, row.dm_non_ac_units, row.dm_small_units)

	def test_normal_items(self):
		self.assertEqual(self.line_units(self.w.ac, 2), (2, 0, 0))
		self.assertEqual(self.line_units(self.w.pipe, 4), (0, 4, 4))  # an accessory: non-AC and small
		self.assertEqual(self.line_units(self.w.service, 1), (0, 0, 0))

	def test_bundle_counts_its_contents(self):
		# 2 kits = 2 AC + 6 pipes (small); the fee in the kit is not a unit.
		self.assertEqual(self.line_units(self.bundle, 2), (2, 6, 6))

	def test_small_bundle_makes_all_its_contents_small(self):
		frappe.db.set_value("Item", self.bundle, "dm_small_item", 1)
		self.assertEqual(self.line_units(self.bundle, 2), (2, 6, 10))  # 2 AC + 6 pipes + 2 fees

	def test_disabled_bundle_is_a_plain_line(self):
		frappe.db.set_value("Product Bundle", self.bundle, "disabled", 1)
		self.assertEqual(self.line_units(self.bundle, 2), (0, 0, 0))

	def test_sale_keeps_its_units_when_the_item_changes(self):
		sold = self.sell([(self.w.pipe, 3, 10)], owner=SECOND)
		frappe.db.set_value("Item", self.w.pipe, "dm_small_item", 0)
		self.assertEqual(self.row(self.stats(), SECOND)["small_units"], 3)
		# The credit note takes off what the sale counted, not today's rule.
		self.credit_note(sold, self.w.pipe, owner=SECOND)
		self.assertEqual(self.row(self.stats(), SECOND)["small_units"], 0)


def item_group(name, parent="All Item Groups"):
	if not frappe.db.exists("Item Group", name):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": name, "parent_item_group": parent,
			"is_group": 1}).insert(ignore_permissions=True)
	return name


class TestServices(SalesStatsCase):
	def setUp(self):
		super().setUp()
		# The fee sits in a group under the Service Item Group: its children count too.
		root = item_group("_DM Services")
		frappe.db.set_value("Item", self.w.service, "item_group", item_group("_DM Fees", root))
		frappe.get_doc({"doctype": "Delivery Service Item Group", "parent": "Delivery Settings",
			"parenttype": "Delivery Settings", "parentfield": "service_item_groups", "item_group": root}).db_insert()
		frappe.db.set_single_value("Delivery Settings", {"commission_min_percent": 10, "commission_max_percent": 10})

	def test_services_are_left_out_by_default(self):
		self.assertIn("_DM Fees", sales.service_groups())
		name = self.sell([(self.w.tv, 1, 100), (self.w.service, 1, 5)])
		row = self.row(self.stats(), "Administrator")
		self.assertEqual((row["total"], row["net_total"], row["services"]), (100.0, 100.0, 5.0))
		self.assertEqual(row["commission"]["total"]["items"], 7.5)  # 100 × 10% × 75%
		[inv] = [i for i in self.invoices(self.stats()) if i["name"] == name]
		self.assertEqual((inv["total"], inv["services"]), (100.0, 5.0))

	def test_services_can_be_counted_in(self):
		self.sell([(self.w.tv, 1, 100), (self.w.service, 1, 5)])
		result = sales.compute(self.today, self.today, self.company, exclude_services=False)
		row = self.row(result, "Administrator")
		self.assertEqual((row["total"], row["net_total"], row["services"]), (105.0, 105.0, 5.0))
		self.assertEqual(result["filters"]["exclude_services"], 0)

	def test_returned_service_is_left_out_too(self):
		sale = self.sell([(self.w.tv, 1, 100), (self.w.service, 1, 5)])
		self.credit_note(sale, self.w.service)
		row = self.row(self.stats(), "Administrator")
		self.assertEqual((row["total"], row["services"]), (100.0, 0.0))

	def test_no_service_groups_counts_everything(self):
		frappe.db.delete("Delivery Service Item Group", {"parent": "Delivery Settings"})
		self.sell([(self.w.tv, 1, 100), (self.w.service, 1, 5)])
		self.assertEqual(self.row(self.stats(), "Administrator")["total"], 105.0)
