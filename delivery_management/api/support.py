"""Endpoints of the Support pages and of the support trips on the delivery pages. The rules
live in ``customer_support.tickets`` (the ticket) and ``customer_support.flow`` (the journey).

Pages and who opens them (System Manager = the old admin, sees and does everything):
* Support Tickets (customer service): Support Team. The tickets they opened, in tabs:
  open, ready for return, closed.
* Technician Issues: TV Driver. His pickup visits (report, then Fetch Item or Resolve).
* Maintenance Issues: Maintenance Technician. Every ticket whose item is with the workshop.
* Driver Assignment (office): the trips next to the TV orders. Delivery Schedule
  (Maintenance tab): the return trips. Goods in Transit: the customer items in transit.
"""

from __future__ import annotations

import base64

import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate, strip_html

from delivery_management.customer_support import flow
from delivery_management.customer_support import tickets as t

FIELDS = """name, subject, customer, customer_name, sm_customer_phone, priority, issue_type, status, sm_stage, sm_step,
	creation, sm_area, sm_block_no, sm_road_no, sm_building_no, sm_address, sm_trip_date, sm_trip_time, sm_driver,
	sm_item_code, sm_serial_no, sm_sales_invoice, sm_technician, sm_maint_report, sm_cs_user, sm_warranty_expiry,
	sm_warehouse, sm_repair_result, sm_last_note, sm_not_home_count"""


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


def full_address(r) -> str:
	parts = [r.get("sm_area"), r.get("sm_block_no") and _("Block {0}").format(r.sm_block_no),
		r.get("sm_road_no") and _("Road {0}").format(r.sm_road_no),
		r.get("sm_building_no") and _("Building {0}").format(r.sm_building_no), r.get("sm_address")]
	return ", ".join(cstr(p) for p in parts if p)


def _row(r, names) -> dict:
	return {
		"name": r.name, "subject": r.subject, "customer": r.customer, "customer_name": r.customer_name or r.customer,
		"customer_phone": r.sm_customer_phone, "priority": r.priority, "issue_type": r.issue_type,
		"status": r.status, "resolved": r.status in t.RESOLVED, "stage": r.sm_stage, "step": r.sm_step or t.STEP_CS,
		"creation": r.creation, "area": r.sm_area, "block_no": r.sm_block_no, "road_no": r.sm_road_no,
		"building_no": r.sm_building_no, "address_line": r.sm_address, "address": full_address(r),
		"trip_date": r.sm_trip_date, "trip_time": r.sm_trip_time and str(r.sm_trip_time)[:5],
		"driver": r.sm_driver, "driver_name": names.get(r.sm_driver),
		"item_code": r.sm_item_code, "serial_no": r.sm_serial_no, "sales_invoice": r.sm_sales_invoice,
		"technician": r.sm_technician, "technician_name": names.get(r.sm_technician),
		"has_maint_report": bool(cstr(r.sm_maint_report).strip()), "repair_result": r.sm_repair_result,
		"cs_user_name": names.get(r.sm_cs_user), "warranty_expiry": r.sm_warranty_expiry,
		"under_warranty": t.under_warranty(r), "warehouse": r.sm_warehouse, "take_from": _take_from(r),
		"last_note": r.sm_last_note, "not_home_count": cint(r.sm_not_home_count),
	}


def _take_from(r) -> str | None:
	"""Where the return driver collects the item: the workshop store or the main store."""
	if (r.sm_step or t.STEP_CS) in (t.STEP_READY, t.STEP_RETURN, t.STEP_BACK) and r.sm_warehouse:
		return r.sm_warehouse
	return None


def _rows(conditions: list[str], values: dict, order: str = "creation desc", limit: int = 500, offset: int = 0) -> list[dict]:
	rows = frappe.db.sql(f"select {FIELDS} from `tabIssue` where {' and '.join(conditions)} order by {order} "
		f"limit {cint(limit)} offset {cint(offset)}", values, as_dict=True)
	names = _names([u for r in rows for u in (r.sm_driver, r.sm_technician, r.sm_cs_user)])
	return [_row(r, names) for r in rows]


def _search(conditions: list[str], values: dict, search) -> None:
	if search:
		values["search"] = f"{cstr(search).strip()}%"  # from the start: the indexes can be used
		conditions.append("(name like %(search)s or subject like %(search)s or customer_name like %(search)s "
			"or customer like %(search)s or sm_customer_phone like %(search)s)")


# view -> (role, {tab: steps})
OPEN_CS = (t.STEP_CS, t.STEP_PICKUP, t.STEP_TO_WORKSHOP, t.STEP_REPAIR, t.STEP_RETURN, t.STEP_OUT, t.STEP_BACK)
VIEWS = {
	"cs": (t.ROLE_CS, {"open": OPEN_CS, "ready": (t.STEP_READY,), "closed": (t.STEP_CLOSED,)}),
	"technician": (t.ROLE_TECH, {"open": (t.STEP_PICKUP,), "closed": (t.STEP_TO_WORKSHOP, t.STEP_REPAIR, t.STEP_READY,
		t.STEP_RETURN, t.STEP_OUT, t.STEP_CLOSED)}),
	"maintenance": (t.ROLE_MAINT, {"open": t.WORKSHOP_STEPS, "closed": (t.STEP_OUT, t.STEP_BACK, t.STEP_CLOSED)}),
}


@frappe.whitelist()
def get_tickets(view, search=None, tab="open", trip_date=None, limit=20, offset=0):
	"""The tickets of one page and tab, a page at a time (``limit`` / ``offset``)."""
	if view not in VIEWS:
		frappe.throw(_("Unknown view {0}").format(view))
	role, tabs = VIEWS[view]
	_require_role(role)
	steps = tabs.get(tab) or tabs["open"]
	conditions = ["sm_step in %(steps)s"]
	values: dict = {"steps": steps}
	me = frappe.session.user
	if not t.is_admin():
		if view == "cs":
			conditions.append("sm_cs_user = %(me)s")
		elif view == "technician":
			# Open: his pickup visits. Done: the visits he made.
			conditions.append("sm_driver = %(me)s" if tab == "open" else "sm_technician = %(me)s")
		elif tab == "closed":
			conditions.append("sm_maint_report_by is not null")
		values["me"] = me
	if view == "maintenance" and tab != "closed":  # only what is (or is coming) in the workshop
		conditions.append("(sm_step = %(to_ws)s or sm_warehouse = %(ws)s)")
		values.update(to_ws=t.STEP_TO_WORKSHOP, ws=flow.get_settings().support_workshop_warehouse or "")
	if trip_date:
		conditions.append("sm_trip_date = %(trip_date)s")
		values["trip_date"] = getdate(trip_date)
	_search(conditions, values, search)
	counts = {}
	for key, tab_steps in tabs.items():
		where = [c for c in conditions if c != "sm_step in %(steps)s"
			and not (key == "closed" and c.startswith("(sm_step = %(to_ws)s"))] + ["sm_step in %(tab_steps)s"]
		if view == "maintenance" and key != "closed" and tab == "closed":
			where.append("(sm_step = %(to_ws)s or sm_warehouse = %(ws)s)")
			values.update(to_ws=t.STEP_TO_WORKSHOP, ws=flow.get_settings().support_workshop_warehouse or "")
		counts[key] = frappe.db.sql(f"select count(*) from `tabIssue` where {' and '.join(where)}",
			{**values, "tab_steps": tab_steps})[0][0]
	limit = min(max(cint(limit) or 20, 1), 200)
	return {
		"rows": _rows(conditions, values, "sm_trip_date is null, sm_trip_date, sm_trip_time, creation desc"
			if view == "technician" and tab == "open" else "creation desc", limit, cint(offset)),
		"counts": counts,
		"total": counts.get(tab if tab in tabs else "open", 0),
		"is_admin": t.is_admin(),
		"can_create": t.is_admin() or t.has_role(t.ROLE_CS),
	}


@frappe.whitelist()
def get_ticket_detail(name, view=None):
	"""The ticket, with the buttons of the page it is opened from (``view``): each page
	does its own part. Opened anywhere else, it is only shown."""
	ticket = _ticket(name)
	names = _names([ticket.sm_driver, ticket.sm_technician, ticket.sm_cs_user, ticket.sm_maint_report_by]
		+ [v.technician for v in ticket.sm_tech_visits])
	row = _row(ticket, names)
	user = frappe.session.user
	step = row["step"]
	is_cs = view == "cs" and (t.is_admin() or t.has_role(t.ROLE_CS))
	workshop = view == "maintenance" and (t.is_admin() or t.has_role(t.ROLE_MAINT))
	driver = bool(ticket.sm_driver) and (ticket.sm_driver == user or t.is_admin())
	on_visit = view == "technician" and driver
	on_return = view == "schedule" and driver
	store = view == "store" and flow.is_stock_keeper()
	row.update({
		"description": strip_html(cstr(ticket.description)).strip(),
		# ERPNext fills raised_by with the creating user when no customer email is given.
		"customer_email": ticket.raised_by if ticket.raised_by not in (ticket.sm_cs_user, ticket.owner) else None,
		"delivery_note": ticket.sm_delivery_note,
		"item_name": ticket.sm_item_code and frappe.db.get_value("Item", ticket.sm_item_code, "item_name"),
		"has_serial": bool(ticket.sm_item_code) and flow.has_serial(ticket.sm_item_code),
		"cs_report": ticket.sm_cs_report,
		"maint_report": ticket.sm_maint_report,
		"maint_report_by_name": names.get(ticket.sm_maint_report_by),
		"visits": [{
			"name": v.name, "visit_on": v.visit_on, "technician": v.technician,
			"technician_name": names.get(v.technician), "completion_status": v.completion_status,
			"report": v.report, "maintenance_visit": v.maintenance_visit,
			"can_edit": view == "technician" and (t.is_admin() or v.technician == user),
		} for v in ticket.sm_tech_visits],
		"files": frappe.get_all("File", filters={"attached_to_doctype": t.ISSUE, "attached_to_name": ticket.name},
			fields=["file_name", "file_url", "creation"], order_by="creation desc"),
		"can": {
			"update_address": is_cs and step in (t.STEP_CS, t.STEP_PICKUP, t.STEP_TO_WORKSHOP, t.STEP_REPAIR,
				t.STEP_READY, t.STEP_RETURN),
			"update_cs_report": False,  # the notes are written with each step
			"book_pickup": is_cs and step == t.STEP_CS,
			"book_return": is_cs and step == t.STEP_READY,
			"log_visit": on_visit and step == t.STEP_PICKUP,
			"fetch_item": on_visit and step == t.STEP_PICKUP,
			"not_home": (on_visit and step == t.STEP_PICKUP) or (on_return and step == t.STEP_OUT),
			"resolve_note": view in ("cs", "maintenance") and flow.can_resolve(ticket, view),
			"receive": workshop and step == t.STEP_TO_WORKSHOP,
			"repair": workshop and step == t.STEP_REPAIR,
			"send_out": workshop and step == t.STEP_RETURN and bool(ticket.sm_driver) and flow.in_workshop(ticket),
			"complete": on_return and step == t.STEP_OUT,
			"receive_back": store and step == t.STEP_BACK,
			"store_send": store and step == t.STEP_RETURN and bool(ticket.sm_driver)
				and ticket.sm_warehouse == frappe.db.get_single_value("Delivery Settings", "support_store_warehouse"),
			"resolve": bool(view) and flow.can_resolve(ticket, view),
		},
		"waiting_driver": view in ("technician", "maintenance", "cs") and step in (t.STEP_PICKUP, t.STEP_RETURN)
			and not ticket.sm_driver,
		"resolve_needs_serial": step in flow.IN_STORE_STEPS and bool(ticket.sm_warehouse) and not ticket.sm_serial_no
			and bool(ticket.sm_item_code) and flow.has_serial(ticket.sm_item_code),
		"notes": [{"content": c.content, "creation": c.creation} for c in frappe.get_all("Comment",
			filters={"reference_doctype": t.ISSUE, "reference_name": ticket.name, "comment_type": "Comment"},
			fields=["content", "creation"], order_by="creation asc")],
	})
	return row


# ---------------------------------------------------------------------------
# Customer service
# ---------------------------------------------------------------------------


@frappe.whitelist()
def lookup_customer(phone=None, customer=None):
	"""The customer of a phone number, his last address and his past orders, to fill the
	ticket by a click."""
	_require_role(t.ROLE_CS)
	customer = customer or t.find_customer(phone)
	if not customer:
		return {"customer": None, "orders": [], "address": {}}
	return {
		"customer": customer,
		"customer_name": frappe.db.get_value("Customer", customer, "customer_name"),
		"address": t.last_address(customer),
		"orders": customer_orders(customer),
	}


@frappe.whitelist()
def get_areas():
	return frappe.get_all("Delivery Area", filters={"enabled": 1}, pluck="name", order_by="name",
		ignore_permissions=True)


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
	# The same answer as t.delivering_voucher per invoice, read for all of them at once.
	names = [si.name for si in invoices] or [""]
	lines = frappe.get_all("Sales Invoice Item", filters={"parent": ["in", names]},
		fields=["parent", "item_code", "item_name", "qty", "sales_order", "delivery_note"], order_by="parent, idx")
	orders = {r.sales_order for r in lines if r.sales_order}
	notes_of_order: dict[str, list[str]] = {}
	if orders:
		for parent, order in frappe.db.sql("""select distinct dni.parent, dni.against_sales_order
			from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
			where dni.against_sales_order in %(orders)s and dn.docstatus = 1 and dn.is_return = 0
			order by dn.modified desc""", {"orders": tuple(orders)}):
			notes_of_order.setdefault(order, []).append(parent)
	direct = {r.delivery_note for r in lines if r.delivery_note}
	submitted = set(frappe.get_all("Delivery Note", filters={"name": ["in", list(direct) or [""]], "docstatus": 1,
		"is_return": 0}, pluck="name"))
	voucher = {}
	for si in invoices:
		if cint(si.update_stock):
			voucher[si.name] = ("Sales Invoice", si.name)
			continue
		mine = [r for r in lines if r.parent == si.name]
		candidates = [r.delivery_note for r in mine if r.delivery_note in submitted]
		for order in dict.fromkeys(r.sales_order for r in mine if r.sales_order):
			candidates += notes_of_order.get(order, [])
		voucher[si.name] = ("Delivery Note", candidates[0]) if candidates else (None, None)
	vouchers = [v for v in voucher.values() if v[1]]
	serials: dict[tuple, dict[str, list[str]]] = {}
	packed: dict[tuple, list] = {}
	if vouchers:
		nos = tuple({v[1] for v in vouchers})
		for vtype, vno, item_code, sn in frappe.db.sql(
			"""select sbb.voucher_type, sbb.voucher_no, sbb.item_code, sbe.serial_no from `tabSerial and Batch Entry` sbe
			join `tabSerial and Batch Bundle` sbb on sbb.name = sbe.parent
			where sbb.voucher_no in %(nos)s and sbb.docstatus = 1 and sbb.is_cancelled = 0
			and sbb.type_of_transaction = 'Outward' and sbe.serial_no is not null and sbe.serial_no != ''""",
			{"nos": nos},
		):
			serials.setdefault((vtype, vno), {}).setdefault(item_code, []).append(sn)
	# Bundle components (the serialised part of a bundle is what breaks).
	packed_parents = [v[1] for v in voucher.values() if v[1]] + [n for n, v in voucher.items() if not v[1]]
	for r in frappe.get_all("Packed Item", filters={"parent": ["in", packed_parents or [""]],
			"parenttype": ["in", ["Sales Invoice", "Delivery Note"]]},
			fields=["parent", "parenttype", "item_code", "item_name", "qty", "parent_item"], order_by="parent, idx"):
		packed.setdefault((r.parenttype, r.parent), []).append(r)
	result = []
	for si in invoices:
		voucher_type, voucher_no = voucher[si.name]
		found = serials.get((voucher_type, voucher_no), {}) if voucher_no else {}
		items = [{"item_code": r.item_code, "item_name": r.item_name, "qty": r.qty} for r in lines if r.parent == si.name]
		items += [dict(item_code=r.item_code, item_name=r.item_name, qty=r.qty, parent_item=r.parent_item, is_component=1)
			for r in packed.get((voucher_type or "Sales Invoice", voucher_no or si.name), [])]
		for it in items:
			it["serials"] = found.get(it["item_code"], [])
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
def update_address(name, data):
	data = frappe.parse_json(data) if isinstance(data, str) else data
	return {"changed": t.update_address(_ticket(name), data)}


@frappe.whitelist(methods=["POST"])
def update_cs_report(name, report):
	_require_role(t.ROLE_CS)
	t.update_cs_report(_ticket(name), report)


@frappe.whitelist()
def get_slot_options(area=None, date=None):
	"""Delivery slots of one area and day (the same capacity as the POS orders)."""
	_require_role(t.ROLE_CS)
	return {"slots": flow.slot_options(area, date) if area and date else []}


@frappe.whitelist(methods=["POST"])
def book_trip(name, area, delivery_date, delivery_time, note=None):
	flow.book_trip(name, area, delivery_date, delivery_time, note)


# ---------------------------------------------------------------------------
# Technician (pickup visit and return trip)
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def log_visit(name, completion_status, report):
	_require_role(t.ROLE_TECH)
	return t.log_technician_visit(_ticket(name), completion_status, report)


@frappe.whitelist(methods=["POST"])
def update_visit_report(name, row, report):
	_require_role(t.ROLE_TECH)
	t.update_visit_report(_ticket(name), row, report)


@frappe.whitelist(methods=["POST"])
def resolve_ticket(name, view, note, serial_no=None):
	return {"stock_entry": flow.resolve_ticket(name, view, note, serial_no)}


@frappe.whitelist(methods=["POST"])
def fetch_item(name, serial_no=None, report=None, completion_status="Partially Completed"):
	return {"stock_entry": flow.fetch_item(name, serial_no, report, completion_status)}


@frappe.whitelist(methods=["POST"])
def customer_not_home(name, note=None):
	flow.customer_not_home(name, note)


@frappe.whitelist(methods=["POST"])
def complete_return(name, note=None):
	return {"stock_entry": flow.complete_return(name, note)}


@frappe.whitelist(methods=["POST"])
def upload_file(name, kind, filename, filedata):
	"""Repair proof (workshop), or the hand-over photo / video and signature (driver)."""
	if "," in filedata[:100]:
		filedata = filedata.split(",", 1)[1]
	return {"file_url": flow.attach(name, kind, filename, base64.b64decode(filedata))}


@frappe.whitelist()
def get_return_trips(search=None, start_date=None, end_date=None):
	"""Delivery Schedule, Maintenance tab: the return trips (booked or out). A driver
	gets only his own."""
	if not (t.is_office() or t.has_role(t.ROLE_TECH)):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	conditions = ["sm_step in %(steps)s", "sm_driver is not null"]
	values = {"steps": (t.STEP_RETURN, t.STEP_OUT)}
	if not t.is_office():
		conditions.append("sm_driver = %(me)s")
		values["me"] = frappe.session.user
	if start_date:
		conditions.append("sm_trip_date >= %(start)s")
		values["start"] = getdate(start_date)
	if end_date:
		conditions.append("sm_trip_date <= %(end)s")
		values["end"] = getdate(end_date)
	_search(conditions, values, search)
	return {"rows": _rows(conditions, values, "sm_trip_date, sm_trip_time, name")}


# ---------------------------------------------------------------------------
# Workshop
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def receive_in_workshop(name, serial_no=None):
	return {"stock_entry": flow.receive_in_workshop(name, serial_no)}


@frappe.whitelist(methods=["POST"])
def finish_repair(name, result, report):
	flow.finish_repair(name, result, report)


@frappe.whitelist(methods=["POST"])
def send_out(name, serial_no=None):
	return {"stock_entry": flow.send_out(name, serial_no)}


# ---------------------------------------------------------------------------
# Delivery office: Driver Assignment and Goods in Transit
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_driver_options(name):
	if not t.is_office():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return flow.driver_options(name)


@frappe.whitelist(methods=["POST"])
def assign_driver(name, driver=None):
	flow.assign_driver(name, driver or None)


@frappe.whitelist()
def get_transit_items():
	"""Customer items in the transit store: fetched from the customer, or out for return."""
	if not (t.is_office() or t.has_role(t.ROLE_MAINT)):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return _rows(["sm_step in %(steps)s"], {"steps": (t.STEP_TO_WORKSHOP, t.STEP_OUT, t.STEP_BACK)}, "modified desc")


# ---------------------------------------------------------------------------
# Stock keeper: items brought back (customer not home) and their next return trip
# ---------------------------------------------------------------------------


def _require_stock_keeper():
	if not flow.is_stock_keeper():
		frappe.throw(_("Only the stock keeper or the delivery office can do this."), frappe.PermissionError)


@frappe.whitelist()
def get_store_items(search=None):
	"""Stock Keeper page, Support tab: ``back`` = items the driver brings back to scan into the
	main store; ``send`` = items in the main store whose return driver is assigned."""
	_require_stock_keeper()
	back, bv = ["sm_step = %(back)s"], {"back": t.STEP_BACK}
	send, sv = ["sm_step = %(ret)s", "sm_driver is not null", "sm_warehouse = %(main)s"], \
{"ret": t.STEP_RETURN, "main": frappe.db.get_single_value("Delivery Settings", "support_store_warehouse") or ""}
	_search(back, bv, search)
	_search(send, sv, search)
	return {"back": _rows(back, bv, "modified", 200),
		"send": _rows(send, sv, "sm_trip_date, sm_trip_time, name", 200), "main": sv["main"]}


@frappe.whitelist(methods=["POST"])
def receive_back(name, serial_no=None):
	_require_stock_keeper()
	return {"stock_entry": flow.receive_back(name, serial_no)}


@frappe.whitelist(methods=["POST"])
def store_send_out(name, serial_no=None):
	_require_stock_keeper()
	if not cstr(serial_no).strip() and flow.has_serial(frappe.db.get_value("Issue", name, "sm_item_code")):
		frappe.throw(_("Scan the serial of the item."), title=_("Serial required"))
	return {"stock_entry": flow.send_out(name, serial_no)}
