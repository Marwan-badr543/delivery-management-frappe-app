"""Workshop spare parts: asked by the technician, approved by a System Manager, held, taken
by hand (serials scanned), returned; the cost goes on the ticket. Nothing leaves the store
any other way."""

import frappe
from frappe.utils import flt

from delivery_management.api import control
from delivery_management.api import spare_parts as sp_api
from delivery_management.api import support as api
from delivery_management.customer_support import spare_parts as sp
from delivery_management.tests.fixtures import item, receive, warehouse
from delivery_management.tests.test_support import SupportTestCase, user

SERIALS = ["_DM-BRD-001", "_DM-BRD-002", "_DM-BRD-003"]


class TestSpareParts(SupportTestCase):
	def setUp(self):
		super().setUp()
		self.boss = user("_dm_boss@example.com", "System Manager")
		self.maint2 = user("_dm_maint2@example.com", "Maintenance Technician")
		self.spare = warehouse("_DM Spare Parts")
		self.settings(spare_parts_warehouse=self.spare, spare_parts_expense_account=None)
		group = frappe.db.get_value("Item", self.w.pipe, "item_group")
		self.board = item("_DM Spare Board", group, serial=1)
		self.screw = item("_DM Spare Screw", group)
		receive(self.screw, self.spare, 10, rate=2)
		for s in SERIALS:
			if frappe.db.exists("Serial No", s):
				frappe.db.set_value("Serial No", s, "warehouse", None)
		receive(self.board, self.spare, 3, serials=SERIALS, rate=50)
		self.ticket = self.at_pickup(item_code=self.w.pipe)
		self.to_workshop(self.ticket)

	def qty(self, code):
		return flt(frappe.db.get_value("Bin", {"item_code": code, "warehouse": self.spare}, "actual_qty"))

	def ask(self, *lines, who=None, ticket=None):
		items = [{"item_code": c, "qty": q} for c, q in lines]
		return self.as_(who or self.maint, sp_api.request_parts, ticket or self.ticket, frappe.as_json(items), "Board burnt")

	def approved(self, *lines, **kw):
		name = self.ask(*lines, **kw)
		self.as_(self.boss, sp_api.approve, name)
		return name

	def test_whole_flow_cost_on_the_ticket(self):
		name = self.ask((self.screw, 4), (self.board, 1))
		self.assertEqual(frappe.db.get_value(sp.REQ, name, "status"), "Pending")
		self.assertEqual(self.qty(self.screw), 10)  # nothing moves on a request
		entry = self.as_(self.boss, sp_api.approve, name, frappe.as_json({self.screw: 3}))
		self.assertEqual(frappe.db.get_value("Stock Entry", entry, ["docstatus", "sm_spare_request"]), (0, name))
		self.assertEqual(self.qty(self.screw), 10)  # held, still in the store
		self.as_(self.maint, sp_api.take, name, frappe.as_json({self.board: [SERIALS[0]]}))
		self.assertEqual(frappe.db.get_value("Stock Entry", entry, "docstatus"), 1)
		self.assertEqual((self.qty(self.screw), self.qty(self.board)), (7, 2))
		self.assertEqual(frappe.db.get_value(sp.REQ, name, ["status", "total_cost"]), ("Taken", 56))
		self.as_(self.maint, sp_api.set_repair_cost, self.ticket, 20)
		self.assertEqual(frappe.db.get_value("Issue", self.ticket, ["sm_parts_cost", "sm_repair_cost", "sm_total_cost"]),
			(56, 20, 76))
		events = frappe.get_all("Order Log", filters={"support_ticket": self.ticket, "event": ["like", "Parts%"]}, pluck="event")
		self.assertEqual(set(events), {"Parts requested", "Parts approved", "Parts taken"})

	def test_return_lowers_the_cost_and_goes_back_in(self):
		name = self.approved((self.screw, 4), (self.board, 2))
		self.as_(self.maint, sp_api.take, name, frappe.as_json({self.board: SERIALS[:2]}))
		self.assertEqual(frappe.db.get_value("Issue", self.ticket, "sm_parts_cost"), 108)
		self.as_(self.maint, sp_api.return_parts, name, frappe.as_json([{"item_code": self.screw, "qty": 1},
			{"item_code": self.board, "qty": 1, "serials": [SERIALS[1]]}]))
		self.assertEqual((self.qty(self.screw), self.qty(self.board)), (7, 2))
		self.assertEqual(frappe.db.get_value("Serial No", SERIALS[1], "warehouse"), self.spare)
		self.assertEqual(frappe.db.get_value(sp.REQ, name, ["status", "total_cost"]), ("Taken", 56))
		self.assertEqual(frappe.db.get_value("Issue", self.ticket, "sm_parts_cost"), 56)
		# not more than what is out, nor a serial of someone else
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, sp_api.return_parts, name,
			frappe.as_json([{"item_code": self.screw, "qty": 4}]))
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, sp_api.return_parts, name,
			frappe.as_json([{"item_code": self.board, "qty": 1, "serials": [SERIALS[2]]}]))
		self.as_(self.maint, sp_api.return_parts, name, frappe.as_json([{"item_code": self.screw, "qty": 3},
			{"item_code": self.board, "qty": 1, "serials": [SERIALS[0]]}]))
		self.assertEqual(frappe.db.get_value(sp.REQ, name, ["status", "total_cost"]), ("Returned", 0))

	def test_held_parts_cannot_be_given_twice(self):
		self.ask((self.screw, 8))  # held from the request, before any approval
		other = self.at_pickup(item_code=self.w.pipe)
		self.to_workshop(other)
		self.assertRaises(frappe.ValidationError, self.ask, (self.screw, 3), who=self.maint2, ticket=other)
		ok = self.ask((self.screw, 2), who=self.maint2, ticket=other)
		self.assertEqual(sp.stock_of([self.screw], self.spare)[self.screw], {"in_store": 10, "held": 10, "free": 0})
		self.as_(self.boss, sp_api.approve, ok)  # its own hold is enough
		self.as_(self.boss, sp_api.reject, frappe.db.get_value(sp.REQ, {"ticket": self.ticket}, "name"), "Too many")
		self.assertEqual(sp.stock_of([self.screw], self.spare)[self.screw]["free"], 8)  # a rejection frees it

	def test_cannot_ask_more_than_free(self):
		self.assertRaises(frappe.ValidationError, self.ask, (self.screw, 11))
		first = self.ask((self.screw, 6))
		self.assertRaises(frappe.ValidationError, self.ask, (self.screw, 6))
		self.as_(self.boss, sp_api.approve, first, frappe.as_json({self.screw: 4}))  # lowered: 2 free again
		self.ask((self.screw, 6))

	def test_only_system_manager_approves_even_his_own(self):
		name = self.ask((self.screw, 1))
		self.assertRaises(frappe.PermissionError, self.as_, self.office, sp_api.approve, name)
		self.assertRaises(frappe.PermissionError, self.as_, self.maint, sp_api.approve, name)
		frappe.get_doc("User", self.boss).add_roles("Maintenance Technician")
		own = self.ask((self.screw, 1), who=self.boss)
		self.as_(self.boss, sp_api.approve, own)
		self.assertEqual(frappe.db.get_value(sp.REQ, own, "status"), "Approved")

	def test_only_the_requester_cancels_from_maintenance(self):
		name = self.approved((self.screw, 1))
		self.assertRaises(frappe.PermissionError, self.as_, self.boss, sp_api.cancel, name)
		self.assertRaises(frappe.PermissionError, self.as_, self.maint2, sp_api.cancel, name)
		can = lambda who, view: self.as_(who, api.get_ticket_detail, self.ticket, view)["parts"]["requests"][0]["can"]["cancel"]
		self.assertEqual((can(self.maint, "maintenance"), can(self.maint, "cs"), can(self.boss, "maintenance")), (True, False, False))
		self.as_(self.maint, sp_api.cancel, name)

	def test_reject_needs_a_reason(self):
		name = self.ask((self.screw, 1))
		self.assertRaises(frappe.ValidationError, self.as_, self.boss, sp_api.reject, name, " ")
		self.as_(self.boss, sp_api.reject, name, "Use the old one")
		self.assertEqual(frappe.db.get_value(sp.REQ, name, ["status", "reject_reason"]), ("Rejected", "Use the old one"))
		self.assertRaises(frappe.ValidationError, self.as_, self.boss, sp_api.approve, name)

	def test_serial_must_be_scanned_and_in_the_store(self):
		name = self.approved((self.board, 1))
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, sp_api.take, name, "{}")
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, sp_api.take, name,
			frappe.as_json({self.board: ["NOPE-1"]}))
		self.assertEqual(self.qty(self.board), 3)
		self.as_(self.maint, sp_api.take, name, frappe.as_json({self.board: [SERIALS[2]]}))
		self.assertFalse(frappe.db.get_value("Serial No", SERIALS[2], "warehouse"))

	def test_only_the_requester_takes(self):
		name = self.approved((self.screw, 1))
		self.assertRaises(frappe.PermissionError, self.as_, self.maint2, sp_api.take, name)
		self.as_(self.maint, sp_api.take, name)
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, sp_api.take, name)  # not twice

	def test_cannot_finish_repair_with_open_request(self):
		name = self.approved((self.screw, 1))
		self.proof(self.ticket, "repair", self.maint)
		self.assertRaises(frappe.ValidationError, self.as_, self.maint, api.finish_repair, self.ticket, "Repaired", "Done")
		self.as_(self.maint, sp_api.cancel, name)
		self.assertEqual(frappe.db.get_value(sp.REQ, name, ["status", "issue_entry"]), ("Cancelled", None))
		self.assertFalse(frappe.get_all("Stock Entry", filters={"sm_spare_request": name}))
		self.assertEqual(sp.stock_of([self.screw], self.spare)[self.screw]["held"], 0)
		self.as_(self.maint, api.finish_repair, self.ticket, "Repaired", "Done")
		self.assertEqual(self.step(self.ticket), "Ready for Return")

	def test_nothing_leaves_the_store_any_other_way(self):
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Issue", "company":
			frappe.db.get_value("Warehouse", self.spare, "company"),
			"items": [{"item_code": self.screw, "qty": 1, "s_warehouse": self.spare}]})
		self.assertRaises(frappe.ValidationError, se.insert)
		name = self.approved((self.screw, 2))
		draft = frappe.get_doc("Stock Entry", frappe.db.get_value(sp.REQ, name, "issue_entry"))
		draft.items[0].qty = 5
		self.assertRaises(frappe.ValidationError, draft.save)  # the draft cannot be edited from the desk
		self.as_(self.maint, sp_api.take, name)
		done = frappe.get_doc("Stock Entry", frappe.db.get_value(sp.REQ, name, "issue_entry"))
		self.assertRaises(frappe.ValidationError, done.cancel)

	def test_only_under_repair(self):
		ticket = self.at_pickup(item_code=self.w.pipe)
		self.assertRaises(frappe.ValidationError, self.ask, (self.screw, 1), ticket=ticket)

	def test_parts_inside_the_ticket(self):
		name = self.approved((self.screw, 1))
		parts = self.as_(self.maint, api.get_ticket_detail, self.ticket, "maintenance")["parts"]
		self.assertTrue(parts["can_request"] and parts["can_set_cost"] and parts["open"])
		req = parts["requests"][0]
		self.assertEqual((req["name"], req["can"]["take"], req["can"]["cancel"]), (name, True, True))
		self.assertFalse(self.as_(self.maint2, api.get_ticket_detail, self.ticket, "maintenance")["parts"]["requests"][0]["can"]["take"])
		found = self.as_(self.maint, sp_api.search_parts, "Spare Screw")
		self.assertEqual((found[0]["item_code"], found[0]["free"]), (self.screw, 9))
		pending = self.ask((self.board, 1))
		waiting = {r["name"]: r for r in self.as_(self.boss, sp_api.get_pending)}
		self.assertNotIn(frappe.db.get_value(sp.REQ, {"ticket": self.ticket, "status": "Approved"}), waiting)
		self.assertEqual(waiting[pending]["items"][0]["free"], 3)  # its own hold counts as free for it
		store = {r["item_code"]: r for r in self.as_(self.boss, sp_api.get_store)["rows"]}
		self.assertEqual((store[self.screw]["in_store"], store[self.screw]["held"]), (10, 1))
		self.assertEqual(store[self.board]["held"], 1)  # pending is held too

	def test_order_control_tabs_keep_tickets_apart(self):
		self.ask((self.screw, 1))
		orders = {r["name"] for r in control.get_orders(kind="orders")}
		tickets = {r["name"] for r in control.get_orders(kind="ticket")}
		self.assertIn(self.ticket, tickets)
		self.assertNotIn(self.ticket, orders)
		self.assertFalse(orders & tickets)
