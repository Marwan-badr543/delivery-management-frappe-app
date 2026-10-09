"""Self-contained POS/stock fixtures, built on the site's default company.

Everything is created inside the test transaction and rolled back afterwards.
"""

import frappe
from frappe.utils import add_days, nowdate


def company():
	name = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name", limit=1)[0]
	return frappe.get_cached_doc("Company", name)


def warehouse(label):
	comp = company()
	name = f"{label} - {comp.abbr}"
	if not frappe.db.exists("Warehouse", name):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": label, "company": comp.name,
			"parent_warehouse": f"All Warehouses - {comp.abbr}"}).insert()
	return name


def item_group(name, parent="All Item Groups", is_group=0):
	if not frappe.db.exists("Item Group", name):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": name, "parent_item_group": parent,
			"is_group": is_group}).insert()
	return name


def item(code, group, serial=0, stock=1):
	if not frappe.db.exists("Item", code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": group,
			"stock_uom": "Nos", "is_stock_item": stock, "has_serial_no": serial, "include_item_in_manufacturing": 0,
			"is_sales_item": 1}).insert()
	return code


def receive(item_code, wh, qty, serials=None, posting_date=None, rate=10):
	"""Material Receipt. ``serials`` are explicit names, so tests can assert them."""
	se = frappe.get_doc({
		"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": company().name,
		"posting_date": posting_date or nowdate(), "set_posting_time": 1,
		"items": [{"item_code": item_code, "qty": qty, "t_warehouse": wh, "basic_rate": rate,
			"serial_no": "\n".join(serials) if serials else None, "conversion_factor": 1, "uom": "Nos"}],
	})
	se.insert()
	se.submit()
	return se


def customer(name="_DM Customer"):
	if not frappe.db.exists("Customer", name):
		frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_type": "Individual",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"}).insert()
	return name


def pos_profile(branch_wh, name="_DM POS"):
	comp = company()
	if not frappe.db.exists("POS Profile", name):
		frappe.get_doc({
			"doctype": "POS Profile", "name": name, "company": comp.name, "warehouse": branch_wh,
			"currency": comp.default_currency, "selling_price_list": _price_list(), "update_stock": 1,
			"write_off_account": comp.write_off_account, "write_off_cost_center": comp.cost_center,
			"cost_center": comp.cost_center,
			"payments": [{"mode_of_payment": "Cash", "default": 1}],
			"applicable_for_users": [{"user": frappe.session.user, "default": 1}],
		}).insert()
	return name


def unlock_terminal(profile, user=None):
	"""POS Awesome V15 only accepts sales from a terminal unlocked by a cashier PIN.
	Mark ``user`` as the verified cashier of this session (what the PIN dialog does)."""
	from posawesome.posawesome.api.terminal_state import activate_verified_cashier

	user = user or frappe.session.user
	doc = frappe.get_doc("POS Profile", profile)
	if not any(row.user == user for row in doc.applicable_for_users):
		doc.append("applicable_for_users", {"user": user})
		doc.save()
	activate_verified_cashier(doc, user)


def opening_shift(profile):
	"""POS Awesome V15 only accepts invoices made inside an open shift of the current user."""
	user = frappe.session.user
	name = frappe.db.get_value("POS Opening Shift",
		{"pos_profile": profile, "user": user, "status": "Open", "docstatus": 1}, "name")
	if name:
		return name
	doc = frappe.get_doc({
		"doctype": "POS Opening Shift", "period_start_date": frappe.utils.now_datetime(), "posting_date": nowdate(),
		"company": company().name, "pos_profile": profile, "user": user,
		"balance_details": [{"mode_of_payment": "Cash", "amount": 0}],
	})
	doc.insert()
	doc.submit()
	return doc.name


def _price_list():
	"""A selling price list in the company currency, so no exchange rate is ever needed."""
	currency = company().default_currency
	name = frappe.db.get_value("Price List", {"selling": 1, "enabled": 1, "currency": currency}, "name")
	if not name:
		name = frappe.get_doc({"doctype": "Price List", "price_list_name": f"_DM Selling {currency}",
			"currency": currency, "selling": 1, "enabled": 1}).insert().name
	return name


class PosWorld:
	"""A complete little shop: main store, branch, AC + TV + accessory + service items."""

	def __init__(self):
		self.company = company()
		self.main = warehouse("_DM Main")
		self.branch = warehouse("_DM Branch")
		item_group("_DM AC", is_group=1)
		item_group("_DM AC Units", parent="_DM AC")
		item_group("_DM AC Accessory", parent="_DM AC")
		item_group("_DM Goods")
		self.ac = item("_DM AC 1.5T", "_DM AC Units", serial=1)
		self.gas = item("_DM Gas Refill", "_DM AC Units", serial=0)
		self.tv = item("_DM TV", "_DM Goods", serial=1)
		self.pipe = item("_DM Copper Pipe", "_DM AC Accessory", serial=0)
		self.service = item("_DM Install Fee", "_DM Goods", serial=0, stock=0)

		old = add_days(nowdate(), -30)
		receive(self.ac, self.main, 2, ["_DMAC-M-OLD1", "_DMAC-M-OLD2"], posting_date=old)
		receive(self.ac, self.main, 2, ["_DMAC-M-NEW1", "_DMAC-M-NEW2"])
		receive(self.ac, self.branch, 1, ["_DMAC-B-1"])
		receive(self.tv, self.main, 3, ["_DMTV-M-1", "_DMTV-M-2", "_DMTV-M-3"])
		receive(self.tv, self.branch, 2, ["_DMTV-B-1", "_DMTV-B-2"])
		receive(self.pipe, self.main, 50)
		receive(self.pipe, self.branch, 5)
		receive(self.gas, self.main, 10)

		self.customer = customer()
		self.pos_profile = pos_profile(self.branch)
		self.opening_shift = opening_shift(self.pos_profile)
		unlock_terminal(self.pos_profile)

		settings = frappe.get_single("Delivery Settings")
		settings.update({"main_warehouse": self.main,
			"global_ac_daily_capacity": 20, "slot_lead_time_hours": 2, "booking_window_days": 30})
		settings.set("ac_item_groups", [{"item_group": "_DM AC"}])
		settings.set("ac_excluded_item_groups", [{"item_group": "_DM AC Accessory"}])
		settings.set("ac_excluded_items", [{"item_code": self.gas}])
		settings.save()

	# --------------------------------------------------------------- POS payload

	def draft_invoice(self, lines, discount_percentage=0):
		"""Create the draft exactly like POS Awesome does (``update_invoice``)."""
		from posawesome.posawesome.api.invoice_processing.creation import update_invoice

		doc = {
			"doctype": "Sales Invoice", "is_pos": 1, "pos_profile": self.pos_profile, "company": self.company.name,
			"customer": self.customer, "currency": self.company.default_currency,
			"selling_price_list": _price_list(), "update_stock": 1, "posting_date": nowdate(),
			"additional_discount_percentage": discount_percentage,
			"posa_pos_opening_shift": self.opening_shift,
			"items": [
				{"item_code": code, "qty": qty, "rate": rate, "price_list_rate": rate, "uom": "Nos",
					"conversion_factor": 1, "warehouse": self.branch, "posa_row_id": f"row{i}"}
				for i, (code, qty, rate) in enumerate(lines)
			],
			"payments": [{"mode_of_payment": "Cash", "amount": 0, "default": 1}],
		}
		saved = update_invoice(frappe.as_json(doc))
		# V15 returns the saved invoice as a dict (v14 returned the document).
		invoice = frappe.parse_json(frappe.as_json(saved if isinstance(saved, dict) else saved.as_dict()))
		for p in invoice["payments"]:
			p["amount"] = invoice["rounded_total"] or invoice["grand_total"]
		return invoice
