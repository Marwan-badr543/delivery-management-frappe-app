"""Endpoints of the Order Control page: each order's current state and its Order Log."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_fullname, getdate, nowdate
from frappe.utils.caching import redis_cache

from delivery_management.api.drivers import format_address
from delivery_management.control.log import LOG

ROLES = ("System Manager", "Delivery Manager")


def _require_office():
	if frappe.session.user != "Administrator" and not set(ROLES) & set(frappe.get_roles()):
		frappe.throw(_("Only the delivery office can see the Order Control."), frappe.PermissionError)


@frappe.whitelist()
def get_orders(search=None, event=None, user=None, from_date=None, to_date=None, kind=None):
	"""Orders with log rows in the filters, latest activity first. ``kind``: ``order`` (a
	delivery order, its Sales Order), ``sale`` (a store sale, its Sales Invoice) or ``ticket``
	(a support ticket, its Issue)."""
	_require_office()
	conditions, values = [], {}
	if kind == "order":
		conditions.append("l.sales_order is not null")
	elif kind == "sale":
		conditions.append("l.sales_order is null and l.sales_invoice is not null")
	elif kind == "ticket":
		conditions.append("l.support_ticket is not null")
	if event:
		conditions.append("l.event = %(event)s")
		values["event"] = event
	if user:
		conditions.append("(l.user = %(user)s or l.driver = %(user)s)")
		values["user"] = user
	# Always inside a period (``at`` is indexed): the log grows forever. Without a From date,
	# the 90 days before the To date (or today).
	conditions.append("l.at >= %(from)s")
	values["from"] = getdate(from_date) if from_date else add_days(getdate(to_date or nowdate()), -90)
	if to_date:
		conditions.append("l.at < date_add(%(to)s, interval 1 day)")
		values["to"] = getdate(to_date)
	if search:
		conditions.append("""(l.sales_order like %(q)s or l.sales_invoice like %(q)s or l.delivery_note like %(q)s
			or l.support_ticket like %(q)s
			or l.customer_name like %(q)s or l.customer like %(q)s
			or exists (select 1 from `tabCustomer` c where c.name = l.customer and c.mobile_no like %(q)s))""")
		values["q"] = f"{search.strip()}%"  # from the start: the indexes can be used
	where = " and ".join(conditions) or "1=1"
	orders = frappe.db.sql(
		f"""select {KEY} as name, max(l.at) as last_at, count(*) as steps,
			max(l.customer_name) as customer_name, max(l.sales_order is not null) as is_order,
			max(l.support_ticket is not null) as is_ticket
		from `tab{LOG}` l where {where} and {KEY} is not null
		group by {KEY} order by last_at desc limit 100""",
		values,
		as_dict=True,
	)
	# The latest event of each order: two queries for the page, not one per order.
	last = {}
	for column, kind_of in (("support_ticket", "ticket"), ("sales_order", "order"), ("sales_invoice", "sale")):
		names = tuple(o.name for o in orders if _kind(o) == kind_of)
		if names:
			last.update(frappe.db.sql(
				f"""select k, event from (select {column} as k, event, row_number() over (partition by {column}
					order by at desc, creation desc) as n from `tab{LOG}` where {column} in %(names)s) t where n = 1""",
				{"names": names}))
	for o in orders:
		o.last_event = last.get(o.name)
		o.kind = _kind(o)
	return orders


# A support ticket's rows may name its invoice too: the ticket is the key.
KEY = "coalesce(l.support_ticket, l.sales_order, l.sales_invoice)"


def _kind(o) -> str:
	return "ticket" if o.is_ticket else "order" if o.is_order else "sale"


@frappe.whitelist()
def get_users():
	_require_office()
	return _users()


@redis_cache(ttl=600)
def _users() -> list[dict]:
	"""Everyone in the log: read once every 10 minutes, not on each page open."""
	users = frappe.db.sql_list(f"select distinct user from `tab{LOG}` union select distinct driver from `tab{LOG}` "
		"where driver is not null")
	return sorted(({"value": u, "label": get_fullname(u)} for u in users if u), key=lambda u: u["label"].lower())


@frappe.whitelist()
def get_order(name):
	"""The order now (customer, money, where its goods are) and every logged step, oldest first."""
	_require_office()
	is_ticket = frappe.db.exists("Issue", name)
	is_order = not is_ticket and frappe.db.exists("Sales Order", name)
	if not (is_ticket or is_order) and not frappe.db.exists("Sales Invoice", name):
		frappe.throw(_("{0} is not an order or a sale.").format(name))
	key = "support_ticket" if is_ticket else "sales_order" if is_order else "sales_invoice"
	steps = frappe.get_all(LOG, filters={key: name}, fields=["event", "at", "user", "driver", "delivery_note",
		"support_ticket",
		"sales_invoice", "amount", "mode_of_payment", "items", "reference_doctype", "reference_name", "note"],
		order_by="at asc, creation asc")
	for s in steps:
		s.user_name = get_fullname(s.user)
		s.driver_name = get_fullname(s.driver) if s.driver else None
	return {**(_ticket(name) if is_ticket else _order(name) if is_order else _sale(name)), "steps": steps}


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


def _ticket(name) -> dict:
	"""A support ticket now: its step, where the customer's item is, the trip."""
	from delivery_management.api.support import full_address
	from delivery_management.customer_support import tickets as t

	issue = frappe.get_doc("Issue", name)
	return {
		"name": issue.name, "kind": "ticket", "status": issue.get("sm_step") or issue.status, "created": issue.creation,
		"created_by": get_fullname(issue.get("sm_cs_user") or issue.owner), "customer": issue.customer,
		"customer_name": issue.customer_name or issue.get("sm_customer_phone"), "mobile": issue.get("sm_customer_phone"),
		"address": full_address(issue), "branch": None, "total": 0, "notes": [], "subject": issue.subject,
		"item_code": issue.get("sm_item_code"), "serial_no": issue.get("sm_serial_no"), "where": issue.get("sm_warehouse"),
		"under_warranty": t.under_warranty(issue), "warranty_expiry": issue.get("sm_warranty_expiry"),
		"trip": {"date": issue.get("sm_trip_date"), "time": issue.get("sm_trip_time") and str(issue.sm_trip_time)[:5],
			"driver_name": get_fullname(issue.sm_driver) if issue.get("sm_driver") else None},
		"invoices": [issue.sm_sales_invoice] if issue.get("sm_sales_invoice") else [], "credit_notes": [],
		"billed": 0, "credited": 0, "due": 0, "paid": 0, "currency": None,
	}
