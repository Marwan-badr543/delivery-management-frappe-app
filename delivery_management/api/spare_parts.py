"""Endpoints of the workshop spare parts: inside the ticket (Maintenance Issues) and the
Spare Parts page (approvals for a System Manager, the store, the history). The rules live in
``customer_support.spare_parts``."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_first_day, nowdate

from delivery_management.customer_support import spare_parts as sp
from delivery_management.customer_support import tickets as t


def _json(value):
	return json.loads(value) if isinstance(value, str) and value else value


def _require_page():
	if not (sp.is_approver() or t.has_role(t.ROLE_MAINT)):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_approver():
	if not sp.is_approver():
		frappe.throw(_("Only a System Manager approves spare parts."), frappe.PermissionError)


# Inside the ticket -----------------------------------------------------------


@frappe.whitelist()
def search_parts(txt=None):
	_require_page()
	return sp.search(txt)


@frappe.whitelist(methods=["POST"])
def request_parts(name, items, reason=None):
	return sp.request_parts(name, _json(items), reason)


@frappe.whitelist(methods=["POST"])
def take(request, serials=None):
	return sp.take(request, _json(serials) or {})


@frappe.whitelist(methods=["POST"])
def cancel(request, reason=None):
	return sp.cancel(request, reason)


@frappe.whitelist(methods=["POST"])
def return_parts(request, rows):
	return sp.return_parts(request, _json(rows))


@frappe.whitelist(methods=["POST"])
def set_repair_cost(name, amount):
	return sp.set_repair_cost(name, amount)


# Approvals ---------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def approve(request, qtys=None):
	return sp.approve(request, _json(qtys) or {})


@frappe.whitelist(methods=["POST"])
def reject(request, reason):
	return sp.reject(request, reason)


def _with_items(reqs: list) -> list:
	"""Requests with their item rows: one query for all of them."""
	if not reqs:
		return reqs
	rows = frappe.get_all("Spare Part Request Item", filters={"parenttype": sp.REQ, "parent": ["in", [r.name for r in reqs]]},
		fields=["parent", "item_code", "item_name", "qty", "approved_qty", "taken_qty", "returned_qty", "rate", "amount"],
		order_by="idx")
	by_parent: dict[str, list] = {}
	for r in rows:
		by_parent.setdefault(r.parent, []).append(r)
	tickets = {i.name: i for i in frappe.get_all(t.ISSUE, filters={"name": ["in", list({r.ticket for r in reqs})]},
		fields=["name", "subject", "customer_name", "sm_item_code", "sm_parts_cost", "sm_step"])}
	for r in reqs:
		r["items"] = by_parent.get(r.name, [])
		r["ticket_info"] = tickets.get(r.ticket)
	return reqs


@frappe.whitelist()
def get_pending():
	"""Waiting for a System Manager, oldest first, each part with its free qty now."""
	_require_approver()
	reqs = _with_items(frappe.get_all(sp.REQ, filters={"status": sp.PENDING}, order_by="creation asc", limit=200,
		fields=["name", "ticket", "technician", "technician_name", "requested_on", "reason", "warehouse"]))
	by_store: dict[str, set] = {}
	for r in reqs:
		by_store.setdefault(r.warehouse, set()).update(i.item_code for i in r["items"])
	stock = {wh: sp.stock_of(items, wh) for wh, items in by_store.items()}
	rates = dict(frappe.db.sql("""select item_code, valuation_rate from `tabBin` where warehouse in %s and item_code in %s""",
		(list(by_store) or [""], list({i for s in by_store.values() for i in s}) or [""]))) if reqs else {}
	for r in reqs:
		for i in r["items"]:
			i.update(stock[r.warehouse].get(i.item_code, {}))
			i["free"] = flt(i.get("free")) + flt(i.qty)  # its own hold is free for it
			i["rate"] = flt(rates.get(i.item_code))
		r["estimate"] = sum(flt(i.qty) * i["rate"] for i in r["items"])
	return reqs


# The store and the history ---------------------------------------------------


@frappe.whitelist()
def get_store(search=None):
	"""Every part in the store: qty, held for open requests, free, value."""
	_require_page()
	warehouse = sp.store()
	like = f"%{cstr(search).strip()}%"
	rows = frappe.db.sql("""select b.item_code, i.item_name, i.has_serial_no, i.stock_uom, b.actual_qty as in_store,
			b.valuation_rate, b.stock_value
		from `tabBin` b join `tabItem` i on i.name = b.item_code
		where b.warehouse = %s and (b.actual_qty != 0 or exists (select 1 from `tabSpare Part Request Item` ri
			join `tabSpare Part Request` r on r.name = ri.parent where r.status in %s and r.warehouse = b.warehouse
			and ri.item_code = b.item_code))
			and (b.item_code like %s or i.item_name like %s)
		order by i.item_name limit 1000""", (warehouse, sp.OPEN, like, like), as_dict=True)
	held = sp.stock_of([r.item_code for r in rows], warehouse)
	for r in rows:
		r.held = held[r.item_code]["held"]
		r.free = held[r.item_code]["free"]
	return {"warehouse": warehouse, "rows": rows, "is_approver": sp.is_approver()}


@frappe.whitelist()
def get_history(search=None, status=None, limit=100):
	"""The latest requests (a technician sees his own), and this month's cost per technician."""
	_require_page()
	filters = {}
	if status:
		filters["status"] = status
	if not sp.is_approver():
		filters["technician"] = frappe.session.user
	or_filters = None
	if cstr(search).strip():
		like = f"%{cstr(search).strip()}%"
		or_filters = {"name": ["like", like], "ticket": ["like", like], "technician_name": ["like", like]}
	reqs = _with_items(frappe.get_all(sp.REQ, filters=filters, or_filters=or_filters, order_by="creation desc",
		limit=min(cint(limit) or 100, 500), fields=["name", "ticket", "status", "technician", "technician_name",
			"requested_on", "taken_on", "total_cost", "reason", "reject_reason", "approver"]))
	month = frappe.db.sql("""select technician, max(technician_name) as technician_name, count(*) as requests,
			sum(total_cost) as cost from `tabSpare Part Request`
		where status in %s and taken_on >= %s {mine} group by technician order by cost desc""".format(
			mine="and technician = %s" if not sp.is_approver() else ""),
		(sp.COSTED, get_first_day(nowdate()), *([frappe.session.user] if not sp.is_approver() else [])), as_dict=True)
	return {"requests": reqs, "month": month}
