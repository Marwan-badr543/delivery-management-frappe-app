"""Phase 3: Delivery Note order type, driver availability / assignment, permissions,
installation statistics, POS order rules (AC/non-AC guard, delivery charge), bundles."""

import datetime

import frappe
from frappe.model.workflow import apply_workflow
from frappe.utils import add_days, getdate, nowdate

from delivery_management.api import drivers as api
from delivery_management.api.pos import check_pos_order_rules, item_rules
from delivery_management.drivers import assignment as asg
from delivery_management.drivers import stats
from delivery_management.drivers.delivery_note import get_permission_query_conditions, only_own_orders
from delivery_management.slots.cart import profile_cart, unit_counts
from delivery_management.tests.fixtures import company, customer, item, item_group, receive, warehouse
from delivery_management.tests.utils import DeliveryTestCase

DAY = getdate(add_days(nowdate(), 220))


def user(email, *roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0, "user_type": "System User"}).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles(*roles)
	return email


class DriversTestCase(DeliveryTestCase):
	def setUp(self):
		super().setUp()
		self.main = warehouse("_DM Main")
		item_group("_DM AC", is_group=1)
		item_group("_DM AC Units", parent="_DM AC")
		item_group("_DM AC Accessory", parent="_DM AC")
		item_group("_DM Goods")
		# Non-serial stock items, so Delivery Notes can be submitted without serial picking.
		self.ac = item("_DM Window AC", "_DM AC Units")
		self.tv = item("_DM TV Bracket", "_DM Goods")
		self.pipe = item("_DM Copper Pipe", "_DM AC Accessory")
		self.charge = item("_DM Delivery Charge", "_DM Goods", stock=0)
		for code in (self.ac, self.tv, self.pipe):
			receive(code, self.main, 50)
		settings = frappe.get_single("Delivery Settings")
		settings.set("ac_item_groups", [{"item_group": "_DM AC"}])
		settings.set("ac_excluded_item_groups", [{"item_group": "_DM AC Accessory"}])
		settings.set("ac_excluded_items", [])
		settings.delivery_charge_item = self.charge
		settings.ac_tech_max_orders_per_day = 2
		settings.save()
		frappe.clear_document_cache("Delivery Settings", "Delivery Settings")
		self.customer = customer()
		self.tv1 = user("_dm.tv1@example.com", asg.ROLE_TV)
		self.tv2 = user("_dm.tv2@example.com", asg.ROLE_TV)
		self.ac1 = user("_dm.ac1@example.com", asg.ROLE_AC)
		self.ac2 = user("_dm.ac2@example.com", asg.ROLE_AC)

	def dn(self, lines, time="10:00", day=DAY, ac_day=None, **extra):
		doc = frappe.get_doc({
			"doctype": "Delivery Note", "company": company().name, "customer": self.customer,
			"set_warehouse": self.main, "posting_date": nowdate(),
			"dm_delivery_area": None, "dm_delivery_date": day, "dm_delivery_time": time,
			"dm_ac_installation_date": ac_day,
			"items": [{"item_code": code, "qty": qty, "rate": 10, "warehouse": self.main, "uom": "Nos",
				"conversion_factor": 1} for code, qty in lines],
			**extra,
		})
		doc.insert()
		return doc

	def send(self, dn):
		"""The stock keeper's workflow step: submit the note as On the way."""
		dn.reload()
		apply_workflow(dn, "On the way")


class TestOrderType(DriversTestCase):
	def test_type_from_items(self):
		self.assertEqual(self.dn([(self.ac, 1)]).dm_delivery_type, "AC")
		self.assertEqual(self.dn([(self.tv, 1)]).dm_delivery_type, "Non AC")
		# Accessories go with the installation; alone they are normal goods.
		self.assertEqual(self.dn([(self.ac, 1), (self.pipe, 2)]).dm_delivery_type, "AC")
		self.assertEqual(self.dn([(self.pipe, 2)]).dm_delivery_type, "Non AC")
		# A non-stock delivery charge never decides the type.
		self.assertEqual(self.dn([(self.ac, 1), (self.charge, 1)]).dm_delivery_type, "AC")

	def test_mixed_order_has_no_type_until_chosen(self):
		dn = self.dn([(self.ac, 1), (self.tv, 1)])
		self.assertFalse(dn.dm_delivery_type)
		dn.remarks = "edit"
		self.assertRaises(frappe.ValidationError, dn.save)
		dn.reload()
		dn.dm_delivery_type = "AC"
		dn.save()
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "dm_delivery_type"), "AC")

	def test_schedule_copied_from_sales_order(self):
		so = frappe.get_doc({
			"doctype": "Sales Order", "company": company().name, "customer": self.customer,
			"delivery_date": DAY, "transaction_date": nowdate(), "set_warehouse": self.main,
			"items": [{"item_code": self.tv, "qty": 1, "rate": 10, "warehouse": self.main, "delivery_date": DAY}],
		})
		so.insert()
		frappe.db.set_value("Sales Order", so.name, {"dm_delivery_date": DAY, "dm_delivery_time": "16:00",
			"dm_block_no": "305", "dm_road_no": "12", "dm_building_no": "7"})
		so.reload()
		so.flags.ignore_validate = True
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		frappe.db.set_value("Sales Order", so.name, "docstatus", 1)
		dn = make_delivery_note(so.name)
		dn.insert()
		self.assertEqual((str(dn.dm_delivery_time)[:5], dn.dm_block_no, dn.dm_delivery_type), ("16:00", "305", "Non AC"))

	def test_bundle_counts_by_components(self):
		bundle = item("_DM TV Bundle", "_DM Goods", stock=0)
		if not frappe.db.exists("Product Bundle", bundle):
			frappe.get_doc({"doctype": "Product Bundle", "new_item_code": bundle,
				"items": [{"item_code": self.tv, "qty": 2}, {"item_code": self.pipe, "qty": 1}]}).insert()
		p = profile_cart([{"item_code": bundle, "qty": 1}])
		self.assertTrue(p.has_non_ac)
		self.assertEqual(unit_counts([{"item_code": bundle, "qty": 2}]), (0, 6))
		self.assertEqual(asg.order_type_of([{"item_code": bundle, "qty": 1}]), "Non AC")

	def test_service_item_groups_are_not_units(self):
		item_group("_DM Services", is_group=1)
		item_group("_DM Install Fee", parent="_DM Services")
		item_group("_DM AC Service", parent="_DM AC")
		fee = item("_DM Install Fee Item", "_DM Install Fee")
		ac_service = item("_DM AC Cleaning", "_DM AC Service", stock=0)
		self.assertEqual(unit_counts([{"item_code": self.ac, "qty": 1}, {"item_code": ac_service, "qty": 3},
			{"item_code": self.tv, "qty": 2}, {"item_code": fee, "qty": 5}]), (1, 2))


class TestAvailability(DriversTestCase):
	def test_tv_conflict_window(self):
		self.assertTrue(asg.schedules_conflict("10:00", "11:00", 1.0))
		self.assertFalse(asg.schedules_conflict("10:00", "11:00", 0.5))
		self.assertTrue(asg.schedules_conflict(None, "11:00", 0.5))

	def test_tv_driver_busy_near_his_other_order(self):
		first = self.dn([(self.tv, 1)], time="10:00")
		asg.assign(first.name, self.tv1)
		second = self.dn([(self.tv, 1)], time="11:00")
		# Administrator assigns: window 0.5 h, so 10:00 and 11:00 do not clash.
		self.assertTrue(next(d for d in asg.availability(second.name)["drivers"] if d["user"] == self.tv1)["available"])
		third = self.dn([(self.tv, 1)], time="10:30")
		info = asg.availability(third.name)
		self.assertFalse(next(d for d in info["drivers"] if d["user"] == self.tv1)["available"])
		self.assertTrue(next(d for d in info["drivers"] if d["user"] == self.tv2)["available"])
		self.assertRaises(frappe.ValidationError, asg.assign, third.name, self.tv1)
		# A non-admin manager works with the 1 hour window.
		manager = user("_dm.manager@example.com", "Delivery Manager")
		self.assertEqual(asg.conflict_window_hours(manager), 1.0)

	def test_role_must_match_type(self):
		dn = self.dn([(self.ac, 1)], ac_day=DAY)
		self.assertRaises(frappe.ValidationError, asg.assign, dn.name, self.tv1)
		asg.assign(dn.name, self.ac1)
		self.assertEqual(frappe.db.get_value("Driver Schedule", dn.name, "driver"), self.ac1)

	def test_ac_daily_limit_override_and_day_off(self):
		a, b, c = (self.dn([(self.ac, 1)], ac_day=DAY) for _ in range(3))
		asg.assign(a.name, self.ac1)
		asg.assign(b.name, self.ac1)
		# Global limit 2 reached.
		self.assertRaises(frappe.ValidationError, asg.assign, c.name, self.ac1)
		load = next(l for l in asg.ac_loads(DAY)[0] if l["user"] == self.ac1)
		self.assertEqual((load["assigned_orders"], load["max_orders"], load["is_full"]), (2, 2, True))
		asg.set_day_limit(self.ac1, DAY, 3)
		asg.assign(c.name, self.ac1)
		# 0 = day off for that technician only.
		asg.set_day_limit(self.ac2, DAY, 0)
		d = self.dn([(self.ac, 1)], ac_day=DAY)
		self.assertRaises(frappe.ValidationError, asg.assign, d.name, self.ac2)
		self.assertEqual(next(x for x in asg.availability(d.name)["drivers"] if x["user"] == self.ac2)["label"], "day off")
		# Back to the global limit.
		asg.set_day_limit(self.ac1, DAY, None)
		self.assertFalse(frappe.db.exists("AC Tech Day Limit", {"driver": self.ac1, "work_date": DAY}))

	def test_unassign_and_cancel_free_the_driver(self):
		a = self.dn([(self.ac, 1)], ac_day=DAY)
		asg.assign(a.name, self.ac1)
		asg.unassign(a.name)
		self.assertFalse(frappe.db.exists("Driver Schedule", a.name))
		asg.assign(a.name, self.ac1)
		a.reload()
		a.submit()
		a.cancel()
		self.assertFalse(frappe.db.exists("Driver Schedule", a.name))

	def test_type_change_drops_a_driver_of_the_wrong_role(self):
		dn = self.dn([(self.ac, 1), (self.tv, 1)], ac_day=DAY)
		dn.dm_delivery_type = "Non AC"
		dn.save()
		asg.assign(dn.name, self.tv1)
		dn.reload()
		dn.dm_delivery_type = "AC"
		dn.save()
		self.assertFalse(dn.dm_driver)
		self.assertFalse(frappe.db.exists("Driver Schedule", dn.name))


class TestDriverAccess(DriversTestCase):
	def test_driver_sees_only_his_orders(self):
		mine = self.dn([(self.tv, 1)], time="10:00")
		other = self.dn([(self.tv, 1)], time="16:00")
		asg.assign(mine.name, self.tv1)
		asg.assign(other.name, self.tv2)
		self.send(mine)
		self.send(other)
		self.assertTrue(only_own_orders(self.tv1))
		self.assertIn(self.tv1, get_permission_query_conditions(self.tv1))
		frappe.set_user(self.tv1)
		rows = api.get_schedule("Non AC", str(DAY), str(DAY))["rows"]
		self.assertEqual([r.name for r in rows], [mine.name])
		self.assertTrue(frappe.has_permission("Delivery Note", "read", mine.name))
		self.assertFalse(frappe.has_permission("Delivery Note", "read", other.name))
		self.assertRaises(frappe.PermissionError, api.get_order_detail, other.name)
		self.assertRaises(frappe.PermissionError, api.get_assignment_orders, "Non AC")
		frappe.set_user("Administrator")
		self.assertEqual(len(api.get_schedule("Non AC", str(DAY), str(DAY))["rows"]), 2)

	def test_driver_attaches_files_and_signature_to_the_delivery_note(self):
		mine = self.dn([(self.tv, 1)], time="10:00")
		asg.assign(mine.name, self.tv1)
		frappe.set_user(self.tv1)
		png = "data:image/png;base64,iVBORw0KGgo="
		api.upload_attachment(mine.name, "photo.png", png)
		api.upload_attachment(mine.name, "signature_x.png", png)
		names = [f["file_name"] for f in api.get_order_detail(mine.name).attachments]
		self.assertEqual(sorted(names), ["photo.png", "signature_x.png"])

	def test_schedule_lists_only_notes_on_the_way_or_delivered(self):
		draft = self.dn([(self.tv, 1)], time="10:00")
		sent = self.dn([(self.tv, 1)], time="16:00")
		for dn in (draft, sent):
			asg.assign(dn.name, self.tv1)
		self.send(sent)
		rows = api.get_schedule("Non AC", str(DAY), str(DAY))["rows"]
		self.assertEqual([(r.name, r.workflow_state) for r in rows], [(sent.name, "On the way")])
		self.assertEqual(api.get_schedule("Non AC", str(DAY), str(DAY), status="delivered")["rows"], [])

	def test_driver_delivers_only_with_a_photo_or_video_and_a_signature(self):
		dn = self.dn([(self.tv, 1)])
		asg.assign(dn.name, self.tv1)
		frappe.set_user(self.tv1)
		self.assertRaisesRegex(frappe.ValidationError, "On the way", api.mark_delivered, dn.name)
		frappe.set_user("Administrator")
		self.send(dn)
		frappe.set_user(self.tv1)
		png = "data:image/png;base64,iVBORw0KGgo="
		api.upload_attachment(dn.name, "notes.txt", png)
		self.assertRaisesRegex(frappe.ValidationError, "photo or video.*signature", api.mark_delivered, dn.name)
		api.upload_attachment(dn.name, "signature_x.png", png)
		self.assertRaisesRegex(frappe.ValidationError, "photo or video", api.mark_delivered, dn.name)
		api.upload_attachment(dn.name, "door.mp4", png)
		api.mark_delivered(dn.name)
		self.assertEqual(frappe.session.user, self.tv1)
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "workflow_state"), "Delivered")
		self.assertEqual([r.name for r in api.get_schedule("Non AC", str(DAY), str(DAY), status="delivered")["rows"]],
			[dn.name])

	def test_office_can_deliver_from_the_form_without_the_page_checks(self):
		dn = self.dn([(self.tv, 1)])
		asg.assign(dn.name, self.tv1)
		self.send(dn)
		apply_workflow(frappe.get_doc("Delivery Note", dn.name), "Deliver")
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "workflow_state"), "Delivered")


class TestInstallationStats(DriversTestCase):
	def test_units_per_driver_from_submitted_notes(self):
		ac_dn = self.dn([(self.ac, 2), (self.pipe, 3)], ac_day=DAY)
		tv_dn = self.dn([(self.tv, 4), (self.charge, 1)])
		draft = self.dn([(self.tv, 9)], time="18:00")
		asg.assign(ac_dn.name, self.ac1)
		asg.assign(tv_dn.name, self.tv1)
		asg.assign(draft.name, self.tv1)
		on_the_way = self.dn([(self.tv, 7)], time="16:00")
		asg.assign(on_the_way.name, self.tv1)
		for dn in (ac_dn, tv_dn, on_the_way):
			self.send(dn)
		for dn in (ac_dn, tv_dn):
			apply_workflow(frappe.get_doc("Delivery Note", dn.name), "Deliver")
		rows = {r["technician"]: r for r in stats.installation_stats(DAY, DAY)}
		self.assertEqual((rows[self.ac1]["ac_units"], rows[self.ac1]["non_ac_units"]), (2, 0))
		# The draft, the note still on the way and the non-stock delivery charge do not count.
		self.assertEqual((rows[self.tv1]["ac_units"], rows[self.tv1]["non_ac_units"]), (0, 4))
		self.assertEqual(list({r["technician"] for r in stats.installation_stats(DAY, DAY, unit="ac")}), [self.ac1])
		# A driver sees only himself.
		frappe.set_user(self.tv1)
		self.assertEqual([r["technician"] for r in api.get_installation_stats(str(DAY), str(DAY))["rows"]], [self.tv1])
		# A Delivery Manager is not a System Manager: only his own numbers.
		frappe.set_user("Administrator")
		manager = user("_dm.dmgr@example.com", "Delivery Manager")
		frappe.set_user(manager)
		self.assertEqual(api.get_installation_stats(str(DAY), str(DAY))["rows"], [])
		frappe.set_user("Administrator")
		self.assertEqual(len(api.get_installation_stats(str(DAY), str(DAY))["rows"]), 2)


class TestPosOrderRules(DriversTestCase):
	def test_ac_and_non_ac_may_share_a_pos_order(self):
		check_pos_order_rules([{"item_code": self.ac, "qty": 1}, {"item_code": self.tv, "qty": 1}])
		check_pos_order_rules([{"item_code": self.ac, "qty": 1}, {"item_code": self.pipe, "qty": 1}])
		check_pos_order_rules([{"item_code": self.tv, "qty": 1}, {"item_code": self.charge, "qty": 1}])

	def test_delivery_charge_only_once(self):
		self.assertRaises(frappe.ValidationError, check_pos_order_rules,
			[{"item_code": self.tv, "qty": 1}, {"item_code": self.charge, "qty": 1}, {"item_code": self.charge, "qty": 1}])
		self.assertRaises(frappe.ValidationError, check_pos_order_rules,
			[{"item_code": self.tv, "qty": 1}, {"item_code": self.charge, "qty": 2}])

	def test_item_rules(self):
		frappe.db.set_value("Item", self.tv, "dm_has_delivery_charge", 1)
		rules = item_rules([self.ac, self.tv, self.pipe, self.charge])
		self.assertEqual({k: v["kind"] for k, v in rules.items()},
			{self.ac: "ac", self.tv: "other", self.pipe: "neutral", self.charge: "neutral"})
		self.assertTrue(rules[self.tv]["delivery_charge"])
		self.assertFalse(rules[self.ac]["delivery_charge"])
