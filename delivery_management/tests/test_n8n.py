"""POS sales posted to n8n: the payload, the delay and the sent flag. No real HTTP or PDF."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_to_date, now_datetime

from delivery_management.orders import n8n, service
from delivery_management.tests.test_orders import DATA, OrdersTestCase

URL = "https://n8n.invalid/webhook/orders"


class TestN8n(OrdersTestCase):
	def setUp(self):
		super().setUp()
		self.settings(n8n_webhook_url=URL, n8n_delay_minutes=5, auto_create_delivery_note=1, auto_delivery_note_company=None)
		# Invoices already on the dev site are not the tests' business.
		frappe.db.sql(f"update `tabSales Invoice` set {n8n.SENT_FIELD} = 1")
		self.post = MagicMock()
		# send_pending commits after each invoice; the tests must stay in their rolled-back transaction.
		for patcher in (patch("requests.post", self.post), patch("frappe.get_print", return_value=b"%PDF"),
				patch.object(frappe.db, "commit")):
			patcher.start()
			self.addCleanup(patcher.stop)

	def age(self, name, minutes):
		frappe.db.set_value("Sales Invoice", name, "creation", add_to_date(now_datetime(), minutes=-minutes),
			update_modified=False)

	def order(self):
		invoice = self.w.draft_invoice([(self.w.ac, 1, 300), (self.w.tv, 2, 100)])
		invoice["contact_mobile"] = "36001122"
		invoice["posa_notes"] = "Username (1): ERP-9199"
		return service.submit_pos_order(invoice, DATA, self.delivery)

	def test_order_payload_and_files(self):
		result = self.order()
		self.age(result["name"], 6)
		n8n.send_pending()

		self.post.assert_called_once()
		args, kwargs = self.post.call_args
		data, files = kwargs["data"], kwargs["files"]
		self.assertEqual(args[0], URL)
		self.assertEqual(data["invoice_number"], result["name"])
		self.assertEqual(data["customer_phone"], "97336001122")
		self.assertEqual(data["products"], f"{self.w.ac}, {self.w.tv}")
		self.assertEqual(data["quantity"], "1.0, 2.0")
		self.assertEqual(data["xtream_info"], "Username (1): ERP-9199")
		self.assertEqual(data["item_categories"], "['AC']")  # no TV items configured
		self.assertEqual((data["delivery_method"], data["block_no"], data["delivery_time"]),
			("delivery", "305", "10:00 AM"))
		self.assertTrue(all(isinstance(v, str) for v in data.values()))
		self.assertEqual(set(files), {"invoice_file_pdf", "dn_file_pdf"})
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], n8n.SENT_FIELD), 1)

		n8n.send_pending()
		self.post.assert_called_once()  # sent once only

	def test_waits_for_the_delay(self):
		result = self.order()
		self.age(result["name"], 2)
		n8n.send_pending()
		self.post.assert_not_called()

	def test_pickup_invoice_has_no_delivery_note(self):
		result = service.submit_pos_invoice(self.w.draft_invoice([(self.w.tv, 1, 100)]), DATA, {})
		self.age(result["name"], 6)
		n8n.send_pending()
		kwargs = self.post.call_args.kwargs
		self.assertEqual(set(kwargs["files"]), {"invoice_file_pdf"})
		self.assertEqual(kwargs["data"]["delivery_method"], "pickup")

	def test_failed_send_is_retried(self):
		result = self.order()
		self.age(result["name"], 6)
		self.post.return_value.raise_for_status.side_effect = ConnectionError("n8n down")
		n8n.send_pending()
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], n8n.SENT_FIELD), 0)

	def test_no_url_sends_nothing(self):
		self.settings(n8n_webhook_url=None)
		self.age(self.order()["name"], 6)
		n8n.send_pending()
		self.post.assert_not_called()
