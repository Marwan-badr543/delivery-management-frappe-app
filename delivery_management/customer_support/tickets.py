"""Support tickets: an ERPNext **Issue** with the support fields ``sm_*``.

The journey (``sm_step``; the trips, stock moves and steps are in ``flow.py``):

1. **Customer Service** (role Support Team) opens the ticket: customer by phone, item,
   address (area, block, road, building). If the call does not solve it, *Assign to Tech*
   books a delivery slot (date, area, time; the TV slots) -> **Pickup**.
2. The delivery office gives the visit a TV Driver (Driver Assignment, with the TV orders).
3. The driver (Technician Issues) writes his report, then resolves it at the customer (-> **Closed**), or
   fetches the item: it enters the transit store with its serial -> **To Workshop**.
4. The workshop (Maintenance Issues, role Maintenance Technician) receives it into the
   workshop store (**Under Repair**), repairs it with a photo / video as proof
   (**Ready for Return**).
5. Customer Service books the return slot (**Return**), the office gives it a TV Driver,
   the workshop sends the item out to the transit store (**Out for Return**).
6. The driver (Delivery Schedule, Maintenance tab) hands it over with the customer's
   signature and a photo / video: the item leaves the transit store -> **Closed**.
   Customer not home: the driver brings it back (**Back to Store**); the stock keeper scans
   it into the main store (**Ready for Return** again). Customer service books a new slot
   and, on the next trip, the stock keeper sends it out from the main store.

Every step is in the Order Log (Order Control page). A serialised item never moves without
its serial.

System Manager = the old admin: sees every ticket and may act at every step.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, cstr, getdate, now_datetime, nowdate, strip_html

ISSUE = "Issue"
STAGE_CS = "Customer Service"
STAGE_TECH = "Technician"
STAGE_MAINT = "Maintenance Technician"

# sm_step
STEP_CS = "Customer Service"
STEP_PICKUP = "Pickup"
STEP_TO_WORKSHOP = "To Workshop"
STEP_REPAIR = "Under Repair"
STEP_READY = "Ready for Return"
STEP_RETURN = "Return"
STEP_OUT = "Out for Return"
STEP_BACK = "Back to Store"  # customer not home at the return: the driver brings it to the main store
STEP_CLOSED = "Closed"
TRIP_STEPS = (STEP_PICKUP, STEP_RETURN, STEP_OUT)  # a TV Driver goes to the customer
WORKSHOP_STEPS = (STEP_TO_WORKSHOP, STEP_REPAIR, STEP_READY, STEP_RETURN)  # Maintenance Issues
STAGE_OF_STEP = {STEP_CS: STAGE_CS, STEP_PICKUP: STAGE_TECH, STEP_READY: STAGE_CS}

ROLE_CS = "Support Team"
ROLE_TECH = "TV Driver"  # the TV Drivers make the support trips
ROLE_MAINT = "Maintenance Technician"
ADMIN_ROLE = "System Manager"
OFFICE_ROLES = ("System Manager", "Delivery Manager")

STATUS_OPEN_ON_CREATE = "Replied"  # as the old system: the customer has been answered
RESOLVED = ("Resolved", "Closed")
PRIORITIES = ("Low", "Medium", "High")
COMPLETION = ("Fully Completed", "Partially Completed")
ADDRESS_PARTS = (("area", "sm_area"), ("block_no", "sm_block_no"), ("road_no", "sm_road_no"),
	("building_no", "sm_building_no"), ("address", "sm_address"))


# ---------------------------------------------------------------------------
# Who is who
# ---------------------------------------------------------------------------


def is_admin(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or ADMIN_ROLE in frappe.get_roles(user)


def is_office(user: str | None = None) -> bool:
	"""The delivery office: assigns the drivers, sees every ticket."""
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(OFFICE_ROLES) & set(frappe.get_roles(user)))


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


def get_ticket(name: str, for_update: bool = False):
	ticket = frappe.get_doc(ISSUE, name, for_update=for_update)
	if not ticket.get("sm_stage"):
		frappe.throw(_("{0} is not a support ticket.").format(name))
	return ticket


def can_view(ticket, user: str | None = None) -> bool:
	"""Office: all. Customer service: the tickets they opened. Technician: his trips.
	Workshop: every ticket that reached it."""
	user = user or frappe.session.user
	if is_office(user):
		return True
	if has_role(ROLE_CS, user) and ticket.sm_cs_user == user:
		return True
	if has_role(ROLE_TECH, user) and user in (ticket.sm_driver, ticket.sm_technician):
		return True
	step = ticket.get("sm_step") or STEP_CS
	if step in (STEP_BACK, STEP_READY, STEP_RETURN, STEP_OUT) and bool(
			{"Stock User", "Stock Manager"} & set(frappe.get_roles(user))):
		return True  # the stock keeper: items brought back to the main store
	return has_role(ROLE_MAINT, user) and step not in (STEP_CS, STEP_PICKUP)


def require_view(ticket, user: str | None = None) -> None:
	if not can_view(ticket, user):
		frappe.throw(_("This ticket is not assigned to you."), frappe.PermissionError)


def require_open(ticket, action: str | None = None) -> None:
	if is_resolved(ticket):
		frappe.throw(action or _("This ticket is already {0}.").format(_(ticket.status).lower()))


def require_step(ticket, *steps) -> None:
	require_open(ticket)
	if (ticket.get("sm_step") or STEP_CS) not in steps:
		frappe.throw(_("Ticket {0} is at step {1}; this needs {2}.").format(
			ticket.name, _(ticket.sm_step or STEP_CS), " / ".join(_(s) for s in steps)))


def warranty_expiry(serial_no=None, sales_invoice=None, item_code=None):
	"""The serial's warranty expiry, else the invoice date + the item's warranty period (days)."""
	if serial_no:
		expiry = frappe.db.get_value("Serial No", serial_no, "warranty_expiry_date")
		if expiry:
			return getdate(expiry)
	if sales_invoice and item_code:
		days = cint(frappe.db.get_value("Item", item_code, "warranty_period"))
		posted = frappe.db.get_value("Sales Invoice", sales_invoice, "posting_date")
		if days and posted:
			return add_days(getdate(posted), days)
	return None


def under_warranty(ticket) -> bool:
	return bool(ticket.get("sm_warranty_expiry")) and getdate(ticket.sm_warranty_expiry) >= getdate(nowdate())


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


def last_address(customer: str | None) -> dict:
	"""Area, block, road and building of the customer's last order that has them."""
	if not customer:
		return {}
	row = {}
	for doctype in ("Sales Invoice", "Sales Order"):
		row = frappe.db.get_value(doctype, {"customer": customer, "docstatus": 1, "dm_delivery_area": ["is", "set"]},
			["dm_delivery_area as area", "dm_block_no as block_no", "dm_road_no as road_no",
				"dm_building_no as building_no"], as_dict=True, order_by="creation desc") or {}
		if row:
			break
	row = dict(row)
	row["address"] = full_address_line(customer)
	return row


def full_address_line(customer: str) -> str:
	"""The customer's full address as stored in the ERP: his address record (primary, else the
	latest), else the address of his last ticket."""
	name = frappe.db.get_value("Customer", customer, "customer_primary_address") or frappe.db.sql(
		"""select a.name from `tabAddress` a join `tabDynamic Link` l on l.parent = a.name and l.parenttype = 'Address'
		where l.link_doctype = 'Customer' and l.link_name = %s and a.disabled = 0
		order by a.is_primary_address desc, a.modified desc limit 1""", customer)
	name = name[0][0] if isinstance(name, (list, tuple)) and name else name
	if name:
		a = frappe.db.get_value("Address", name, ["address_line1", "address_line2", "city"], as_dict=True)
		if a:
			text = ", ".join(cstr(p).strip() for p in (a.address_line1, a.address_line2, a.city) if cstr(p).strip())
			if text:
				return text
	return cstr(frappe.db.get_value(ISSUE, {"customer": customer, "sm_address": ["is", "set"]}, "sm_address",
		order_by="creation desc"))


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


def _address(data: dict) -> dict:
	"""The ticket's address fields from the page; all required."""
	values = {field: cstr(data.get(key)).strip() or None for key, field in ADDRESS_PARTS}
	labels = {"sm_area": _("Area"), "sm_block_no": _("Block"), "sm_road_no": _("Road"),
		"sm_building_no": _("Building"), "sm_address": _("Full Address")}
	missing = [labels[f] for f, v in values.items() if not v]
	if missing:
		frappe.throw(_("Please fill the address: {0}.").format(", ".join(missing)))
	if not frappe.db.exists("Delivery Area", values["sm_area"]):
		frappe.throw(_("Area {0} not found.").format(values["sm_area"]))
	return values


def create_ticket(data: dict, user: str | None = None) -> str:
	"""Customer by phone, item and address required, Issue at step Customer Service."""
	from delivery_management.control import log

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
	if email and ("@" not in email or "." not in email):
		frappe.throw(_("Invalid email format. Must contain '@' and a domain."))
	# A customer calls from any phone: the ticket keeps that phone, linked to a customer only
	# when one is known by it.
	customer = data.get("customer") or find_customer(phone)
	if customer and not frappe.db.exists("Customer", customer):
		customer = None

	sales_invoice = data.get("sales_invoice") or None
	if sales_invoice and customer and frappe.db.get_value("Sales Invoice", sales_invoice, "customer") != customer:
		frappe.throw(_("Sales Invoice {0} is not an invoice of this customer.").format(sales_invoice))
	item_code = cstr(data.get("item_code")).strip()
	if not item_code:
		frappe.throw(_("Item Code is required."))
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} not found.").format(item_code))
	serial_no = cstr(data.get("serial_no")).strip() or None
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
		"sm_step": STEP_CS,
		"sm_customer_phone": phone,
		"sm_cs_user": user,
		"sm_cs_report": report or None,
		"sm_sales_invoice": sales_invoice,
		"sm_item_code": item_code,
		"sm_serial_no": serial_no,
		"sm_delivery_note": _delivery_note_of(sales_invoice, item_code),
		"sm_warranty_expiry": warranty_expiry(serial_no, sales_invoice, item_code),
		**_address(data),
	})
	issue.flags.ignore_permissions = True
	issue.insert()
	log.add("Ticket opened", support_ticket=issue.name, items=[{"item_code": item_code,
		"item_name": frappe.db.get_value("Item", item_code, "item_name"), "qty": 1,
		"serials": [serial_no] if serial_no else []}], note=subject)
	return issue.name


def update_address(ticket, data: dict) -> list[str]:
	"""Customer service corrects the address before a trip is on the way."""
	if not (is_admin() or has_role(ROLE_CS)):
		frappe.throw(_("You do not have permission to update this ticket."), frappe.PermissionError)
	require_step(ticket, STEP_CS, STEP_PICKUP, STEP_TO_WORKSHOP, STEP_REPAIR, STEP_READY, STEP_RETURN, STEP_BACK)
	values = _address(data)
	changed = [f for f, v in values.items() if cstr(ticket.get(f)) != v]
	if changed:
		ticket.update(values)
		_save(ticket)
	return changed


def update_cs_report(ticket, report: str) -> None:
	report = cstr(report).strip()
	require_open(ticket)
	if not report:
		frappe.throw(_("A customer service report is required before proceeding."))
	ticket.sm_cs_report = report
	_save(ticket)


# ---------------------------------------------------------------------------
# Technician visit report (Maintenance Visit)
# ---------------------------------------------------------------------------


def log_technician_visit(ticket, completion_status: str, report: str, user: str | None = None) -> dict:
	"""The Maintenance Visit and the visit row, by the Technician of the pickup trip."""
	from delivery_management.control import log

	user = user or frappe.session.user
	report = cstr(report).strip()
	if not report:
		frappe.throw(_("A technician report is required."))
	if completion_status not in COMPLETION:
		frappe.throw(_("Choose the completion status."))
	require_step(ticket, STEP_PICKUP)
	if not ticket.sm_driver:
		frappe.throw(_("No driver is assigned to this trip. The delivery office assigns one in Driver Assignment."))
	if ticket.sm_driver != user and not is_admin(user):
		frappe.throw(_("You are not authorized to create maintenance visit for this ticket."), frappe.PermissionError)
	technician = ticket.sm_driver or user
	visit = make_maintenance_visit(ticket, completion_status, report, technician)
	ticket.append("sm_tech_visits", {
		"visit_on": now_datetime(),
		"technician": technician,
		"completion_status": completion_status,
		"report": report,
		"maintenance_visit": visit,
	})
	_save(ticket)
	log.add("Visit logged", support_ticket=ticket.name, driver=technician, note=report,
		reference=("Maintenance Visit", visit) if visit else None)
	add_note(ticket, _("Driver visit report ({0})").format(_(completion_status)), report)
	return {"maintenance_visit": visit}


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


def _has_tech_report(ticket) -> bool:
	return any(cstr(r.report).strip() for r in ticket.get("sm_tech_visits") or [])


def add_note(ticket, title: str, text: str | None = None) -> None:
	"""Every step's note on the Issue itself (its timeline), so the whole story is in one
	place; the last one is shown in the lists."""
	from frappe.utils import escape_html

	text = cstr(text).strip()
	who = frappe.utils.get_fullname(frappe.session.user)
	ticket.add_comment("Comment", f"<b>{escape_html(title)}</b> · {escape_html(who)}"
		+ (f"<br>{escape_html(text)}".replace("\n", "<br>") if text else ""))
	if text:
		frappe.db.set_value(ISSUE, ticket.name, "sm_last_note", f"{title}: {text}"[:1000], update_modified=False)
		ticket.sm_last_note = f"{title}: {text}"[:1000]


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


def make_maintenance_visit(ticket, completion_status: str, report: str, technician: str) -> str | None:
	"""A draft Maintenance Visit, as the old system made (the office submits it). A ticket of
	an unknown customer has none: the report stays on the ticket."""
	if not ticket.customer:
		return None
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
			"item_code": ticket.sm_item_code or None,
			"serial_no": (ticket.sm_item_code and ticket.sm_serial_no) or None,
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
