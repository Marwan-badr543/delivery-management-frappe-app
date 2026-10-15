"""Endpoints of the Support pages (port of the old /admin/issue-tickets, /admin/technicians
and /admin/maint-tech routers). The rules live in ``customer_support.tickets``.

Pages and who opens them (System Manager = the old admin, sees and does everything):
* Support Tickets (customer service): Support Team. Sees the tickets they opened.
* Technician Issues: Technician. Sees tickets in the technician stage assigned to him.
* Maintenance Issues: Maintenance Technician. Same for the maintenance stage.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, getdate, strip_html

from delivery_management.customer_support import tickets as t

VIEWS = {
	"cs": (t.ROLE_CS, None, "sm_cs_user"),
	"technician": (t.ROLE_TECH, t.STAGE_TECH, "sm_technician"),
	"maintenance": (t.ROLE_MAINT, t.STAGE_MAINT, "sm_maint_technician"),
}


def _require_role(*roles):
	if t.is_admin() or any(t.has_role(r) for r in roles):
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _ticket(name):
	ticket = t.get_ticket(name)
	t.require_view(ticket)
	return ticket


def _names(users) -> dict:
	users = [u for u in set(users) if u]
	if not users:
		return {}
	return {r.name: r.full_name or r.name for r in frappe.get_all("User", filters={"name": ["in", users]},
		fields=["name", "full_name"])}


def _row(r, names) -> dict:
	return {
		"name": r.name, "subject": r.subject, "customer": r.customer, "customer_name": r.customer_name or r.customer,
		"customer_phone": r.sm_customer_phone, "priority": r.priority, "issue_type": r.issue_type,
		"status": r.status, "resolved": r.status in t.RESOLVED, "stage": r.sm_stage, "creation": r.creation,
		"address": r.sm_address, "visit_datetime": r.sm_visit_datetime, "item_code": r.sm_item_code,
		"sales_invoice": r.sm_sales_invoice,
		"technician": r.sm_technician, "technician_name": names.get(r.sm_technician),
		"maint_technician": r.sm_maint_technician, "maint_technician_name": names.get(r.sm_maint_technician),
		"has_maint_report": bool(cstr(r.sm_maint_report).strip()),
		"cs_user_name": names.get(r.sm_cs_user),
	}


@frappe.whitelist()
def get_tickets(view, search=None, visit_date=None, status=None):
	"""Old /cs, /technicians/my-issues and /maint-tech/my-issues (no paging there either;
	capped at 500, newest first)."""
	if view not in VIEWS:
		frappe.throw(_("Unknown view {0}").format(view))
	role, stage, own_field = VIEWS[view]
	_require_role(role)
	conditions = ["ifnull(sm_stage, '') != ''"]
	values: dict = {}
	if stage:
		conditions.append("sm_stage = %(stage)s")
		values["stage"] = stage
	if not t.is_admin():
		conditions.append(f"{own_field} = %(me)s")
		values["me"] = frappe.session.user
	if visit_date:
		conditions.append("date(sm_visit_datetime) = %(visit_date)s")
		values["visit_date"] = getdate(visit_date)
	if status == "open":
		conditions.append("status not in ('Resolved', 'Closed')")
	elif status == "resolved":
		conditions.append("status in ('Resolved', 'Closed')")
	if search:
		values["search"] = f"%{cstr(search).strip()}%"
		conditions.append("(name like %(search)s or subject like %(search)s or customer_name like %(search)s "
			"or customer like %(search)s or sm_customer_phone like %(search)s)")
	rows = frappe.db.sql(
		f"""select name, subject, customer, customer_name, sm_customer_phone, priority, issue_type, status, sm_stage,
			creation, sm_address, sm_visit_datetime, sm_item_code, sm_sales_invoice, sm_technician,
			sm_maint_technician, sm_maint_report, sm_cs_user
		from `tabIssue` where {' and '.join(conditions)} order by creation desc limit 500""",
		values,
		as_dict=True,
	)
	names = _names([u for r in rows for u in (r.sm_technician, r.sm_maint_technician, r.sm_cs_user)])
	return {
		"rows": [_row(r, names) for r in rows],
		"is_admin": t.is_admin(),
		"can_create": t.is_admin() or t.has_role(t.ROLE_CS),
	}


@frappe.whitelist()
def get_ticket_detail(name):
	ticket = _ticket(name)
	names = _names([ticket.sm_technician, ticket.sm_maint_technician, ticket.sm_cs_user, ticket.sm_maint_report_by]
		+ [v.technician for v in ticket.sm_tech_visits])
	row = _row(ticket, names)
	user = frappe.session.user
	row.update({
		"description": strip_html(cstr(ticket.description)).strip(),
		# ERPNext fills raised_by with the creating user when no customer email is given.
		"customer_email": ticket.raised_by if ticket.raised_by not in (ticket.sm_cs_user, ticket.owner) else None,
		"serial_no": ticket.sm_serial_no,
		"delivery_note": ticket.sm_delivery_note,
		"item_name": ticket.sm_item_code and frappe.db.get_value("Item", ticket.sm_item_code, "item_name"),
		"cs_report": ticket.sm_cs_report,
		"maint_report": ticket.sm_maint_report,
		"maint_report_by_name": names.get(ticket.sm_maint_report_by),
		"pickup": {"area": ticket.sm_pickup_area, "date": ticket.sm_pickup_date, "time": ticket.sm_pickup_time}
			if ticket.sm_pickup_date else None,
		"visits": [{
			"name": v.name, "visit_on": v.visit_on, "technician": v.technician,
			"technician_name": names.get(v.technician), "completion_status": v.completion_status,
			"report": v.report, "maintenance_visit": v.maintenance_visit, "warranty_claim": v.warranty_claim,
			"can_edit": t.is_admin() or v.technician == user,
		} for v in ticket.sm_tech_visits],
		"can": {
			"update_schedule": not row["resolved"] and (t.is_admin() or t.has_role(t.ROLE_CS)),
			"update_cs_report": not row["resolved"] and ticket.sm_stage == t.STAGE_CS,
			"move_to_technician": not row["resolved"] and ticket.sm_stage == t.STAGE_CS,
			"log_visit": not row["resolved"] and ticket.sm_stage != t.STAGE_CS
				and (t.is_admin() or ticket.sm_technician == user),
			"move_to_maintenance": not row["resolved"] and ticket.sm_stage == t.STAGE_TECH,
			"maint_report": not row["resolved"] and ticket.sm_stage == t.STAGE_MAINT
				and (t.is_admin() or ticket.sm_maint_technician == user),
			"resolve": not row["resolved"],
		},
	})
	return row


# ---------------------------------------------------------------------------
# Customer service
# ---------------------------------------------------------------------------


@frappe.whitelist()
def lookup_customer(phone=None, customer=None):
	"""The customer of a phone number and his past orders, to fill the ticket by a click."""
	_require_role(t.ROLE_CS)
	customer = customer or t.find_customer(phone)
	if not customer:
		return {"customer": None, "orders": []}
	return {
		"customer": customer,
		"customer_name": frappe.db.get_value("Customer", customer, "customer_name"),
		"orders": customer_orders(customer),
	}


def _address_of(si) -> str:
	parts = [si.get("dm_delivery_area"), si.get("dm_block_no") and f"Block {si.dm_block_no}",
		si.get("dm_road_no") and f"Road {si.dm_road_no}", si.get("dm_building_no") and f"Building {si.dm_building_no}"]
	text = ", ".join(p for p in parts if p)
	if not text and si.get("address_display"):
		text = ", ".join(l.strip() for l in strip_html(si.address_display.replace("<br>", "\n")).splitlines() if l.strip())
	return text


def customer_orders(customer: str, limit: int = 15) -> list[dict]:
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "docstatus": 1, "is_return": 0},
		fields=["name", "posting_date", "grand_total", "currency", "status", "update_stock", "dm_delivery_area",
			"dm_block_no", "dm_road_no", "dm_building_no", "address_display", "dm_order_type"],
		order_by="posting_date desc, creation desc",
		limit=limit,
	)
	result = []
	for si in invoices:
		voucher_type, voucher_no = t.delivering_voucher(si.name)
		serials: dict[str, list[str]] = {}
		if voucher_no:
			for item_code, sn in frappe.db.sql(
				"""select sbb.item_code, sbe.serial_no from `tabSerial and Batch Entry` sbe
				join `tabSerial and Batch Bundle` sbb on sbb.name = sbe.parent
				where sbb.voucher_type = %s and sbb.voucher_no = %s and sbb.docstatus = 1 and sbb.is_cancelled = 0
				and sbb.type_of_transaction = 'Outward' and ifnull(sbe.serial_no, '') != ''""",
				(voucher_type, voucher_no),
			):
				serials.setdefault(item_code, []).append(sn)
		items = frappe.get_all("Sales Invoice Item", filters={"parent": si.name},
			fields=["item_code", "item_name", "qty"], order_by="idx")
		# Bundle components (the serialised part of a bundle is what breaks).
		items += [dict(r, is_component=1) for r in frappe.get_all("Packed Item",
			filters={"parent": voucher_no or si.name, "parenttype": voucher_type or "Sales Invoice"},
			fields=["item_code", "item_name", "qty", "parent_item"], order_by="idx")]
		for it in items:
			it["serials"] = serials.get(it["item_code"], [])
		result.append({
			"name": si.name, "posting_date": si.posting_date, "grand_total": si.grand_total, "currency": si.currency,
			"status": si.status, "order_type": si.dm_order_type, "address": _address_of(si),
			"delivery_note": voucher_no if voucher_type == "Delivery Note" else None, "items": items,
		})
	return result


@frappe.whitelist(methods=["POST"])
def create_ticket(data):
	_require_role(t.ROLE_CS)
	data = frappe.parse_json(data) if isinstance(data, str) else data
	return {"name": t.create_ticket(data)}


@frappe.whitelist(methods=["POST"])
def update_schedule(name, address=None, visit_datetime=None):
	ticket = _ticket(name)
	return {"changed": t.update_schedule(ticket, address, visit_datetime)}


@frappe.whitelist(methods=["POST"])
def update_cs_report(name, report):
	_require_role(t.ROLE_CS)
	t.update_cs_report(_ticket(name), report)


@frappe.whitelist()
def get_technician_options(name=None, visit_datetime=None, day=None):
	_require_role(t.ROLE_CS)
	if name and not visit_datetime and not day:
		visit_datetime = frappe.db.get_value("Issue", name, "sm_visit_datetime")
	return t.technician_options(visit_datetime=visit_datetime, day=day, exclude=name)


@frappe.whitelist(methods=["POST"])
def move_to_technician(name, technician):
	_require_role(t.ROLE_CS)
	t.move_to_technician(_ticket(name), technician)


# ---------------------------------------------------------------------------
# Technician
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def log_visit(name, completion_status, report):
	_require_role(t.ROLE_TECH)
	return t.log_technician_visit(_ticket(name), completion_status, report)


@frappe.whitelist(methods=["POST"])
def update_visit_report(name, row, report):
	_require_role(t.ROLE_TECH)
	t.update_visit_report(_ticket(name), row, report)


@frappe.whitelist()
def get_maintenance_technicians():
	_require_role(t.ROLE_TECH)
	return t.users_with_role(t.ROLE_MAINT)


@frappe.whitelist()
def get_pickup_options(area=None, date=None):
	"""Areas, and the delivery slots of one area and day (the Delivery Slots capacity)."""
	_require_role(t.ROLE_TECH)
	from delivery_management.slots import engine

	areas = frappe.get_all("Delivery Area", filters={"enabled": 1}, pluck="name", order_by="name")
	slots = []
	if area and date:
		slots = [{"time": str(s["delivery_time"])[:5], "delivery_time": str(s["delivery_time"]),
			"available": bool(s["available"]), "remaining": s.get("remaining")}
			for s in engine.get_delivery_slots(area, getdate(date))]
	return {"areas": areas, "slots": slots}


@frappe.whitelist(methods=["POST"])
def move_to_maintenance(name, maint_technician, area=None, delivery_date=None, delivery_time=None):
	_require_role(t.ROLE_TECH)
	t.move_to_maintenance(_ticket(name), maint_technician,
		{"area": area, "delivery_date": delivery_date, "delivery_time": delivery_time})


# ---------------------------------------------------------------------------
# Maintenance technician / everyone
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def save_maint_report(name, report):
	_require_role(t.ROLE_MAINT)
	t.save_maint_report(_ticket(name), report)


@frappe.whitelist(methods=["POST"])
def resolve(name):
	ticket = _ticket(name)
	return {"warranty_claims_closed": t.resolve(ticket)}
