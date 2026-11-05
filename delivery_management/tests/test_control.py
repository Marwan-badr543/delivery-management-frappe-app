"""Order Log and the Order Control page: each logistic and money step is logged once."""

import frappe

from delivery_management.api import control, drivers as api
from delivery_management.tests.fixtures import send_out
from delivery_management.tests.test_transit import TransitTestCase


class TestOrderLog(TransitTestCase):
	def steps(self, key, name):
		return frappe.get_all("Order Log", filters={key: name}, fields=["event", "amount", "mode_of_payment", "items",
			"user", "driver", "reference_name", "note"], order_by="at asc, creation asc")

	def events(self, key, name):
		return [s.event for s in self.steps(key, name)]

	def test_order_delivered(self):
		si, so = self.order([(self.w.tv, 1, 100)])
		name = self.note(so)
		send_out(name, {self.w.tv: ["_DMTV-M-2"]})
		api.mark_delivered_by_office(name)

		steps = self.steps("sales_order", so)
		self.assertEqual([s.event for s in steps], ["Order created", "Payment", "Sent out", "Delivered"])
		paid = steps[1]
		self.assertTrue(paid.amount > 0 and paid.mode_of_payment)
		sent = steps[2]
		self.assertIn("_DMTV-M-2", sent["items"])
		self.assertTrue(sent.driver and sent.reference_name)
		self.assertEqual(sent.user, frappe.session.user)

		order = control.get_order(so)
		self.assertEqual((order["kind"], order["invoices"], order["due"]), ("order", [si], 0))
		self.assertEqual(order["notes"][0]["where"], "With the customer")
		self.assertEqual(len(order["steps"]), 4)
		self.assertIn(so, [o.name for o in control.get_orders(search=so)])

	def test_items_not_delivered_and_customer_not_available(self):
		_si, so = self.order([(self.w.tv, 2, 100), (self.w.pipe, 3, 5)])
		name = self.note(so)
		send_out(name, {self.w.tv: ["_DMTV-M-2", "_DMTV-M-3"]})
		self.take_off(name, self.w.tv, 1, ["_DMTV-M-3"])
		api.bring_back_all(name)
		self.receive(name, {self.w.tv: ["_DMTV-M-2", "_DMTV-M-3"], self.w.pipe: 3}, reserve_again=1)

		steps = self.steps("sales_order", so)
		events = [s.event for s in steps]
		# The Credit Note is submitted inside the take-off step, so it is logged just before it.
		self.assertEqual(events[-5:], ["Sent out", "Credit note", "Not delivered items", "Customer not available",
			"Received back"])
		taken = next(s for s in steps if s.event == "Not delivered items")
		self.assertIn("_DMTV-M-3", taken["items"])
		self.assertTrue(taken.reference_name)
		back = steps[-1]
		self.assertEqual(back.note, "Reserved again for the order")

	def test_pos_return_of_a_branch_sale(self):
		si = self.branch_sale([(self.w.pipe, 2, 5)])
		self.give_back(si, {self.w.pipe: (1, 0)})

		events = self.events("sales_invoice", si)
		self.assertEqual(events[:2], ["Store sale", "Payment"])
		self.assertIn("POS return", events)
		self.assertIn("Credit note", events)
		self.assertIn("Refund", events)
		order = control.get_order(si)
		self.assertEqual(order["kind"], "sale")
		self.assertTrue(order["credited"] > 0)

	def test_failed_step_leaves_no_row(self):
		_si, so = self.order([(self.w.tv, 1, 100)])
		name = self.note(so)
		before = len(self.events("sales_order", so))
		self.assertRaises(frappe.ValidationError, send_out, name, {self.w.tv: ["_DMTV-B-1"]})
		self.assertEqual(len(self.events("sales_order", so)), before)

	def test_office_only(self):
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.PermissionError, control.get_orders)
		finally:
			frappe.set_user("Administrator")
