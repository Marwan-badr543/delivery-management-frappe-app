"""Recreate the client's (showsatellite) structure on a LOCAL development site.

NEVER run this on production. It is not wired to any install hook. Run it by hand:

    bench --site erp16.localhost execute delivery_management.dev.setup_showsatellite.run

It is idempotent: re-running only adds what is missing. The data comes from
``showsatellite_snapshot.json``, a read-only snapshot of the production ERP
(company, warehouse names, branches, POS profiles, item group tree, the AC items
plus a few real non-AC items with prices, and the Sales Order -> Delivery Note
server script).

What it sets up:
* Company ``showsatellite`` (abbr ``SHD``, BHD, Bahrain), so warehouse names
  match production exactly (``Stores - SHD``, ``JIDHAFS - SHD``, ...).
* The 4 branch POS profiles with their branch warehouses, Sales Orders allowed.
* Opening stock with serial numbers in the main store and in every branch.
* The production server script that builds the Delivery Note from the Sales Order.
* Sample delivery areas and work hours, and Delivery Settings (main store, AC group,
  accessory exclusions).
* The site timezone (Asia/Bahrain) and customer naming, as in production.
"""

import hashlib
import json
import os

import frappe
from frappe.installer import update_site_config
from frappe.utils import add_days, flt, nowdate

SNAPSHOT = os.path.join(os.path.dirname(__file__), "showsatellite_snapshot.json")
PRICE_LIST = "Standard Selling BHD"

# Sample only: the old system's real areas live in its own database.
SAMPLE_AREAS = {
	"Jidhafs": 2, "Bani Jamra": 2, "Muharraq": 2, "Nuwaidrat": 2,
	"Manama": 3, "Riffa": 2, "Isa Town": 1, "Hamad Town": 1,
}
SAMPLE_HOURS = ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00"]
FRIDAY_HOURS = {"16:00", "18:00", "20:00"}


def run():
	if frappe.conf.get("developer_mode") is None and not frappe.conf.get("allow_tests"):
		frappe.throw("Refusing to run: this looks like a production site (allow_tests is off).")
	snap = json.load(open(SNAPSHOT))
	frappe.flags.in_import = False
	log = []

	_system(snap, log)
	company = _company(snap, log)
	_fiscal_year(log)
	_warehouses(snap, company, log)
	_branches(snap, log)
	_item_groups(snap, log)
	_price_list(log)
	_items(snap, log)
	_mode_of_payment(company, log)
	_pos_profiles(snap, company, log)
	_customers(log)
	_cashier_pin(log)
	_server_scripts(snap, log)
	_delivery(log)
	frappe.db.commit()
	_opening_stock(snap, company, log)
	frappe.db.commit()
	frappe.clear_cache()  # timezone and settings are cached
	for line in log:
		print("  -", line)
	print("showsatellite development environment ready.")


# --------------------------------------------------------------------------- steps


def _system(snap, log):
	others = frappe.get_all("Company", filters={"name": ["!=", snap["company"]["company_name"]]}, pluck="name")
	if others:
		# A site shared with another project (e.g. erp16.localhost): keep its timezone and naming.
		log.append(f"site shared with {', '.join(others)}: timezone and customer naming left as they are")
	else:
		frappe.db.set_single_value("System Settings", "time_zone", snap["system"]["time_zone"])
		frappe.db.set_single_value("Selling Settings", "cust_master_name", snap["system"]["cust_master_name"])
	update_site_config("server_script_enabled", 1)
	# v16 reads this flag from common_site_config.json only (bench set-config -g server_script_enabled 1).
	update_site_config("server_script_enabled", 1, site_config_path=os.path.join(frappe.utils.get_bench_path(), "sites", "common_site_config.json"))
	# v16: serial numbers must be switched on, and kept in the plain Serial No text fields
	# (the app picks serials into those fields; ERPNext builds the bundles from them).
	frappe.db.set_single_value("Stock Settings", {"enable_serial_and_batch_no_for_item": 1, "use_serial_batch_fields": 1})
	frappe.db.set_value("Currency", "BHD", "enabled", 1)
	# The production SO -> DN server script leaves the price list to the global default.
	# On this dev site that default is not in BHD, so give it an exchange rate.
	default_pl = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	pl_currency = default_pl and frappe.db.get_value("Price List", default_pl, "currency")
	if pl_currency and pl_currency != "BHD" and not frappe.db.exists(
		"Currency Exchange", {"from_currency": pl_currency, "to_currency": "BHD"}
	):
		frappe.get_doc({"doctype": "Currency Exchange", "date": "2020-01-01", "from_currency": pl_currency,
			"to_currency": "BHD", "exchange_rate": 0.0078, "for_selling": 1, "for_buying": 1}).insert()
	log.append("timezone Asia/Bahrain, customer naming by name, server scripts enabled")


def _company(snap, log):
	c = snap["company"]
	if not frappe.db.exists("Company", c["company_name"]):
		frappe.get_doc({
			"doctype": "Company", "company_name": c["company_name"], "abbr": c["abbr"],
			"default_currency": c["default_currency"], "country": c["country"],
			"create_chart_of_accounts_based_on": "Standard Template", "chart_of_accounts": "Standard",
		}).insert()
		log.append(f"company {c['company_name']} created")
	return frappe.get_doc("Company", c["company_name"])


def _fiscal_year(log):
	today = nowdate()
	if not frappe.db.sql("select name from `tabFiscal Year` where %s between year_start_date and year_end_date", today):
		year = today[:4]
		frappe.get_doc({"doctype": "Fiscal Year", "year": year, "year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31"}).insert()
		log.append(f"fiscal year {year} created")


def _warehouses(snap, company, log):
	suffix = f" - {company.abbr}"
	for w in snap["warehouses"]:
		if frappe.db.exists("Warehouse", w["name"]):
			continue
		# The document name comes from warehouse_name + abbr; production renamed some
		# labels later (e.g. "JIDHAFS - SHD" shows as "Jidhafs"), so create by name, then relabel.
		doc = frappe.get_doc({
			"doctype": "Warehouse", "warehouse_name": w["name"][: -len(suffix)], "company": company.name,
			"is_group": w["is_group"], "parent_warehouse": w["parent_warehouse"] or None,
		}).insert()
		if doc.warehouse_name != w["warehouse_name"]:
			frappe.db.set_value("Warehouse", doc.name, "warehouse_name", w["warehouse_name"])
		log.append(f"warehouse {doc.name}")


def _branches(snap, log):
	for b in snap["branches"]:
		if not frappe.db.exists("Branch", b["name"]):
			frappe.get_doc({"doctype": "Branch", "branch": b["name"]}).insert()


def _item_groups(snap, log):
	for g in snap["item_groups"]:
		if frappe.db.exists("Item Group", g["name"]):
			continue
		frappe.get_doc({"doctype": "Item Group", "item_group_name": g["name"],
			"parent_item_group": g["parent_item_group"] or "All Item Groups", "is_group": g["is_group"]}).insert()
	log.append(f"{len(snap['item_groups'])} item groups (production tree)")


def _price_list(log):
	if not frappe.db.exists("Price List", PRICE_LIST):
		frappe.get_doc({"doctype": "Price List", "price_list_name": PRICE_LIST, "currency": "BHD",
			"selling": 1, "enabled": 1}).insert()
		log.append(f"price list {PRICE_LIST}")


def _items(snap, log):
	for i in snap["items"]:
		if not frappe.db.exists("Item", i["item_code"]):
			frappe.get_doc({
				"doctype": "Item", "item_code": i["item_code"], "item_name": i["item_name"][:140],
				"item_group": i["item_group"], "stock_uom": i["stock_uom"] or "Nos",
				"is_stock_item": i["is_stock_item"], "has_serial_no": i["has_serial_no"], "is_sales_item": 1,
				"include_item_in_manufacturing": 0,
			}).insert()
		if not frappe.db.exists("Item Price", {"item_code": i["item_code"], "price_list": PRICE_LIST}):
			frappe.get_doc({"doctype": "Item Price", "item_code": i["item_code"], "price_list": PRICE_LIST,
				"price_list_rate": flt(i["rate"]) or 10}).insert()
	log.append(f"{len(snap['items'])} items with prices")


def _mode_of_payment(company, log):
	mop = frappe.get_doc("Mode of Payment", "Cash")
	if not any(a.company == company.name for a in mop.accounts):
		account = frappe.db.get_value("Account", {"company": company.name, "account_type": "Cash", "is_group": 0})
		mop.append("accounts", {"company": company.name, "default_account": account})
		mop.save()
		log.append(f"Cash payment account {account}")


def _pos_profiles(snap, company, log):
	for p in snap["pos_profiles"]:
		if frappe.db.exists("POS Profile", p["name"]):
			_apply_production_flags(p)
			continue
		frappe.get_doc({
			"doctype": "POS Profile", "name": p["name"], "company": company.name, "warehouse": p["warehouse"],
			"currency": "BHD", "selling_price_list": PRICE_LIST, "update_stock": 1,
			"write_off_account": company.write_off_account, "write_off_cost_center": company.cost_center,
			"cost_center": company.cost_center,
			"payments": [{"mode_of_payment": "Cash", "default": 1}],
			"applicable_for_users": [{"user": "Administrator", "default": 1 if p["name"] == "JIDHAFS" else 0}],
			"posa_allow_sales_order": 1, "posa_input_qty": 1, "posa_display_items_in_stock": 0,
		}).insert()
		_apply_production_flags(p)
		log.append(f"POS profile {p['name']} -> {p['warehouse']}")


def _apply_production_flags(p):
	"""Same POS Awesome switches as production (rate editing off, local storage on, ...)."""
	meta = frappe.get_meta("POS Profile")
	flags = {k: v for k, v in (p.get("flags") or {}).items() if meta.has_field(k)}
	# Dev only: production has these off; the developer wants to edit rates/discounts locally.
	flags.update(posa_allow_user_to_edit_rate=1, posa_allow_user_to_edit_item_discount=1,
		posa_allow_user_to_edit_additional_discount=1)
	if flags:
		frappe.db.set_value("POS Profile", p["name"], flags)


# Local dev only: POS Awesome V15 unlocks the terminal with a cashier PIN (User.posa_pos_pin).
DEV_CASHIER_PIN = "1234"


def _cashier_pin(log):
	meta = frappe.get_meta("User")
	if not meta.has_field("posa_pos_pin"):
		return
	user = frappe.get_doc("User", "Administrator")
	if not user.get_password("posa_pos_pin", raise_exception=False):
		user.posa_pos_pin = DEV_CASHIER_PIN
		user.save(ignore_permissions=True)
		log.append("cashier PIN for Administrator (see DEV_CASHIER_PIN)")


def _customers(log):
	for name, mobile in (("Ahmed Ali", "33112233"), ("Fatima Hasan", "36998877")):
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_type": "Individual",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"), "mobile_no": mobile}).insert()
	log.append("sample customers Ahmed Ali, Fatima Hasan")


def _server_scripts(snap, log):
	for s in snap["server_scripts"]:
		if not frappe.db.exists("Server Script", s["name"]):
			frappe.get_doc({"doctype": "Server Script", "name": s["name"], "script_type": s["script_type"],
				"reference_doctype": s["reference_doctype"], "doctype_event": s["doctype_event"],
				"script": s["script"]}).insert()
			log.append(f"server script '{s['name']}'")


def _delivery(log):
	for area, cars in SAMPLE_AREAS.items():
		if not frappe.db.exists("Delivery Area", area):
			frappe.get_doc({"doctype": "Delivery Area", "area_name": area, "cars_number": cars}).insert()
		for hour in SAMPLE_HOURS:
			if not frappe.db.exists("Delivery Work Hour", {"area": area, "hour": f"{hour}:00"}):
				frappe.get_doc({"doctype": "Delivery Work Hour", "area": area, "hour": hour,
					"friday": 1 if hour in FRIDAY_HOURS else 0}).insert()
	settings = frappe.get_single("Delivery Settings")
	if not settings.ac_item_groups:
		settings.append("ac_item_groups", {"item_group": "Air Conditioner"})
	settings.update({"main_warehouse": "Stores - SHD",
		"global_ac_daily_capacity": settings.global_ac_daily_capacity or 20,
		"slot_lead_time_hours": settings.slot_lead_time_hours or 2, "booking_window_days": 30})
	if not settings.ac_excluded_item_groups:
		settings.append("ac_excluded_item_groups", {"item_group": "Air Conditioner Accessory"})
	if not settings.ac_excluded_items and frappe.db.exists("Item", "GAS R32 FILLING"):
		settings.append("ac_excluded_items", {"item_code": "GAS R32 FILLING"})
	settings.save()
	if not frappe.db.exists("Has Role", {"parent": "Administrator", "role": "Delivery Manager"}):
		frappe.get_doc("User", "Administrator").add_roles("Delivery Manager")
	log.append(f"{len(SAMPLE_AREAS)} sample delivery areas x {len(SAMPLE_HOURS)} slots, Delivery Settings")


def _opening_stock(snap, company, log):
	"""Opening stock: plenty in the main store, a few units per branch."""
	if frappe.db.exists("Stock Entry", {"remarks": "showsatellite dev opening stock", "docstatus": 1}):
		return
	main = "Stores - SHD"
	branches = [p["warehouse"] for p in snap["pos_profiles"]]
	plan = []
	for i in snap["items"]:
		if not i["is_stock_item"]:
			continue
		plan.append((i, main, 12))
		# Branches get little stock, and AC units only in two of them, so the POS
		# "out of stock" behaviour differs between Invoice and Order.
		is_ac = "Air Conditioner" in i["item_group"]
		for idx, wh in enumerate(branches):
			if is_ac and idx > 1:
				continue
			plan.append((i, wh, 2))

	rows = []
	for item, wh, qty in plan:
		row = {"item_code": item["item_code"], "qty": qty, "t_warehouse": wh, "uom": item["stock_uom"] or "Nos",
			"conversion_factor": 1, "basic_rate": round((flt(item["rate"]) or 10) * 0.6, 3)}
		if item["has_serial_no"]:
			code = hashlib.md5(item["item_code"].encode()).hexdigest()[:5].upper()
			wh_code = wh.split(" - ")[0][:4].upper()
			row["serial_no"] = "\n".join(f"SN-{code}-{wh_code}-{n:03d}" for n in range(1, qty + 1))
		rows.append(row)

	se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": company.name,
		"posting_date": add_days(nowdate(), -1), "set_posting_time": 1,
		"remarks": "showsatellite dev opening stock", "items": rows})
	se.insert()
	se.submit()
	log.append(f"opening stock {se.name}: {len(rows)} lines (main store x12, branches x2)")
