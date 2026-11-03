"""Goods out with the driver: send out, take off the note at the customer, deliver, receive back."""

import frappe
from frappe.utils import flt

from delivery_management.api import drivers as api, stock_keeper
from delivery_management.returns import service as returns
from delivery_management.tests.fixtures import send_out
from delivery_management.tests.test_orders import reserved_qty, serial_status, tv_bundle
from delivery_management.tests.test_returns import ReturnsTestCase, balance, outstanding, stock

TRANSIT = "Delivery Transit"


class TransitTestCase(ReturnsTestCase):
	def setUp(self):
		super().setUp()
		self.transit = frappe.db.get_single_value("Delivery Settings", "transit_warehouse")

	def note(self, so):
		return self.notes(so)[0]

	def rows(self, name):
		return {r.item_code: r for r in frappe.get_doc("Delivery Note", name).items}

	def take_off(self, name, item_code, qty, serials=None):
		row = self.rows(name)[item_code]
		return api.take_off_note(name, [{"dn_detail": row.name, "qty": qty, "serials": serials or []}])

	def receive(self, name, scans, reserve_again=0):
		rows = {r.item_code: r.name for r in frappe.get_doc(TRANSIT, name).items}
		return stock_keeper.receive(name, {rows[code]: value for code, value in scans.items()}, reserve_again)


class TestSendOut(TransitTestCase):
	def test_goods_leave_the_main_store_and_the_reservation(self):
		_si, so = self.order([(self.w.tv, 2, 100), (self.w.pipe, 3, 5)])
		name = self.note(so)
		self.assertEqual(reserved_qty(so, self.w.tv), 2)
		send_out(name, {self.w.tv: ["_DMTV-M-2", "_DMTV-M-3"]})

		self.assertEqual(reserved_qty(so, self.w.tv), 0)
		self.assertEqual((stock(self.w.tv, self.w.main), stock(self.w.tv, self.transit)), (1, 2))
		self.assertEqual(stock(self.w.pipe, self.transit), 3)
		self.assertEqual(serial_status("_DMTV-M-2"), ("Active", self.transit))
		dn = frappe.get_doc("Delivery Note", name)
		self.assertEqual((dn.docstatus, dn.dm_delivery_status, dn.set_warehouse), (0, "On the way", self.transit))
		self.assertEqual(sorted(self.rows(name)[self.w.tv].serial_no.split("\n")), ["_DMTV-M-2", "_DMTV-M-3"])
		doc = frappe.get_doc(TRANSIT, name)
		self.assertEqual((doc.sales_order, doc.customer, doc.status), (so, self.customer, "On the way"))
		self.assertTrue(doc.driver and doc.out_entry)
		self.assertEqual(frappe.db.get_value("Stock Entry", doc.out_entry, "dm_delivery_note"), name)
		# Out of the store: the cashier cannot cancel it, nobody edits the note.
		self.assertEqual(returns.get_sale(_si)["lines"][0]["max_cancel"], 0)
		dn.remarks = "edit"
		self.assertRaisesRegex(frappe.ValidationError, "out of the store", dn.save)

	def test_every_serial_is_scanned_from_the_main_store(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		name = self.note(so)
		self.assertRaisesRegex(frappe.ValidationError, "scan 2 serial", send_out, name, {self.w.tv: ["_DMTV-M-1"]})
		self.assertRaisesRegex(frappe.ValidationError, "not in", send_out, name, {self.w.tv: ["_DMTV-M-1", "_DMTV-B-1"]})

	def test_not_submitted_before_it_is_sent_out(self):
		_si, so = self.order([(self.w.pipe, 1, 5)])
		dn = frappe.get_doc("Delivery Note", self.note(so))
		self.assertRaisesRegex(frappe.ValidationError, "Stock Keeper page", dn.submit)


class TestAtTheCustomer(TransitTestCase):
	def test_installed_in_full(self):
		_si, so = self.order([(self.w.tv, 1, 100)])
		name = self.note(so)
		send_out(name)
		api.mark_delivered_by_office(name)

		dn = frappe.get_doc("Delivery Note", name)
		self.assertEqual((dn.docstatus, dn.dm_delivery_status), (1, "Delivered"))
		self.assertEqual(stock(self.w.tv, self.transit), 0)
		self.assertFalse(frappe.db.exists(TRANSIT, name))
		self.assertEqual(frappe.db.get_value("Sales Order", so, "per_delivered"), 100)

	def test_part_not_installed_unpaid(self):
		si, so = self.order([(self.w.tv, 2, 100), (self.w.pipe, 3, 5)], unpaid=True)
		name = self.note(so)
		send_out(name, {self.w.tv: ["_DMTV-M-1", "_DMTV-M-2"]})
		cn = self.take_off(name, self.w.tv, 1, ["_DMTV-M-2"])["credit_notes"]

		# The customer owes only what they get; the order no longer delivers the TV.
		self.assertEqual(len(cn), 1)
		self.assertEqual((outstanding(si), balance(self.customer)), (115, 115))
		self.assertEqual(frappe.db.get_value("Sales Invoice", cn[0], "update_stock"), 0)
		tv = self.rows(name)[self.w.tv]
		self.assertEqual((tv.qty, tv.serial_no), (1, "_DMTV-M-1"))
		soi = frappe.db.get_value("Sales Order Item", tv.so_detail, ["dm_cancelled_qty", "returned_qty"])
		self.assertEqual(soi, (1, 1))
		self.assertEqual([d.name for d in api.sale_documents(frappe.get_doc("Delivery Note", name))], [si, cn[0]])

		api.mark_delivered_by_office(name)
		self.assertEqual(stock(self.w.tv, self.transit), 1)  # the TV is still coming back
		doc = frappe.get_doc(TRANSIT, name)
		self.assertEqual(doc.status, "Coming back")
		self.assertRaisesRegex(frappe.ValidationError, "not coming back", self.receive, name,
			{self.w.tv: ["_DMTV-M-1"]})
		self.receive(name, {self.w.tv: ["_DMTV-M-2"]})

		self.assertEqual((stock(self.w.tv, self.transit), stock(self.w.tv, self.w.main)), (0, 2))
		self.assertEqual(serial_status("_DMTV-M-2"), ("Active", self.w.main))
		self.assertFalse(frappe.db.exists(TRANSIT, name))
		# Delivered in full what is left on the order; it closes once paid.
		self.assertNotEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")
		api.record_payment(name, 115, "Cash")
		self.assertEqual(frappe.db.get_value("Sales Order", so, "status"), "Closed")

	def test_part_not_installed_paid_leaves_credit(self):
		si, so = self.order([(self.w.pipe, 3, 5)])
		name = self.note(so)
		send_out(name)
		self.take_off(name, self.w.pipe, 2)
		self.assertEqual((outstanding(si), balance(self.customer)), (0, -10))
		self.assertEqual(self.available_credit(), 10)

	def test_nothing_left_is_not_a_take_off(self):
		_si, so = self.order([(self.w.pipe, 3, 5)])
		name = self.note(so)
		send_out(name)
		self.assertRaisesRegex(frappe.ValidationError, "Customer Not Found", self.take_off, name, self.w.pipe, 3)
		self.assertRaisesRegex(frappe.ValidationError, "choose the 1 serial", self.take_off_tv_without_serials)

	def take_off_tv_without_serials(self):
		_si, so = self.order([(self.w.tv, 2, 100)])
		name = self.note(so)
		send_out(name)
		self.take_off(name, self.w.tv, 1)


class TestComingBack(TransitTestCase):
	def test_customer_not_found_reserved_again(self):
		si, so = self.order([(self.w.tv, 1, 100), (self.w.pipe, 2, 5)])
		name = self.note(so)
		send_out(name, {self.w.tv: ["_DMTV-M-3"]})
		api.bring_back_all(name)
		self.assertEqual(frappe.db.get_value("Delivery Note", name, "dm_delivery_status"), "Returned")
		self.assertRaises(frappe.ValidationError, api.mark_delivered_by_office, name)

		self.receive(name, {self.w.pipe: 2})
		self.assertTrue(frappe.db.exists(TRANSIT, name))  # the TV is still out
		self.receive(name, {self.w.tv: ["_DMTV-M-3"]}, reserve_again=1)

		self.assertFalse(frappe.db.exists(TRANSIT, name))
		dn = frappe.get_doc("Delivery Note", name)
		self.assertEqual((dn.docstatus, dn.dm_delivery_status or None, dn.set_warehouse), (0, None, self.w.main))
		self.assertFalse(self.rows(name)[self.w.tv].serial_no)
		self.assertEqual((reserved_qty(so, self.w.tv), reserved_qty(so, self.w.pipe)), (1, 2))
		self.assertEqual(outstanding(si), 0)  # nothing credited
		# Sent again another day.
		send_out(name)
		api.mark_delivered_by_office(name)
		self.assertEqual(frappe.db.get_value("Sales Order", so, "per_delivered"), 100)

	def test_customer_not_found_not_reserved(self):
		_si, so = self.order([(self.w.pipe, 2, 5)])
		name = self.note(so)
		send_out(name)
		api.bring_back_all(name)
		self.receive(name, {self.w.pipe: 2})
		self.assertEqual(reserved_qty(so, self.w.pipe), 0)
		self.assertEqual(stock(self.w.pipe, self.w.main), 50)

	def test_bundle_part_back(self):
		bundle = tv_bundle(self.w)
		si, so = self.order([(bundle, 2, 120)], unpaid=True)
		name = self.note(so)
		send_out(name, {self.w.tv: ["_DMTV-M-1", "_DMTV-M-2"]})
		self.assertEqual((stock(self.w.tv, self.transit), stock(self.w.pipe, self.transit)), (2, 4))
		self.take_off(name, bundle, 1, ["_DMTV-M-1"])
		api.mark_delivered_by_office(name)
		self.assertEqual((stock(self.w.tv, self.transit), stock(self.w.pipe, self.transit)), (1, 2))
		self.assertEqual(outstanding(si), 120)
		self.receive(name, {self.w.tv: ["_DMTV-M-1"], self.w.pipe: 2})
		self.assertEqual((stock(self.w.tv, self.transit), stock(self.w.pipe, self.transit)), (0, 0))
		self.assertFalse(frappe.db.exists(TRANSIT, name))

	def test_transit_list(self):
		_si, so = self.order([(self.w.pipe, 2, 5)])
		name = self.note(so)
		send_out(name)
		row = next(r for r in stock_keeper.get_transit() if r["name"] == name)
		self.assertEqual((row["items"][0]["qty"], row["items"][0]["coming_back"]), (2, 0))
		self.take_off(name, self.w.pipe, 1)
		row = next(r for r in stock_keeper.get_transit(coming_back_only=1) if r["name"] == name)
		self.assertEqual(flt(row["items"][0]["coming_back"]), 1)


class TestPosReturnStore(TransitTestCase):
	def test_goods_come_back_into_the_cashiers_branch(self):
		si = self.branch_sale([(self.w.pipe, 3, 5)])
		self.give_back(si, {self.w.pipe: (1, 0)}, wh=self.other)
		self.assertEqual((stock(self.w.pipe, self.w.branch), stock(self.w.pipe, self.other)), (3, 0))


class TestDriverAndUnits(TransitTestCase):
	def test_driver_changes_while_on_the_way(self):
		from delivery_management.drivers import assignment as asg
		from delivery_management.tests.test_drivers import user

		first, second = user("_dm.tv1@example.com", asg.ROLE_TV), user("_dm.tv2@example.com", asg.ROLE_TV)
		_si, so = self.order([(self.w.tv, 1, 100)])
		name = self.note(so)
		asg.assign(name, first)
		send_out(name)
		self.assertRaisesRegex(frappe.ValidationError, "change the driver instead", api.assign_driver, name, None)
		api.assign_driver(name, second)
		self.assertEqual(frappe.db.get_value(TRANSIT, name, "driver"), second)
		self.assertEqual(frappe.db.get_value("Driver Schedule", name, "driver"), second)
		api.mark_delivered_by_office(name)
		self.assertRaisesRegex(frappe.ValidationError, "is delivered", api.assign_driver, name, first)

	def test_ac_units_and_statistics_count_what_is_installed(self):
		from delivery_management.drivers import stats

		_si, so = self.order([(self.w.ac, 2, 300)])
		name = self.note(so)
		send_out(name, {self.w.ac: ["_DMAC-M-OLD1", "_DMAC-M-OLD2"]})
		self.take_off(name, self.w.ac, 1, ["_DMAC-M-OLD2"])
		self.assertEqual(frappe.db.get_value("Delivery Note", name, "dm_ac_units"), 1)
		api.mark_delivered_by_office(name)
		frappe.db.set_value("Delivery Note", name, "dm_driver", "Administrator")
		day = frappe.db.get_value("Delivery Note", name, "dm_ac_installation_date")
		self.assertEqual(stats.installation_stats(day, day, technician="Administrator")[0]["ac_units"], 1)


class TestDriverPrints(TransitTestCase):
	def test_invoice_and_credit_note_in_the_show_satellite_format(self):
		from unittest.mock import patch

		si, so = self.order([(self.w.pipe, 3, 5)], unpaid=True)
		name = self.note(so)
		send_out(name)
		cn = self.take_off(name, self.w.pipe, 1)["credit_notes"][0]
		printed = []
		with patch("frappe.get_print", lambda dt, dn, fmt, as_pdf: printed.append((dt, dn, fmt)) or b"%PDF"):
			for doc in (si, cn):
				api.print_sale_document(name, doc)
				self.assertEqual((frappe.local.response.type, frappe.local.response.filename), ("pdf", f"{doc}.pdf"))
		self.assertEqual(printed, [("Sales Invoice", si, "Show Satellite Tax Invoice"),
			("Sales Invoice", cn, "Show Satellite Tax Invoice")])
		self.assertRaises(frappe.PermissionError, api.print_sale_document, name, "SINV-NOT-OURS")
