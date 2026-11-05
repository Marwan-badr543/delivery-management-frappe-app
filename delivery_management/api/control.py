"""Endpoints of the Order Control page: each order's current state and its Order Log."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, get_fullname, getdate

from delivery_management.api.drivers import format_address
from delivery_management.control.log import LOG

ROLES = ("System Manager", "Delivery Manager")


def _require_office():
	if frappe.session.user != "Administrator" and not set(ROLES) & set(frappe.get_roles()):
		frappe.throw(_("Only the delivery office can see the Order Control."), frappe.PermissionError)


@frappe.whitelist()
def get_orders(search=None, event=None, user=None, from_date=None, to_date=None):
	"""Orders with log rows in the filters, latest activity first."""
	_require_office()
	conditions, values = [], {}
	if event:
		conditions.append("l.event = %(event)s")
		values["event"] = event
	if user:
		conditions.append("(l.user = %(user)s or l.driver = %(user)s)")
		values["user"] = user
	if from_date:
		conditions.append("l.at >= %(from)s")
		values["from"] = getdate(from_date)
	if to_date:
		conditions.append("l.at < date_add(%(to)s, interval 1 day)")
		values["to"] = getdate(to_date)
	if search:
		conditions.append("""(l.sales_order like %(q)s or l.sales_invoice like %(q)s or l.delivery_note like %(q)s
			or l.customer_name like %(q)s or l.customer like %(q)s
			or exists (select 1 from `tabCustomer` c where c.name = l.customer and c.mobile_no like %(q)s))""")
		values["q"] = f"%{search.strip()}%"
	where = " and ".join(conditions) or "1=1"
	orders = frappe.db.sql(
		f"""select coalesce(l.sales_order, l.sales_invoice) as name, max(l.at) as last_at, count(*) as steps,
			max(l.customer_name) as customer_name, max(l.sales_order is not null) as is_order
		from `tab{LOG}` l where {where} and coalesce(l.sales_order, l.sales_invoice) is not null
		group by coalesce(l.sales_order, l.sales_invoice) order by last_at desc limit 100""",
		values,
		as_dict=True,
	)
	for o in orders:
		last = frappe.db.sql(
			f"""select event from `tab{LOG}` where {'sales_order' if o.is_order else 'sales_invoice'} = %s
			order by at desc, creation desc limit 1""", o.name)
		o.last_event = last[0][0] if last else None
	return orders


@frappe.whitelist()
def get_users():
	_require_office()
	users = frappe.db.sql_list(f"select distinct user from `tab{LOG}` union select distinct driver from `tab{LOG}` "
		"where driver is not null")
	return sorted(({"value": u, "label": get_fullname(u)} for u in users if u), key=lambda u: u["label"].lower())


@frappe.whitelist()
def get_order(name):
	"""The order now (customer, money, where its goods are) and every logged step, oldest first."""
	_require_office()
	is_order = frappe.db.exists("Sales Order", name)
	if not is_order and not frappe.db.exists("Sales Invoice", name):
		frappe.throw(_("{0} is not an order or a sale.").format(name))
	key = "sales_order" if is_order else "sales_invoice"
	steps = frappe.get_all(LOG, filters={key: name}, fields=["event", "at", "user", "driver", "delivery_note",
		"sales_invoice", "amount", "mode_of_payment", "items", "reference_doctype", "reference_name", "note"],
		order_by="at asc, creation asc")
	for s in steps:
		s.user_name = get_fullname(s.user)
		s.driver_name = get_fullname(s.driver) if s.driver else None
	return {**(_order(name) if is_order else _sale(name)), "steps": steps}


def _money(invoices: list[str]) -> dict:
	rows = frappe.get_all("Sales Invoice", filters={"name": ["in", invoices or [""]], "docstatus": 1},
		fields=["name", "rounded_total", "grand_total", "outstanding_amount", "currency", "status"])
	credits = frappe.get_all("Sales Invoice", filters={"return_against": ["in", invoices or [""]], "docstatus": 1,
		"is_return": 1}, fields=["name", "rounded_total", "grand_total"])
	billed = sum(flt(r.rounded_total or r.grand_total) for r in rows)
	credited = -sum(flt(c.rounded_total or c.grand_total) for c in credits)
	due = sum(max(flt(r.outstanding_amount), 0) for r in rows)
	return {"invoices": [r.name for r in rows], "credit_notes": [c.name for c in credits], "billed": billed,
		"credited": credited, "due": due, "paid": billed - credited - due,
		"currency": rows[0].currency if rows else None}


def _order(name) -> dict:
	so = frappe.get_doc("Sales Order", name)
	invoices = frappe.db.sql_list("""select distinct parent from `tabSales Invoice Item`
		where sales_order = %s and docstatus = 1""", name)
	invoices = frappe.get_all("Sales Invoice", filters={"name": ["in", invoices or [""]], "is_return": 0}, pluck="name")
	notes = frappe.db.sql(
		"""select distinct dn.name, dn.docstatus, dn.dm_delivery_status as status, dn.dm_delivery_type as order_type,
			dn.dm_driver as driver, coalesce(dn.dm_ac_installation_date, dn.dm_delivery_date) as work_date,
			dn.dm_delivery_time as slot
		from `tabDelivery Note` dn join `tabDelivery Note Item` i on i.parent = dn.name
		where i.against_sales_order = %s and dn.is_return = 0 and dn.docstatus < 2 order by dn.creation""",
		name, as_dict=True)
	for n in notes:
		n.driver_name = get_fullname(n.driver) if n.driver else None
		n.slot = n.slot and str(n.slot)[:5]
		n.where = (_("With the customer") if n.docstatus == 1 else _("With the driver") if n.status == "On the way"
			else _("Coming back to the store") if n.status == "Returned" else _("In the store"))
	return {
		"name": so.name, "kind": "order", "status": so.status, "created": so.creation,
		"created_by": get_fullname(so.owner), "customer": so.customer, "customer_name": so.customer_name,
		"mobile": so.get("contact_mobile") or frappe.db.get_value("Customer", so.customer, "mobile_no"),
		"address": format_address(frappe._dict(shipping_address=so.get("shipping_address"),
			address_display=so.get("address_display"), area=so.get("dm_delivery_area"),
			block_no=so.get("dm_block_no"), road_no=so.get("dm_road_no"), building_no=so.get("dm_building_no"))),
		"branch": so.get("set_warehouse"), "total": flt(so.rounded_total or so.grand_total), "notes": notes,
		**_money(invoices),
	}


def _sale(name) -> dict:
	si = frappe.get_doc("Sales Invoice", name)
	return {
		"name": si.name, "kind": "sale", "status": si.status, "created": si.creation,
		"created_by": get_fullname(si.owner), "customer": si.customer, "customer_name": si.customer_name,
		"mobile": si.get("contact_mobile") or frappe.db.get_value("Customer", si.customer, "mobile_no"),
		"address": None, "branch": si.get("pos_profile") or si.get("set_warehouse"),
		"total": flt(si.rounded_total or si.grand_total), "notes": [], **_money([si.name]),
	}
