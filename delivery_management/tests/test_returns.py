"""Customer Return / Exchange from the POS: stock, accounts, the order and the shift.

Every case checks the real ledgers: the stock of each store, the cost the goods come
back at, the GL of every document (balanced, stock account = stock ledger), the
customer's balance, what POS Awesome offers as Customer Credit, the Sales Order and the
cashier's shift closing.
"""

import json

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe.utils import add_days, flt, getdate, nowdate

from delivery_management.api import drivers, returns as api
from delivery_management.orders import payments, service
from delivery_management.orders.stock import get_sellable_qty
from delivery_management.returns import service as returns
from delivery_management.slots import get_reservations
from delivery_management.tests.fixtures import PosWorld, receive, warehouse
from delivery_management.tests.test_orders import DATA, reserved_qty, scan, serial_status, tv_bundle
from delivery_management.tests.utils import DeliveryTestCase


def balance(customer):
	return flt(frappe.db.sql("""select sum(debit - credit) from `tabGL Entry` where party_type = 'Customer'
		and party = %s and is_cancelled = 0""", customer)[0][0], 3)


def stock(item_code, wh):
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": wh}, "actual_qty"))


def outstanding(invoice):
	return flt(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount"), 3)


def store_reserved(item_code, wh):
	"""What the store sees as reserved for Sales Orders (Bin), as opposed to the Stock
	Reservation Entries the POS sells around."""
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": wh}, "reserved_qty"))


class ReturnsTestCase(DeliveryTestCase):
	def setUp(self):
		super().setUp()
		self.w = PosWorld()
		self.settings(auto_create_delivery_note=1, auto_delivery_note_company=None)
		self.other = warehouse("_DM Branch 2")
		self.customer = self.w.customer
		self.slot_date = getdate(add_days(nowdate(), 200))
		self.area = self.make_area("_DM Area", cars=2, hours=("10:00", "16:00"))
		self.delivery = {"area": self.area, "block_no": "305", "road_no": "512", "building_no": "1102",
			"delivery_date": str(self.slot_date), "delivery_time": "10:00",
			"ac_date": str(add_days(self.slot_date, 1))}
		self.stock_gap_before = self.stock_gap()
		self.reserved_before = store_reserved(self.w.tv, self.w.main)

	def tearDown(self):
		# Whatever a test did, the stock ledger and the stock accounts still agree.
		self.assertAlmostEqual(self.stock_gap(), self.stock_gap_before, places=3)
		super().tearDown()

	# ------------------------------------------------------------------ sales

	def branch_sale(self, lines):
		return service.submit_pos_invoice(self.w.draft_invoice(lines), dict(DATA), {})["name"]

	def order(self, lines, unpaid=False, paid=None):
		invoice = self.w.draft_invoice(lines)
		if paid is not None:
			invoice["payments"][0]["amount"] = paid
		result = service.submit_pos_order(invoice, dict(DATA), self.delivery, unpaid=unpaid)
		return result["name"], result["sales_order"]

	def notes(self, so, docstatus=0):
		return frappe.get_all("Delivery Note Item", filters={"against_sales_order": so, "docstatus": docstatus},
			pluck="parent", distinct=True)

	def deliver(self, so, serials=None, qty=None):
		"""The store submits the order's draft notes, scanning ``serials`` ({item: [..]})."""
		for name in self.notes(so):
			dn = frappe.get_doc("Delivery Note", name)
			for code, sns in (serials or {}).items():
				if any(r.item_code == code for r in list(dn.items) + list(dn.get("packed_items") or [])):
					scan(dn, code, sns)
			for code, q in (qty or {}).items():
				for r in dn.items:
					if r.item_code == code:
						r.qty = q
			dn.save()
			dn.submit()

	# ------------------------------------------------------------------ returns

	def give_back(self, invoice, lines, refund="all", wh=None, **extra):
		"""``lines``: {item_code: (returned, cancelled[, serials])}."""
		sale = returns.get_sale(invoice)
		req = []
		for line in sale["lines"]:
			if line["item_code"] in lines:
				spec = lines[line["item_code"]]
				req.append({"si_detail": line["si_detail"], "returned_qty": spec[0], "cancelled_qty": spec[1],
					"serial_nos": spec[2] if len(spec) > 2 else None})
		data = {"sales_invoice": invoice, "pos_profile": self.w.pos_profile, "lines": req, "warehouse": wh, **extra}
		if refund == "all":
			data["refund_all"] = 1
		else:
			data["refund_amount"] = refund
		return returns.make_return(data)

	# ------------------------------------------------------------------ checks

	def stock_gap(self):
		"""Stock ledger value minus the stock accounts in the GL (0 when they agree)."""
		company = self.w.company.name
		sle = flt(frappe.db.sql("""select sum(stock_value_difference) from `tabStock Ledger Entry`
			where company = %s and is_cancelled = 0""", company)[0][0])
		gl = flt(frappe.db.sql("""select sum(g.debit - g.credit) from `tabGL Entry` g join `tabAccount` a on a.name = g.account
			where g.company = %s and a.account_type = 'Stock' and g.is_cancelled = 0""", company)[0][0])
		return sle - gl

	def assert_balanced(self, *vouchers):
		for v in vouchers:
			dr, cr = frappe.db.sql("""select sum(debit), sum(credit) from `tabGL Entry` where voucher_no = %s
				and is_cancelled = 0""", v)[0]
			self.assertAlmostEqual(flt(dr), flt(cr), places=3, msg=v)

	def gl(self, voucher, account_type):
		return flt(frappe.db.sql("""select sum(g.debit - g.credit) from `tabGL Entry` g join `tabAccount` a on a.name = g.account
			where g.voucher_no = %s and a.account_type = %s and g.is_cancelled = 0""", (voucher, account_type))[0][0], 3)

	def expected_cash(self):
		from posawesome.posawesome.doctype.pos_closing_shift.closing_processing.creation import (
			make_closing_shift_from_opening,
		)

		shift = frappe.get_doc("POS Opening Shift", self.w.opening_shift)
		closing = make_closing_shift_from_opening(json.dumps(shift.as_dict(), default=str))["closing_shift"]
		return sum(flt(p.expected_amount) for p in closing.payment_reconciliation if p.mode_of_payment == "Cash")

	def available_credit(self):
		"""Customer Credit as POS Awesome offers it (its endpoint is replaced by the app's)."""
		return flt(sum(r["total_credit"] for r in api.get_available_credit(self.customer, self.w.company.name)), 3)


class TestBranchReturn(ReturnsTestCase):
	def test_full_refund_same_branch(self):
		si = self.branch_sale([(self.w.tv, 1, 100), (self.w.pipe, 2, 5)])
		self.assertEqual((stock(self.w.tv, self.w.branch), stock(self.w.pipe, self.w.branch)), (1, 3))
		sn = returns.get_sale(si)["lines"][0]["serial_nos"]
		result = self.give_back(si, {self.w.tv: (1, 0, sn), self.w.pipe: (2, 0)})

		self.assertEqual((result["return_amount"], result["refunded_amount"], result["credit_amount"]), (110, 110, 0))
		self.assertEqual((stock(self.w.tv, self.w.branch), stock(self.w.pipe, self.w.branch)), (2, 5))
		self.assertEqual(serial_status(sn[0]), ("Active", self.w.branch))
		# Credit Note: sales and cost reversed at the cost the goods left with; cash out once.
		cn = result["credit_note"]
		self.assert_balanced(si, cn)
		self.assertEqual(self.gl(cn, "Stock"), -self.gl(si, "Stock"))
		self.assertEqual(self.gl(cn, "Cash"), -110)
		self.assertEqual((balance(self.customer), outstanding(si), outstanding(cn)), (0, 0, 0))
		self.assertEqual(self.available_credit(), 0)
		doc = frappe.get_doc("Customer Return", result["name"])
		self.assertEqual((doc.warehouse, doc.items[0].serial_no, doc.pos_opening_shift),
			(self.w.branch, sn[0], self.w.opening_shift))

	def test_returned_at_another_store(self):
		si = self.branch_sale([(self.w.pipe, 3, 5)])
		self.give_back(si, {self.w.pipe: (1, 0)}, wh=self.other)
		self.assertEqual((stock(self.w.pipe, self.w.branch), stock(self.w.pipe, self.other)), (2, 1))
		self.assertEqual(balance(self.customer), 0)

	def test_partial_then_rest_never_more_than_sold(self):
		si = self.branch_sale([(self.w.pipe, 3, 5)])
		self.give_back(si, {self.w.pipe: (2, 0)})
		self.assertEqual(returns.get_sale(si)["lines"][0]["max_return"], 1)
		self.assertRaisesRegex(frappe.ValidationError, "only 1 can be returned", self.give_back, si, {self.w.pipe: (2, 0)})
		self.give_back(si, {self.w.pipe: (1, 0)})
		self.assertEqual(stock(self.w.pipe, self.w.branch), 5)
		self.assertEqual(returns.get_sale(si)["lines"][0]["max_return"], 0)

	def test_keep_as_credit_then_exchange_pays_only_the_difference(self):
		si = self.branch_sale([(self.w.pipe, 2, 5)])
		cash_before = self.expected_cash()
		result = self.give_back(si, {self.w.pipe: (2, 0)}, refund=0, reason="Exchange")
		self.assertEqual((result["refunded_amount"], result["credit_amount"]), (0, 10))
		self.assertEqual((balance(self.customer), self.available_credit()), (-10, 10))
		self.assertEqual(self.expected_cash(), cash_before)  # no cash moved

		# The new sale uses the credit (POS Awesome "Customer Credit"); cash only for the rest.
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		invoice["payments"][0]["amount"] = 90
		data = dict(DATA, redeemed_customer_credit=10, customer_credit_dict=[{"type": "Invoice",
			"credit_origin": result["credit_note"], "total_credit": 10, "credit_to_redeem": 10}])
		new = service.submit_pos_invoice(invoice, data, {})["name"]
		self.assertEqual((outstanding(new), outstanding(result["credit_note"]), balance(self.customer)), (0, 0, 0))
		self.assertEqual(self.expected_cash(), cash_before + 90)

	def test_part_refund_part_credit(self):
		si = self.branch_sale([(self.w.pipe, 4, 5)])
		result = self.give_back(si, {self.w.pipe: (4, 0)}, refund=15)
		self.assertEqual((result["refunded_amount"], result["credit_amount"]), (15, 5))
		self.assertEqual((balance(self.customer), self.available_credit()), (-5, 5))

	def test_refund_never_more_than_the_return(self):
		si = self.branch_sale([(self.w.pipe, 2, 5)])
		self.assertRaisesRegex(frappe.ValidationError, "at most", self.give_back, si, {self.w.pipe: (1, 0)}, refund=6)

	def test_serial_must_be_one_sold_on_this_sale(self):
		si = self.branch_sale([(self.w.tv, 1, 100)])
		self.assertRaisesRegex(frappe.ValidationError, "was not sold on this sale", self.give_back, si,
			{self.w.tv: (1, 0, ["_DMTV-M-1"])})

	def test_a_return_cannot_be_returned(self):
		si = self.branch_sale([(self.w.pipe, 1, 5)])
		cn = self.give_back(si, {self.w.pipe: (1, 0)})["credit_note"]
		self.assertRaisesRegex(frappe.ValidationError, "already a return", returns.get_sale, cn)

	def test_retried_request_returns_the_first_result(self):
		si = self.branch_sale([(self.w.pipe, 3, 5)])
		first = self.give_back(si, {self.w.pipe: (1, 0)}, request_id="req-1")
		again = self.give_back(si, {self.w.pipe: (1, 0)}, request_id="req-1")
		self.assertEqual(first["name"], again["name"])
		self.assertEqual(stock(self.w.pipe, self.w.branch), 3)

	def test_shift_closing_counts_the_refund_once(self):
		before = self.expected_cash()
		si = self.branch_sale([(self.w.pipe, 4, 5)])
		self.assertEqual(self.expected_cash(), before + 20)
		self.give_back(si, {self.w.pipe: (2, 0)})
		self.assertEqual(self.expected_cash(), before + 10)


class TestOrderReturn(ReturnsTestCase):
	def test_delivered_order_returned_at_a_branch(self):
		si, so = self.order([(self.w.tv, 1, 100), (self.w.pipe, 2, 5)])
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]})
		self.assertEqual(returns.get_sale(si)["lines"][0]["serial_nos"], ["_DMTV-M-1"])
		result = self.give_back(si, {self.w.tv: (1, 0, ["_DMTV-M-1"]), self.w.pipe: (2, 0)}, wh=self.w.branch)

		# Stock back into the branch through a Return Delivery Note, at the delivered cost.
		self.assertEqual((stock(self.w.tv, self.w.branch), stock(self.w.pipe, self.w.branch)), (3, 7))
		self.assertEqual(serial_status("_DMTV-M-1"), ("Active", self.w.branch))
		ret = result["return_delivery_notes"][0]
		dn = self.notes(so, docstatus=1)
		self.assertEqual(self.gl(ret, "Stock"), -sum(self.gl(d, "Stock") for d in dn if d != ret))
		# The Credit Note moves no stock (it is the Return Delivery Note's job).
		cn = result["credit_note"]
		self.assertEqual((frappe.db.get_value("Sales Invoice", cn, "update_stock"), self.gl(cn, "Stock")), (0, 0))
		self.assert_balanced(si, cn, ret)
		self.assertEqual((balance(self.customer), result["refunded_amount"]), (0, 110))
		# Nothing left to deliver: closed, never delivered again, nothing reserved.
		self.assertEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")
		self.assertEqual(reserved_qty(so, self.w.tv), 0)

	def test_paid_order_cancelled_before_delivery(self):
		si, so = self.order([(self.w.tv, 2, 100)])
		self.assertEqual(reserved_qty(so, self.w.tv), 2)
		sellable = get_sellable_qty([self.w.tv], self.w.main)[self.w.tv]
		result = self.give_back(si, {self.w.tv: (0, 2)})

		self.assertEqual((result["refunded_amount"], balance(self.customer)), (200, 0))
		self.assertEqual(result["return_delivery_notes"], [])
		self.assertEqual(self.notes(so), [])  # the draft note is gone
		self.assertEqual(reserved_qty(so, self.w.tv), 0)
		self.assertEqual(get_sellable_qty([self.w.tv], self.w.main)[self.w.tv], sellable + 2)
		self.assertEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")
		self.assertEqual(get_reservations("Sales Order", so), [])  # the delivery slot is free again
		self.assertEqual(stock(self.w.tv, self.w.main), 3)

	def test_one_line_back_while_another_is_still_to_deliver(self):
		si, so = self.order([(self.w.tv, 1, 100), (self.w.pipe, 4, 5)])
		self.deliver(so, {self.w.tv: ["_DMTV-M-2"]}, qty={self.w.pipe: 1})  # 3 pipes not delivered yet
		self.give_back(si, {self.w.tv: (1, 0, ["_DMTV-M-2"])}, wh=self.w.main)

		# The order stays open for the pipes and keeps its delivery slot.
		self.assertNotEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")
		self.assertEqual([b.slot_type for b in get_reservations("Sales Order", so)], ["Delivery"])
		self.assertEqual(reserved_qty(so, self.w.tv), 0)
		# ERPNext still offers the returned TV for delivery: the note is refused.
		again = make_delivery_note(so)
		self.assertEqual({r.item_code: r.qty for r in again.items}, {self.w.tv: 1, self.w.pipe: 3})
		again.insert()
		scan(again, self.w.tv, ["_DMTV-M-3"])
		self.assertRaisesRegex(frappe.ValidationError, "only 0 left to deliver", again.submit)
		# The last pipes go out: the order closes by itself.
		again = frappe.get_doc("Delivery Note", again.name)
		again.set("items", [r for r in again.items if r.item_code == self.w.pipe])
		again.save()
		again.submit()
		self.assertEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")
		self.assertEqual(balance(self.customer), 0)
	def test_part_cancelled_before_delivery(self):
		si, so = self.order([(self.w.tv, 2, 100)])
		self.give_back(si, {self.w.tv: (0, 1)})
		self.assertEqual(reserved_qty(so, self.w.tv), 1)
		draft = frappe.get_doc("Delivery Note", self.notes(so)[0])
		self.assertEqual(draft.items[0].qty, 1)
		self.assertNotEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")
		self.assertEqual(len(get_reservations("Sales Order", so)), 1)  # still to deliver: slot kept
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]})
		self.assertEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")
		self.assertEqual(balance(self.customer), 0)

	def test_unpaid_order_delivered_and_returned(self):
		"""Nothing was paid: the TV comes back and simply comes off the draft bill."""
		si, so = self.order([(self.w.tv, 1, 100), (self.w.pipe, 2, 5)], unpaid=True)
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]})
		self.assertEqual(api.search_sales(self.w.pos_profile, si)[0]["outstanding_amount"], 110)
		cash = self.expected_cash()
		result = self.give_back(si, {self.w.tv: (1, 0)}, refund="all")
		self.assertEqual((result["return_amount"], result["cleared_amount"], result["refunded_amount"],
			result["credit_amount"], result["credit_note"]), (100, 100, 0, 0, None))
		draft = frappe.get_doc("Sales Invoice", si)
		self.assertEqual((draft.docstatus, [r.item_code for r in draft.items], draft.grand_total), (0, [self.w.pipe], 10))
		self.assertEqual((balance(self.customer), self.expected_cash()), (0, cash))
		self.assertEqual(serial_status("_DMTV-M-1"), ("Active", self.w.branch))
		# Driver collects the rest: the draft is submitted, then paid.
		dn = self.notes(so, docstatus=1)[0]
		self.assertEqual(drivers.payment_summary([dn])[dn]["outstanding"], 10)
		payments.collect(si, 10, "Cash", dn, "on delivery")
		self.assertEqual((frappe.db.get_value("Sales Invoice", si, "status"), balance(self.customer)), ("Paid", 0))

	def test_unpaid_order_all_cancelled_deletes_the_draft(self):
		si, so = self.order([(self.w.tv, 1, 100)], unpaid=True)
		first = self.give_back(si, {self.w.tv: (0, 1)})
		self.assertFalse(frappe.db.exists("Sales Invoice", si))
		self.assertEqual(frappe.db.get_value("Customer Return", first["name"], ["sales_invoice", "sales_order"]),
			(None, so))
		self.assertEqual((frappe.db.get_value("Sales Order", so, "status"), store_reserved(self.w.tv, self.w.main)),
			("Closed", self.reserved_before))

	def test_part_paid_order_refunds_only_what_was_paid(self):
		si, so = self.order([(self.w.tv, 1, 100)], unpaid=True)
		payments.collect(si, 40, "Cash", "x", "deposit")
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]})
		self.assertEqual(balance(self.customer), 60)
		self.assertRaisesRegex(frappe.ValidationError, "at most", self.give_back, si, {self.w.tv: (1, 0)}, refund=100)
		result = self.give_back(si, {self.w.tv: (1, 0)}, refund=40)
		self.assertEqual((result["cleared_amount"], result["refunded_amount"]), (60, 40))
		self.assertEqual((balance(self.customer), outstanding(si), outstanding(result["credit_note"])), (0, 0, 0))

	def test_returned_serial_must_have_been_delivered_on_this_order(self):
		si, so = self.order([(self.w.tv, 1, 100)])
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]})
		self.assertRaisesRegex(frappe.ValidationError, "was not sold on this sale", self.give_back, si,
			{self.w.tv: (1, 0, ["_DMTV-M-2"])})

	def test_nothing_delivered_cannot_be_returned_only_cancelled(self):
		si, so = self.order([(self.w.pipe, 2, 5)])
		self.assertRaisesRegex(frappe.ValidationError, "only 0 can be returned", self.give_back, si, {self.w.pipe: (1, 0)})

	def test_bundle_order_delivered_and_returned(self):
		bundle = tv_bundle(self.w)
		si, so = self.order([(bundle, 1, 150)])
		component = frappe.get_all("Product Bundle Item", filters={"parent": bundle}, fields=["item_code", "qty"])
		serial_item = next(c.item_code for c in component if frappe.db.get_value("Item", c.item_code, "has_serial_no"))
		self.deliver(so, {serial_item: ["_DMTV-M-1"]})
		before = {c.item_code: stock(c.item_code, self.w.main) for c in component}
		result = self.give_back(si, {bundle: (1, 0)}, wh=self.w.main)
		for c in component:
			self.assertEqual(stock(c.item_code, self.w.main), before[c.item_code] + c.qty)
		self.assertEqual((balance(self.customer), frappe.db.get_value("Sales Order", so, "status")), (0, "Closed"))
		self.assert_balanced(result["credit_note"], *result["return_delivery_notes"])

	def test_ac_cancelled_tv_still_delivered(self):
		si, so = self.order([(self.w.ac, 1, 300), (self.w.tv, 1, 100)])
		self.assertEqual(sorted(b.slot_type for b in get_reservations("Sales Order", so)), ["AC", "Delivery"])
		self.give_back(si, {self.w.ac: (0, 1)})
		# The AC installation day is free again; the TV keeps its delivery slot and note.
		self.assertEqual([b.slot_type for b in get_reservations("Sales Order", so)], ["Delivery"])
		notes = [frappe.get_doc("Delivery Note", n) for n in self.notes(so)]
		self.assertEqual([[r.item_code for r in n.items] for n in notes], [[self.w.tv]])
		self.assertEqual((reserved_qty(so, self.w.ac), reserved_qty(so, self.w.tv)), (0, 1))
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]})
		self.assertEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")

	def test_returned_goods_are_sellable_again(self):
		sellable = get_sellable_qty([self.w.tv], self.w.main)[self.w.tv]
		si, so = self.order([(self.w.tv, 1, 100)])
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]})
		self.give_back(si, {self.w.tv: (1, 0)}, wh=self.w.main)
		self.assertEqual(get_sellable_qty([self.w.tv], self.w.main)[self.w.tv], sellable)

	def test_two_returns_of_one_order(self):
		si, so = self.order([(self.w.pipe, 4, 5)])
		self.deliver(so)
		self.give_back(si, {self.w.pipe: (1, 0)}, wh=self.w.main)
		self.assertEqual(returns.get_sale(si)["lines"][0]["max_return"], 3)
		self.give_back(si, {self.w.pipe: (3, 0)}, wh=self.other)
		self.assertEqual((stock(self.w.pipe, self.w.main), stock(self.w.pipe, self.other)), (47, 3))
		self.assertEqual((balance(self.customer), frappe.db.get_value("Sales Order", so, "status")), (0, "Closed"))


class TestExchange(ReturnsTestCase):
	"""Return with the money kept as credit, then a new sale paid (partly) with it."""

	def credit(self):
		si = self.branch_sale([(self.w.pipe, 2, 5)])
		return self.give_back(si, {self.w.pipe: (2, 0)}, refund=0)["credit_note"]

	def with_credit(self, credit_note, amount):
		return dict(DATA, redeemed_customer_credit=amount, customer_credit_dict=[{"type": "Invoice",
			"credit_origin": credit_note, "total_credit": amount, "credit_to_redeem": amount}])

	def test_exchange_into_a_paid_delivery_order(self):
		cn = self.credit()
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		invoice["payments"][0]["amount"] = 90
		result = service.submit_pos_order(invoice, self.with_credit(cn, 10), self.delivery)
		self.assertEqual((outstanding(result["name"]), outstanding(cn), balance(self.customer)), (0, 0, 0))

	def test_exchange_into_an_order_paid_on_delivery(self):
		cn = self.credit()
		invoice = self.w.draft_invoice([(self.w.tv, 1, 100)])
		result = service.submit_pos_order(invoice, self.with_credit(cn, 10), self.delivery, unpaid=True)
		self.assertEqual((outstanding(result["name"]), outstanding(cn), balance(self.customer)), (90, 0, 90))

	def test_credit_bigger_than_the_new_sale(self):
		cn = self.credit()
		invoice = self.w.draft_invoice([(self.w.pipe, 1, 5)])
		invoice["payments"][0]["amount"] = 0
		result = service.submit_pos_invoice(invoice, self.with_credit(cn, 5), {})
		self.assertEqual((outstanding(result["name"]), outstanding(cn), balance(self.customer)), (0, -5, -5))
		self.assertEqual(self.available_credit(), 5)


class TestStoreReservation(ReturnsTestCase):
	"""Goods given back are not reserved for the order any more (Bin reserved qty, what the
	store sees), while the rest of the order stays reserved."""

	def test_returned_after_delivery_not_reserved_again(self):
		si, so = self.order([(self.w.tv, 2, 100)])
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]}, qty={self.w.tv: 1})
		self.assertEqual(store_reserved(self.w.tv, self.w.main), self.reserved_before + 1)
		self.give_back(si, {self.w.tv: (1, 0, ["_DMTV-M-1"])}, wh=self.w.main)
		self.assertEqual(store_reserved(self.w.tv, self.w.main), self.reserved_before + 1)  # only the pending one
		self.assertEqual(frappe.db.get_value("Sales Order Item", {"parent": so}, "returned_qty"), 1)

	def test_cancelled_before_delivery_not_reserved(self):
		si, so = self.order([(self.w.tv, 2, 100), (self.w.pipe, 2, 5)])
		self.give_back(si, {self.w.tv: (0, 1)})
		self.assertEqual(store_reserved(self.w.tv, self.w.main), self.reserved_before + 1)
		self.assertNotEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")

	def test_cancelled_then_delivered_one_returned(self):
		"""ERPNext recounts returned_qty from the Return Delivery Notes: the cancelled qty stays."""
		si, so = self.order([(self.w.tv, 3, 100)])
		self.give_back(si, {self.w.tv: (0, 1)})
		self.deliver(so, {self.w.tv: ["_DMTV-M-1"]}, qty={self.w.tv: 1})
		self.give_back(si, {self.w.tv: (1, 0, ["_DMTV-M-1"])}, wh=self.w.main)
		row = frappe.db.get_value("Sales Order Item", {"parent": so}, ["returned_qty", "dm_cancelled_qty"], as_dict=True)
		self.assertEqual((row.returned_qty, row.dm_cancelled_qty), (2, 1))
		self.assertEqual(store_reserved(self.w.tv, self.w.main), self.reserved_before + 1)
		self.assertEqual(returns.get_sale(si)["lines"][0]["max_cancel"], 1)


class TestCustomerMoney(ReturnsTestCase):
	"""Money at the till outside a sale: a payment on what a customer owes, and their
	credit paid out in cash. Both are in the cashier's shift."""

	def money(self, method, **data):
		data = {"pos_profile": self.w.pos_profile, "request_id": frappe.generate_hash(), **data}
		return getattr(api, method)(json.dumps(data))

	def test_customer_pays_an_unpaid_order_at_the_till(self):
		si, so = self.order([(self.w.tv, 1, 100)], unpaid=True)
		found = api.search_customers(self.w.pos_profile, self.customer)[0]
		self.assertEqual((found["due"], found["credit"]), (100, 0))
		cash = self.expected_cash()
		self.money("collect_payment", invoice=si, amount=60)
		self.assertEqual((outstanding(si), balance(self.customer), self.expected_cash()), (40, 40, cash + 60))
		self.assertRaisesRegex(frappe.ValidationError, "cannot be greater", self.money, "collect_payment",
			invoice=si, amount=41)

	def test_credit_paid_out_later_from_the_drawer(self):
		si = self.branch_sale([(self.w.pipe, 4, 5)])
		cn = self.give_back(si, {self.w.pipe: (4, 0)}, refund=0)["credit_note"]
		self.assertEqual(api.get_customer_money(self.w.pos_profile, self.customer)["credit"], 20)
		cash = self.expected_cash()
		self.assertRaisesRegex(frappe.ValidationError, "why", self.money, "pay_out_credit", customer=self.customer,
			amount=15)
		out = self.money("pay_out_credit", customer=self.customer, amount=15, reason="Came back next day")
		self.assertEqual(out["payment_type"], "Pay")
		self.assertEqual((outstanding(cn), balance(self.customer), self.available_credit()), (-5, -5, 5))
		self.assertEqual(self.expected_cash(), cash - 15)
		self.assertRaisesRegex(frappe.ValidationError, "most that can be paid out", self.money, "pay_out_credit",
			customer=self.customer, amount=6, reason="again")
		self.assert_balanced(out["payment_entry"])

	def test_pay_out_takes_the_oldest_credit_first(self):
		for qty in (1, 2):
			si = self.branch_sale([(self.w.pipe, qty, 5)])
			self.give_back(si, {self.w.pipe: (qty, 0)}, refund=0)
		notes = payments.open_credit_notes(self.customer, self.w.company.name)
		self.money("pay_out_credit", customer=self.customer, amount=8, reason="cash")
		self.assertEqual([outstanding(n.name) for n in notes], [0, -7])  # 5 then 10 of credit
		self.assertEqual(self.available_credit(), 7)

	def test_retried_request_pays_once(self):
		si, so = self.order([(self.w.tv, 1, 100)], unpaid=True)
		request_id = frappe.generate_hash()
		first = self.money("collect_payment", invoice=si, amount=30, request_id=request_id)
		frappe.db.after_commit.run()  # what a committed request leaves behind
		again = self.money("collect_payment", invoice=si, amount=30, request_id=request_id)
		self.assertEqual((first["payment_entry"], outstanding(si)), (again["payment_entry"], 70))
