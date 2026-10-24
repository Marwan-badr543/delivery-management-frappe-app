"""TV accounts for POS sales: which items count, numbering, notes and clean-up.

The accounts database is replaced by an in-memory fake; tests never connect out.
"""

from unittest.mock import patch

import frappe

from delivery_management.orders import service, tv_accounts
from delivery_management.tests.fixtures import PosWorld, item, item_group
from delivery_management.tests.test_orders import DATA
from delivery_management.tests.utils import DeliveryTestCase


class FakeAccountsDB:
	"""The ``lines`` table: (member_id, username, password) rows."""

	def __init__(self, rows=()):
		self.rows = list(rows)
		self.down = False

	def connect(self):
		if self.down:
			raise ConnectionError("accounts database unreachable")
		return _FakeConn(self)


class _FakeConn:
	def __init__(self, db):
		self.db = db
		self.pending = []

	def cursor(self):
		return _FakeCursor(self)

	def commit(self):
		self.db.rows += self.pending
		self.pending = []

	def rollback(self):
		self.pending = []

	def close(self):
		pass


class _FakeCursor:
	def __init__(self, conn):
		self.conn = conn
		self.result = None

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False

	def execute(self, sql, args):
		rows = self.conn.db.rows
		if sql.startswith("select"):
			member, prefix = args
			mine = [r for r in rows if r[0] == member and r[1].startswith(prefix[:-1])]
			self.result = (mine[-1][1],) if mine else None
		elif sql.startswith("delete"):
			member, usernames = args
			self.conn.db.rows = [r for r in rows if not (r[0] == member and r[1] in usernames)]

	def executemany(self, sql, values):
		self.conn.pending += [(v[0], v[1], v[2]) for v in values]

	def fetchone(self):
		return self.result


class TVAccountsTestCase(DeliveryTestCase):
	def setUp(self):
		super().setUp()
		self.w = PosWorld()
		item_group("_DM TV Group", is_group=1)
		item_group("_DM Smart TV", parent="_DM TV Group")
		self.smart_tv = item("_DM Smart TV 55", "_DM Smart TV")
		self.remote = item("_DM TV Remote", "_DM Smart TV", serial=0)
		settings = frappe.get_single("Delivery Settings")
		settings.set("tv_items", [{"item_code": self.w.tv}])
		settings.set("tv_item_groups", [{"item_group": "_DM TV Group"}])
		settings.set("tv_excluded_items", [{"item_code": self.remote}])
		settings.update({"tv_account_host": "accounts.invalid", "tv_account_member_id": 1410})
		settings.save()
		self.accounts = FakeAccountsDB([(1410, "ERP-9198", "x"), (999, "ERP-50000", "x")])
		patcher = patch.object(tv_accounts.AccountsDB, "connect", lambda _db: self.accounts.connect())
		patcher.start()
		self.addCleanup(patcher.stop)


class TestTVClassification(TVAccountsTestCase):
	def test_listed_item_group_member_and_exclusion(self):
		codes = tv_accounts.get_tv_item_codes([self.w.tv, self.smart_tv, self.remote, self.w.ac])
		self.assertEqual(codes, {self.w.tv, self.smart_tv})

	def test_units_follow_stock_qty(self):
		lines = [{"item_code": self.smart_tv, "qty": 2, "conversion_factor": 1},
			{"item_code": self.remote, "qty": 3}, {"item_code": self.w.ac, "qty": 1}]
		self.assertEqual(tv_accounts.tv_units(lines), 2)


class TestTVAccountsOnPosSale(TVAccountsTestCase):
	def sale(self, lines, mobile="36001122"):
		invoice = self.w.draft_invoice(lines)
		invoice["contact_mobile"] = mobile
		return invoice

	def test_invoice_gets_one_account_per_unit_in_its_notes(self):
		invoice = self.sale([(self.w.tv, 2, 100), (self.w.pipe, 1, 2)])
		result = service.submit_pos_invoice(invoice, DATA, {})

		notes = frappe.db.get_value("Sales Invoice", result["name"], "posa_notes")
		self.assertEqual(notes, "Username (1): ERP-9199\nPassword (1): 36001122\n"
			"Username (2): ERP-9200\nPassword (2): 36001122")
		self.assertIn((1410, "ERP-9200", "36001122"), self.accounts.rows)

	def test_order_puts_the_accounts_on_the_sales_order_too(self):
		invoice = self.sale([(self.w.tv, 1, 100)])
		invoice["posa_notes"] = "Call before coming"
		result = service.submit_pos_order(invoice, DATA, {"block_no": "1", "road_no": "2", "building_no": "3",
			"area": self.make_area(), "delivery_date": "2031-03-04", "delivery_time": "10:00"})

		expected = "Call before coming\nUsername (1): ERP-9199\nPassword (1): 36001122"
		self.assertEqual(frappe.db.get_value("Sales Order", result["sales_order"], "posa_notes"), expected)
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], "posa_notes"), expected)

	def test_no_tv_no_account(self):
		service.submit_pos_invoice(self.sale([(self.w.pipe, 1, 2)]), DATA, {})
		self.assertEqual(len(self.accounts.rows), 2)

	def test_failed_sale_deletes_its_accounts(self):
		invoice = self.sale([(self.w.tv, 1, 100), (self.w.ac, 2, 300)])  # branch has one AC only
		self.assertRaises(frappe.ValidationError, service.submit_pos_invoice, invoice, DATA, {})
		self.assertEqual(len(self.accounts.rows), 2)  # stock is checked before any account is made

		invoice = self.sale([(self.w.tv, 1, 100)])
		tv_accounts.add_to_pos_sale(invoice)
		self.assertEqual(len(self.accounts.rows), 3)
		frappe.db.rollback()
		self.assertEqual(len(self.accounts.rows), 2)

	def test_sale_goes_on_when_the_accounts_database_is_down(self):
		self.accounts.down = True
		result = service.submit_pos_invoice(self.sale([(self.w.tv, 1, 100)]), DATA, {})
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], "docstatus"), 1)
		self.assertFalse(frappe.db.get_value("Sales Invoice", result["name"], "posa_notes"))

	def test_desk_order_gets_no_account(self):
		so = frappe.get_doc({"doctype": "Sales Order", "customer": self.w.customer, "company": self.w.company.name,
			"delivery_date": "2031-03-04", "set_warehouse": self.w.main,
			"items": [{"item_code": self.w.tv, "qty": 1, "rate": 100}]}).insert()
		self.assertFalse(so.posa_notes)
		self.assertEqual(len(self.accounts.rows), 2)

	def test_not_configured_makes_nothing(self):
		self.settings(tv_account_host=None)
		service.submit_pos_invoice(self.sale([(self.w.tv, 1, 100)]), DATA, {})
		self.assertEqual(len(self.accounts.rows), 2)
