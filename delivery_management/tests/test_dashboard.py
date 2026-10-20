"""Sales Dashboard: invoice counts by status and branch, and the top products."""

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe.utils import nowdate

from delivery_management.orders import service
from delivery_management.stats import dashboard
from delivery_management.tests.test_orders import DATA, OrdersTestCase


class TestSalesDashboard(OrdersTestCase):
	def sell(self, lines):
		return service.submit_pos_invoice(self.w.draft_invoice(lines), dict(DATA))["name"]

	def counts(self):
		today = nowdate()
		return ({r.status: r.count for r in dashboard.status_stats(today, today)},
			{a["area"]: a["count"] for a in dashboard.area_stats(today, today)},
			{p.item_code: p.qty for p in dashboard.top_products(today, today, self.w.pos_profile)})

	def test_sales_count_by_branch_and_product(self):
		status, areas, top = self.counts()
		self.sell([(self.w.tv, 1, 100)])
		cancelled = self.sell([(self.w.tv, 1, 100)])
		frappe.get_doc("Sales Invoice", cancelled).cancel()

		status2, areas2, top2 = self.counts()
		self.assertEqual(status2.get("Paid", 0) - status.get("Paid", 0), 2 - 1)
		self.assertEqual(status2.get("Cancelled", 0) - status.get("Cancelled", 0), 1)
		# The cancelled sale counts neither for the branch nor for the products.
		self.assertEqual(areas2[self.w.pos_profile] - areas.get(self.w.pos_profile, 0), 1)
		self.assertEqual(top2[self.w.tv] - top.get(self.w.tv, 0), 1)
		self.assertIn(dashboard.ONLINE, areas2)

	def test_summary_counts_draft_delivery_notes_as_pending(self):
		so = service.submit_pos_order(self.w.draft_invoice([(self.w.tv, 1, 100)]), dict(DATA),
			{**self.delivery, "ac_date": None})["sales_order"]
		before = dashboard.summary()["pending_orders"]
		make_delivery_note(so).insert()
		self.assertEqual(dashboard.summary()["pending_orders"], before + 1)
