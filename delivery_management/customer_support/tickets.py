"""Support tickets: port of the old system's support part (issue_tickets.py,
technicians.py, maint_tech.py) onto ERPNext's own documents.

A ticket is an ERPNext **Issue** (the old system already created one per ticket) with
the support fields ``sm_*``. It moves through three stages:

    Customer Service  ->  Technician  ->  Maintenance Technician

* **Customer Service** (role Support Team): opens the ticket for a customer found by
  phone, with an internal report, the visit address and time.
* **Technician** (role Technician): visits the customer. Each visit is logged as an
  ERPNext **Maintenance Visit**; if the item's serial is still under warranty, a
  **Warranty Claim** is opened too. The visits are the ``sm_tech_visits`` rows.
* **Maintenance Technician** (role Maintenance Technician): the workshop. Writes the
  final report. A pickup delivery slot can be booked when the ticket is escalated.

A ticket can be resolved at any stage once that stage's report exists. Resolving sets
the Issue to Resolved and closes its Warranty Claims.

System Manager = the old admin: sees every ticket and may act at every stage.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, get_datetime, getdate, now_datetime, nowdate, strip_html

ISSUE = "Issue"
STAGE_CS = "Customer Service"
STAGE_TECH = "Technician"
STAGE_MAINT = "Maintenance Technician"
STAGES = (STAGE_CS, STAGE_TECH, STAGE_MAINT)

ROLE_CS = "Support Team"
ROLE_TECH = "Technician"
ROLE_MAINT = "Maintenance Technician"
ADMIN_ROLE = "System Manager"

STATUS_OPEN_ON_CREATE = "Replied"  # as the old system: the customer has been answered
RESOLVED = ("Resolved", "Closed")
PRIORITIES = ("Low", "Medium", "High")
COMPLETION = ("Fully Completed", "Partially Completed")


# ---------------------------------------------------------------------------
# Who is who
# ---------------------------------------------------------------------------


def is_admin(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or ADMIN_ROLE in frappe.get_roles(user)


def has_role(role: str, user: str | None = None) -> bool:
	return role in frappe.get_roles(user or frappe.session.user)


def users_with_role(role: str) -> list[dict]:
	return frappe.db.sql(
		"""select distinct u.name as user, coalesce(nullif(u.full_name, ''), u.name) as full_name
		from `tabUser` u join `tabHas Role` hr on hr.parent = u.name and hr.parenttype = 'User'
		where hr.role = %s and u.enabled = 1 and u.name not in ('Administrator', 'Guest')
		order by full_name""",
		role,
		as_dict=True,
	)


# ---------------------------------------------------------------------------
# Reading tickets
# ---------------------------------------------------------------------------


def is_resolved(ticket) -> bool:
	return ticket.status in RESOLVED


def get_ticket(name: str):
	ticket = frappe.get_doc(ISSUE, name)
	if not ticket.get("sm_stage"):
		frappe.throw(_("{0} is not a support ticket.").format(name))
	return ticket


def require_view(ticket, user: str | None = None) -> None:
	"""Admin: all. Customer service: the tickets they opened. Technicians: their own."""
	user = user or frappe.session.user
	if is_admin(user):
		return
	if has_role(ROLE_CS, user) and ticket.sm_cs_user == user:
		return
	if has_role(ROLE_TECH, user) and ticket.sm_technician == user:
		return
	if has_role(ROLE_MAINT, user) and ticket.sm_maint_technician == user:
		return
	frappe.throw(_("This ticket is not assigned to you."), frappe.PermissionError)


def require_open(ticket, action: str | None = None) -> None:
	if is_resolved(ticket):
		frappe.throw(action or _("This ticket is already {0}.").format(_(ticket.status).lower()))


# ---------------------------------------------------------------------------
# Customer service
# ---------------------------------------------------------------------------


def find_customer(phone: str) -> str | None:
	"""Customer by phone: the customer ID (production uses the phone as ID), its mobile
	number, or a Contact phone linked to a customer."""
	phone = cstr(phone).strip()
	if not phone:
		return None
	name = frappe.db.get_value("Customer", {"name": phone, "disabled": 0}) or frappe.db.get_value(
		"Customer", {"mobile_no": phone, "disabled": 0}, "name", order_by="creation desc"
	)
	if name:
		return name
	row = frappe.db.sql(
		"""select dl.link_name from `tabContact Phone` cp
		join `tabDynamic Link` dl on dl.parent = cp.parent and dl.parenttype = 'Contact' and dl.link_doctype = 'Customer'
		where cp.phone = %s order by cp.creation desc limit 1""",
		phone,
	)
	return row[0][0] if row else None


def ensure_issue_type(issue_type: str | None) -> str | None:
	issue_type = cstr(issue_type).strip()
	if issue_type and not frappe.db.exists("Issue Type", issue_type):
		frappe.get_doc({"doctype": "Issue Type", "__newname": issue_type, "name": issue_type}).insert(
			ignore_permissions=True
		)
	return issue_type or None


def ensure_priority(priority: str | None) -> str:
	priority = priority if priority in PRIORITIES else "Medium"
	if not frappe.db.exists("Issue Priority", priority):
		frappe.get_doc({"doctype": "Issue Priority", "__newname": priority, "name": priority}).insert(
			ignore_permissions=True
		)
	return priority


def _visit_datetime(value, *, allow_past: bool = False):
	if not value:
		return None
	value = get_datetime(value)
	if not allow_past and value < now_datetime():
		frappe.throw(_("The visit datetime cannot be in the past."))
	return value


def create_ticket(data: dict, user: str | None = None) -> str:
	"""Old ``create_issue_ticket``: customer by phone, report required, Issue in stage
	Customer Service owned by the current user."""
	user = user or frappe.session.user
	subject = cstr(data.get("subject")).strip()
	description = cstr(data.get("description")).strip()
	report = cstr(data.get("report")).strip()
	phone = cstr(data.get("customer_phone")).strip()
	email = cstr(data.get("customer_email")).strip() or None
	if not subject:
		frappe.throw(_("Subject is required."))
	if not phone:
		frappe.throw(_("Customer phone is required."))
	if not description:
		frappe.throw(_("Issue description is required."))
	if not report:
		frappe.throw(_("A customer service report is required to create an issue ticket."))
	if email and ("@" not in email or "." not in email):
		frappe.throw(_("Invalid email format. Must contain '@' and a domain."))
	customer = data.get("customer") or find_customer(phone)
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer with phone '{0}' not found. Please create the customer first.").format(phone))

	sales_invoice = data.get("sales_invoice") or None
	if sales_invoice and frappe.db.get_value("Sales Invoice", sales_invoice, "customer") != customer:
		frappe.throw(_("Sales Invoice {0} is not an invoice of this customer.").format(sales_invoice))
	item_code = data.get("item_code") or None
	if item_code and not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} not found.").format(item_code))
	serial_no = data.get("serial_no") or None
	if serial_no and frappe.db.get_value("Serial No", serial_no, "item_code") != item_code:
		frappe.throw(_("Serial No {0} is not a serial of {1}.").format(serial_no, item_code))

	issue = frappe.get_doc({
		"doctype": ISSUE,
		"subject": subject,
		"customer": customer,
		"raised_by": email,
		"priority": ensure_priority(data.get("priority")),
		"issue_type": ensure_issue_type(data.get("issue_type")),
		"description": description,
		"status": STATUS_OPEN_ON_CREATE,
		"company": (sales_invoice and frappe.db.get_value("Sales Invoice", sales_invoice, "company"))
			or frappe.defaults.get_user_default("Company")
			or frappe.db.get_single_value("Global Defaults", "default_company"),
		"sm_stage": STAGE_CS,
		"sm_customer_phone": phone,
		"sm_cs_user": user,
		"sm_cs_report": report,
		"sm_sales_invoice": sales_invoice,
		"sm_item_code": item_code,
		"sm_serial_no": serial_no,
		"sm_delivery_note": _delivery_note_of(sales_invoice, item_code),
		"sm_address": cstr(data.get("address")).strip() or None,
		"sm_visit_datetime": _visit_datetime(data.get("visit_datetime")),
	})
	issue.flags.ignore_permissions = True
	issue.insert()
	return issue.name


def update_schedule(ticket, address=None, visit_datetime=None) -> list[str]:
	"""Old ``update_issue_ticket`` for address / visit time. Empty values are ignored (the
	old API ignored nulls too). The technician's visit moves with it."""
	if not (is_admin() or has_role(ROLE_CS)):
		frappe.throw(_("You do not have permission to update this ticket."), frappe.PermissionError)
	require_open(ticket)
	changed = []
	address = cstr(address).strip()
	if address and address != cstr(ticket.sm_address):
		ticket.sm_address = address
		changed.append("address")
	if visit_datetime:
		value = _visit_datetime(visit_datetime)
		if str(value) != str(ticket.sm_visit_datetime or ""):
			if ticket.sm_technician and ticket.sm_stage == STAGE_TECH:
				check_technician_free(ticket.sm_technician, value, exclude=ticket.name)
			ticket.sm_visit_datetime = value
			changed.append("visit_datetime")
	if changed:
		_save(ticket)
	return changed


def update_cs_report(ticket, report: str) -> None:
	report = cstr(report).strip()
	if is_resolved(ticket):
		frappe.throw(_("Cannot update report: Issue is already {0}.").format(_(ticket.status).lower()))
	if ticket.sm_stage != STAGE_CS:
		frappe.throw(_("Cannot update report: Issue has moved to stage '{0}'.").format(_(ticket.sm_stage)))
	if not report:
		frappe.throw(_("A customer service report is required before proceeding."))
	ticket.sm_cs_report = report
	_save(ticket)


# ---------------------------------------------------------------------------
# Technician availability (old employee_visit_counts, now read from the tickets)
# ---------------------------------------------------------------------------


def gap_minutes() -> int:
	from delivery_management.slots.settings import get_settings

	return get_settings().support_visit_gap_minutes


def visits_on(day, exclude: str | None = None) -> dict[str, list]:
	"""Visit times per technician on ``day``: every ticket assigned to him with a visit
	time that day, whatever its stage or status (the old visit counts were never removed)."""
	day = getdate(day)
	rows = frappe.db.sql(
		f"""select sm_technician, sm_visit_datetime, name from `tab{ISSUE}`
		where ifnull(sm_technician, '') != '' and sm_visit_datetime >= %s and sm_visit_datetime < %s
		and name != %s order by sm_visit_datetime""",
		(day, add_to_date(day, days=1), exclude or ""),
		as_dict=True,
	)
	visits: dict[str, list] = {}
	for r in rows:
		visits.setdefault(r.sm_technician, []).append(r)
	return visits


def conflicts(visit_times, when, gap: int) -> list:
	"""Visits within ``gap`` minutes either side of ``when`` (inclusive, as the old page)."""
	when = get_datetime(when)
	return [v for v in visit_times if abs((get_datetime(v.sm_visit_datetime) - when).total_seconds()) <= gap * 60]


def technician_options(visit_datetime=None, day=None, exclude: str | None = None) -> dict:
	"""Old ``employee-visits``: every Technician with his visits that day, greyed out
	when one is within the gap of the chosen time."""
	when = get_datetime(visit_datetime) if visit_datetime else None
	day = getdate(when or day or nowdate())
	gap = gap_minutes()
	visits = visits_on(day, exclude=exclude)
	options = []
	for tech in users_with_role(ROLE_TECH):
		mine = visits.get(tech.user, [])
		clash = conflicts(mine, when, gap) if when else []
		options.append({
			"user": tech.user,
			"full_name": tech.full_name,
			"number_of_visits": len(mine),
			"visit_times": [get_datetime(v.sm_visit_datetime).strftime("%H:%M") for v in mine],
			"available": not clash,
			"reason": _("Unavailable (±{0} min)").format(gap) if clash else "",
		})
	return {"date": str(day), "gap_minutes": gap, "technicians": options}


def check_technician_free(technician: str, when, exclude: str | None = None) -> None:
	gap = gap_minutes()
	clash = conflicts(visits_on(getdate(when), exclude=exclude).get(technician, []), when, gap)
	if clash:
		frappe.throw(_("This technician has another visit within {0} minutes of the selected time ({1}). "
			"Please pick a different technician or time.").format(
				gap, ", ".join(f"{get_datetime(v.sm_visit_datetime):%H:%M} ({v.name})" for v in clash)))


# ---------------------------------------------------------------------------
# Stage moves
# ---------------------------------------------------------------------------


def move_to_technician(ticket, technician: str) -> None:
	"""Old ``move_to_technician_stage``."""
	if ticket.sm_stage == STAGE_TECH:
		frappe.throw(_("Issue is already moved to technician stage."))
	if ticket.sm_stage != STAGE_CS:
		frappe.throw(_("Only a ticket in the customer service stage can move to a technician."))
	if not ticket.sm_address or not ticket.sm_visit_datetime:
		frappe.throw(_("Cannot move to technician stage: Address and Visit Datetime must be set first."))
	require_open(ticket, _("Can not move resolved or completed issue ticket"))
	if not cstr(ticket.sm_cs_report).strip():
		frappe.throw(_("A customer service report is required before proceeding."))
	if not technician or not has_role(ROLE_TECH, technician):
		frappe.throw(_("{0} is not a Technician.").format(technician or _("Nobody")))
	# Two people assigning the same technician at once: the second one waits and re-checks.
	frappe.db.sql("select name from `tabUser` where name = %s for update", technician)
	check_technician_free(technician, ticket.sm_visit_datetime, exclude=ticket.name)
	ticket.sm_technician = technician
	ticket.sm_stage = STAGE_TECH
	_save(ticket)


def log_technician_visit(ticket, completion_status: str, report: str, user: str | None = None) -> dict:
	"""Old ``create_maintenance_visit_and_warranty``: Maintenance Visit, warranty claim
	when the serial is under warranty (never blocking), and the visit row."""
	user = user or frappe.session.user
	report = cstr(report).strip()
	if not report:
		frappe.throw(_("A technician report is required."))
	if completion_status not in COMPLETION:
		frappe.throw(_("Choose the completion status."))
	if ticket.sm_technician != user and not is_admin(user):
		frappe.throw(_("You are not authorized to create maintenance visit for this ticket."), frappe.PermissionError)
	require_open(ticket, _("Can not move resolved or completed issue ticket"))
	if ticket.sm_stage == STAGE_CS:
		frappe.throw(_("This ticket in customer service, move it first."))
	if not ticket.sm_item_code:
		frappe.throw(_("No item code associated with this ticket."))

	technician = ticket.sm_technician or user
	visit = make_maintenance_visit(ticket, completion_status, report, technician)

	claim = next((r.warranty_claim for r in ticket.get("sm_tech_visits") or [] if r.warranty_claim), None)
	if not claim:
		claim = open_warranty_claim(ticket, report)

	ticket.append("sm_tech_visits", {
		"visit_on": now_datetime(),
		"technician": technician,
		"completion_status": completion_status,
		"report": report,
		"maintenance_visit": visit,
		"warranty_claim": claim,
	})
	_save(ticket)
	return {"maintenance_visit": visit, "warranty_claim": claim}


def update_visit_report(ticket, row_name: str, report: str, user: str | None = None) -> None:
	user = user or frappe.session.user
	report = cstr(report).strip()
	if not report:
		frappe.throw(_("Technician report cannot be empty."))
	row = next((r for r in ticket.get("sm_tech_visits") or [] if r.name == row_name), None)
	if not row:
		frappe.throw(_("Visit not found on this ticket."))
	if row.technician != user and not is_admin(user):
		frappe.throw(_("Only the technician of this visit can edit its report."), frappe.PermissionError)
	row.report = report
	_save(ticket)


def move_to_maintenance(ticket, maint_technician: str, pickup: dict | None = None) -> None:
	"""Old ``move_to_maint_tech_stage``, with the optional pickup slot booked in the same
	transaction (the old page booked it first and leaked it when the move failed)."""
	if ticket.sm_stage == STAGE_MAINT:
		frappe.throw(_("Issue is already moved to maintenance technician stage."))
	require_open(ticket, _("Can not move resolved or completed issue ticket"))
	if ticket.sm_stage != STAGE_TECH:
		frappe.throw(_("Only a ticket in the technician stage can move to a maintenance technician."))
	if not _has_tech_report(ticket):
		frappe.throw(_("A technician report is required before proceeding."))
	if not maint_technician or not has_role(ROLE_MAINT, maint_technician):
		frappe.throw(_("{0} is not a Maintenance Technician.").format(maint_technician or _("Nobody")))

	pickup = {k: v for k, v in (pickup or {}).items() if v}
	if pickup:
		if not all(pickup.get(k) for k in ("area", "delivery_date", "delivery_time")):
			frappe.throw(_("Choose the area, date and time of the pickup slot, or none of them."))
		from delivery_management.slots import engine

		engine.release(ISSUE, ticket.name)
		engine.reserve(ISSUE, ticket.name, area=pickup["area"], delivery_date=getdate(pickup["delivery_date"]),
			delivery_time=pickup["delivery_time"], customer=ticket.customer,
			contact_phone=ticket.sm_customer_phone or None,
			remarks=_("Pickup for support ticket {0}").format(ticket.name))
		ticket.sm_pickup_area = pickup["area"]
		ticket.sm_pickup_date = getdate(pickup["delivery_date"])
		ticket.sm_pickup_time = pickup["delivery_time"]

	ticket.sm_maint_technician = maint_technician
	ticket.sm_stage = STAGE_MAINT
	_save(ticket)


def save_maint_report(ticket, report: str, user: str | None = None) -> None:
	user = user or frappe.session.user
	report = cstr(report).strip()
	if not report:
		frappe.throw(_("A maintenance technician report is required."))
	if ticket.sm_maint_technician != user and not is_admin(user):
		frappe.throw(_("You are not authorized to create maintenance note for this ticket."), frappe.PermissionError)
	require_open(ticket, _("Can not create note for resolved or completed issue ticket"))
	if ticket.sm_stage != STAGE_MAINT:
		frappe.throw(_("This ticket is in {0} stage, it must be in maintenance technician stage.").format(
			_(ticket.sm_stage)))
	ticket.sm_maint_report = report
	ticket.sm_maint_report_by = user
	_save(ticket)


def resolve(ticket, user: str | None = None) -> list[str]:
	"""Old ``resolve_issue_ticket``: the current stage's report is required; the Issue is
	Resolved and its Warranty Claims are closed."""
	user = user or frappe.session.user
	require_open(ticket)
	if ticket.sm_stage == STAGE_CS and not cstr(ticket.sm_cs_report).strip():
		frappe.throw(_("A customer service report is required before proceeding."))
	if ticket.sm_stage == STAGE_TECH and not _has_tech_report(ticket):
		frappe.throw(_("A technician report is required before proceeding."))
	if ticket.sm_stage == STAGE_MAINT and not cstr(ticket.sm_maint_report).strip():
		frappe.throw(_("A maintenance technician report is required before proceeding."))

	closed = []
	for claim in {r.warranty_claim for r in ticket.get("sm_tech_visits") or [] if r.warranty_claim}:
		doc = frappe.get_doc("Warranty Claim", claim)
		if doc.status not in ("Closed", "Cancelled"):
			doc.status = "Closed"
			doc.resolution_date = now_datetime()
			doc.resolved_by = user
			doc.resolution_details = doc.resolution_details or _("Closed with support ticket {0}.").format(ticket.name)
			doc.flags.ignore_permissions = True
			doc.save()
			closed.append(claim)
	ticket.status = "Resolved"
	ticket.sm_resolved_by = user
	_save(ticket)
	return closed


def _has_tech_report(ticket) -> bool:
	return any(cstr(r.report).strip() for r in ticket.get("sm_tech_visits") or [])


def _save(ticket) -> None:
	ticket.flags.ignore_permissions = True
	ticket.save()


# ---------------------------------------------------------------------------
# ERPNext documents made by the technician
# ---------------------------------------------------------------------------


def company_of(ticket) -> str:
	if ticket.get("company"):
		return ticket.company
	if ticket.sm_sales_invoice:
		return frappe.db.get_value("Sales Invoice", ticket.sm_sales_invoice, "company")
	return frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")


def service_person_of(technician: str) -> str:
	"""The technician's own Sales Person (Employee.user_id -> Sales Person.employee), else
	Delivery Settings > Default Service Person, else the first Sales Person."""
	from delivery_management.slots.settings import get_settings

	employee = frappe.db.get_value("Employee", {"user_id": technician, "status": "Active"}, "name")
	person = employee and frappe.db.get_value("Sales Person", {"employee": employee, "enabled": 1, "is_group": 0}, "name")
	# Last resort: any Sales Person; production's old visits used the root "Sales Team".
	person = person or get_settings().support_maint_service_person or frappe.db.get_value(
		"Sales Person", {"enabled": 1}, "name", order_by="is_group asc, creation asc")
	if not person:
		frappe.throw(_("Create a Sales Person (or set Delivery Settings > Default Service Person) first: "
			"a Maintenance Visit needs a service person."))
	return person


def make_maintenance_visit(ticket, completion_status: str, report: str, technician: str) -> str:
	"""A draft Maintenance Visit, as the old system made (the office submits it)."""
	now = now_datetime()
	visit = frappe.get_doc({
		"doctype": "Maintenance Visit",
		"customer": ticket.customer,
		"company": company_of(ticket),
		"mntc_date": now.date(),
		"mntc_time": now.strftime("%H:%M:%S"),
		"maintenance_type": "Breakdown",
		"completion_status": completion_status,
		"customer_feedback": strip_html(cstr(ticket.description))[:500] or None,
		"purposes": [{
			"item_code": ticket.sm_item_code,
			"serial_no": ticket.sm_serial_no or None,
			"service_person": service_person_of(technician),
			"description": report,
			"work_done": report,
			"prevdoc_doctype": ISSUE,
			"prevdoc_docname": ticket.name,
		}],
	})
	visit.flags.ignore_permissions = True
	visit.insert()
	return visit.name


def delivering_voucher(sales_invoice: str | None, item_code: str | None = None) -> tuple[str | None, str | None]:
	"""The document that took the item out of stock for this invoice: the invoice itself
	(branch sale, update_stock) or the submitted Delivery Note of its order."""
	if not sales_invoice:
		return None, None
	si = frappe.db.get_value("Sales Invoice", sales_invoice, ["update_stock", "docstatus"], as_dict=True)
	if not si:
		return None, None
	if cint(si.update_stock):
		return "Sales Invoice", sales_invoice
	orders = frappe.get_all("Sales Invoice Item", filters={"parent": sales_invoice, "sales_order": ["is", "set"]},
		pluck="sales_order", distinct=True)
	direct = frappe.get_all("Sales Invoice Item", filters={"parent": sales_invoice, "delivery_note": ["is", "set"]},
		pluck="delivery_note", distinct=True)
	filters = {"docstatus": 1, "is_return": 0}
	candidates = list(direct)
	if orders:
		candidates += frappe.get_all("Delivery Note Item", filters={"against_sales_order": ["in", orders],
			"docstatus": 1}, pluck="parent", distinct=True)
	if item_code and candidates:
		with_item = [d for d in candidates if frappe.db.exists("Delivery Note Item", {"parent": d, "item_code": item_code})
			or frappe.db.exists("Packed Item", {"parent": d, "parenttype": "Delivery Note", "item_code": item_code})]
		candidates = with_item or candidates
	candidates = [d for d in dict.fromkeys(candidates) if frappe.db.get_value("Delivery Note", d, list(filters))]
	return ("Delivery Note", candidates[0]) if candidates else (None, None)


def _delivery_note_of(sales_invoice, item_code) -> str | None:
	voucher_type, voucher_no = delivering_voucher(sales_invoice, item_code)
	return voucher_no if voucher_type == "Delivery Note" else None


def delivered_serials(voucher_type: str | None, voucher_no: str | None, item_code: str | None = None) -> list[str]:
	"""Serials that left the store with this voucher (v16 keeps them in Serial and Batch Bundles)."""
	if not voucher_no:
		return []
	return frappe.db.sql_list(
		"""select sbe.serial_no from `tabSerial and Batch Entry` sbe
		join `tabSerial and Batch Bundle` sbb on sbb.name = sbe.parent
		where sbb.voucher_type = %s and sbb.voucher_no = %s and sbb.docstatus = 1 and sbb.is_cancelled = 0
		and sbb.type_of_transaction = 'Outward' and ifnull(sbe.serial_no, '') != ''
		and (%s = '' or sbb.item_code = %s) order by sbe.idx""",
		(voucher_type, voucher_no, item_code or "", item_code or ""),
	)


def serial_of_ticket(ticket) -> str | None:
	if ticket.sm_serial_no:
		return ticket.sm_serial_no
	voucher_type, voucher_no = delivering_voucher(ticket.sm_sales_invoice, ticket.sm_item_code)
	serials = delivered_serials(voucher_type, voucher_no, ticket.sm_item_code)
	return serials[0] if serials else None


def open_warranty_claim(ticket, report: str) -> str | None:
	"""A Warranty Claim when the item's serial is under warranty. Never blocks the visit:
	a missing serial, an expired warranty or an error only skip the claim."""
	try:
		serial = serial_of_ticket(ticket)
		if not serial:
			return None
		expiry = frappe.db.get_value("Serial No", serial, "warranty_expiry_date")
		if not expiry or getdate(expiry) < getdate(nowdate()):
			return None
		claim = frappe.get_doc({
			"doctype": "Warranty Claim",
			"status": "Work In Progress",
			"complaint_date": nowdate(),
			"serial_no": serial,
			"item_code": ticket.sm_item_code,
			"customer": ticket.customer,
			"company": company_of(ticket),
			"complaint": report,
			"warranty_amc_status": "Under Warranty",
			"warranty_expiry_date": expiry,
		})
		claim.flags.ignore_permissions = True
		claim.insert()
		return claim.name
	except Exception:
		frappe.log_error(title=f"Support ticket {ticket.name}: warranty claim not created")
		frappe.clear_last_message()
		return None
