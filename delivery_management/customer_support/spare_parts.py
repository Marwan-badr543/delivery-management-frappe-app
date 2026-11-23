"""Workshop spare parts: nothing leaves the Spare Parts Store without a System Manager's OK.

The journey of a Spare Part Request (only while its ticket is Under Repair):

1. **Pending**: the workshop technician asks for parts, from inside the ticket.
   The parts are **held** from now: free qty = qty in the store - qty asked (Pending) or
   approved (Approved) and not taken yet. Nobody can ask beyond the free qty.
2. **Approved** / **Rejected**: a System Manager decides (and may lower the quantities; a
   rejection frees the hold). A draft Stock Entry (Material Issue) is made for the technician.
3. **Taken**: the technician takes the parts by hand (no stock keeper) and confirms: the draft
   is submitted with the serials he scanned (for items with serials) and the parts leave the
   store. Their stock value is the ticket's parts cost.
4. **Returned**: what he did not use goes back in (Material Receipt at the same rate) and comes
   off the ticket's cost. Partly returned stays Taken.

Pending or Approved can be **Cancelled** only by the technician who asked (Maintenance Issues). The repair
cannot be finished while a request is Pending or Approved.

A guard on stock documents (``guard_*``) blocks any other way out of the store.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, now_datetime

from delivery_management.control import log
from delivery_management.customer_support import flow
from delivery_management.customer_support import tickets as t

REQ = "Spare Part Request"
PENDING, APPROVED, TAKEN, RETURNED, REJECTED, CANCELLED = (
	"Pending", "Approved", "Taken", "Returned", "Rejected", "Cancelled")
OPEN = (PENDING, APPROVED)
COSTED = (TAKEN, RETURNED)
APPROVER_ROLE = "System Manager"


# ---------------------------------------------------------------------------
# Store, people, quantities
# ---------------------------------------------------------------------------


def store_or_none() -> str | None:
	return frappe.get_cached_doc("Delivery Settings").get("spare_parts_warehouse") or None


def store() -> str:
	name = store_or_none()
	if not name:
		frappe.throw(_("Set the Spare Parts Store in Delivery Settings (Support tab) first."))
	return name


def is_approver(user: str | None = None) -> bool:
	return APPROVER_ROLE in frappe.get_roles(user or frappe.session.user)


def _require(condition: bool, message: str) -> None:
	if not condition:
		frappe.throw(message, frappe.PermissionError)


def _require_owner(req) -> None:
	"""The technician who asked answers for the parts; a System Manager may act for him."""
	_require(req.technician == frappe.session.user or is_approver(), _("This is not your request."))


def stock_of(items, warehouse: str | None = None, exclude: str | None = None, for_update: bool = False) -> dict:
	"""``{item_code: {"in_store", "held", "free"}}`` for the given items: two grouped queries,
	whatever the number of items. Held = asked (Pending) or approved (Approved), not taken yet.
	``exclude``: a request not to count as held."""
	items = sorted({i for i in items if i})
	if not items:
		return {}
	warehouse = warehouse or store()
	lock = " for update" if for_update else ""
	in_store = dict(frappe.db.sql(f"""select item_code, actual_qty from `tabBin`
		where warehouse = %s and item_code in %s{lock}""", (warehouse, items)))
	held = dict(frappe.db.sql("""select i.item_code,
			sum(case when r.status = %s then i.approved_qty else i.qty end) from `tabSpare Part Request Item` i
		join `tabSpare Part Request` r on r.name = i.parent
		where r.status in %s and r.warehouse = %s and r.name != %s and i.item_code in %s
		group by i.item_code""", (APPROVED, OPEN, warehouse, exclude or "", items)))
	return {i: {"in_store": flt(in_store.get(i)), "held": flt(held.get(i)),
		"free": flt(in_store.get(i)) - flt(held.get(i))} for i in items}


def _lines(items) -> list[dict]:
	"""Clean request lines: one per item, whole positive quantities, stock items only."""
	merged: dict[str, float] = {}
	for row in items or []:
		code = cstr(row.get("item_code")).strip()
		qty = flt(row.get("qty"))
		if not code:
			continue
		if qty <= 0 or qty != int(qty):
			frappe.throw(_("Quantity of {0} must be a whole number above 0.").format(code))
		merged[code] = merged.get(code, 0) + qty
	if not merged:
		frappe.throw(_("Add at least one part."))
	info = {r.name: r for r in frappe.get_all("Item", filters={"name": ["in", list(merged)]},
		fields=["name", "item_name", "is_stock_item", "disabled"])}
	for code in merged:
		row = info.get(code)
		if not row or row.disabled or not cint(row.is_stock_item):
			frappe.throw(_("{0} is not a stock item.").format(code))
	return [{"item_code": c, "item_name": info[c].item_name, "qty": q} for c, q in merged.items()]


def _check_free(lines, warehouse, exclude=None, for_update=False, field="qty") -> None:
	stock = stock_of([r["item_code"] if isinstance(r, dict) else r.item_code for r in lines], warehouse,
		exclude=exclude, for_update=for_update)
	for r in lines:
		code, qty = (r["item_code"], r[field]) if isinstance(r, dict) else (r.item_code, flt(r.get(field)))
		free = stock[code]["free"]
		if qty > free:
			frappe.throw(_("Only {0} of {1} free in {2} ({3} held for other repairs).").format(
				max(free, 0), code, warehouse, stock[code]["held"]), title=_("Not enough in the store"))


def _log_items(req, field: str, serial_field: str | None = None) -> list[dict]:
	return [{"item_code": r.item_code, "item_name": r.item_name, "qty": flt(r.get(field)),
		"serials": cstr(r.get(serial_field)).split() if serial_field else []}
		for r in req.items if flt(r.get(field))]


def _notify(users, subject: str, req_name: str) -> None:
	for user in {u for u in users if u and u not in ("Guest", frappe.session.user)}:
		frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": "Alert", "subject": subject,
			"document_type": REQ, "document_name": req_name, "from_user": frappe.session.user}
		).insert(ignore_permissions=True)


def _approvers() -> list[str]:
	return [u.user for u in t.users_with_role(APPROVER_ROLE)]


def _ticket_in_repair(name: str):
	ticket = t.get_ticket(name, for_update=True)
	if ticket.sm_step != t.STEP_REPAIR:
		frappe.throw(_("Spare parts are only for a ticket Under Repair (this one is {0}).").format(_(ticket.sm_step)))
	t.require_view(ticket)
	return ticket


def _get(name: str):
	return frappe.get_doc(REQ, name, for_update=True)


def _require_status(req, *statuses) -> None:
	if req.status not in statuses:
		frappe.throw(_("{0} is {1}: it was already handled. Refresh the page.").format(req.name, _(req.status)),
			title=_("Already done"))


def _save(req) -> None:
	req.flags.ignore_permissions = True
	req.save()


def update_ticket_cost(ticket_name: str) -> dict:
	"""Parts cost = what was taken minus what came back, over the ticket's requests."""
	parts = flt(frappe.db.sql("""select coalesce(sum(total_cost), 0) from `tabSpare Part Request`
		where ticket = %s and status in %s""", (ticket_name, COSTED))[0][0])
	repair = flt(frappe.db.get_value(t.ISSUE, ticket_name, "sm_repair_cost"))
	values = {"sm_parts_cost": parts, "sm_total_cost": parts + repair}
	frappe.db.set_value(t.ISSUE, ticket_name, values, update_modified=False)
	return values


def _total(req) -> None:
	for r in req.items:
		r.amount = (flt(r.taken_qty) - flt(r.returned_qty)) * flt(r.rate)
	req.total_cost = sum(flt(r.amount) for r in req.items)


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def request_parts(ticket_name: str, items, reason: str | None = None) -> str:
	"""The workshop technician asks; a System Manager is told."""
	flow._require_workshop()
	ticket = _ticket_in_repair(ticket_name)
	warehouse = store()
	lines = _lines(items)
	# Held from the request: the Bin rows are locked, two requests cannot ask the same last unit.
	_check_free(lines, warehouse, for_update=True)
	req = frappe.get_doc({
		"doctype": REQ, "ticket": ticket.name, "status": PENDING, "technician": frappe.session.user,
		"technician_name": frappe.utils.get_fullname(frappe.session.user), "requested_on": now_datetime(),
		"warehouse": warehouse, "reason": cstr(reason).strip() or None, "items": lines,
	}).insert(ignore_permissions=True)
	log.add("Parts requested", support_ticket=ticket.name, items=lines, note=req.reason, reference=(REQ, req.name))
	t.add_note(ticket, _("Spare parts requested ({0})").format(req.name), log.items_text(lines))
	_notify(_approvers(), _("{0} asks for spare parts for {1}").format(req.technician_name, ticket.name), req.name)
	return req.name


def approve(name: str, qtys: dict | None = None) -> str:
	"""Hold the parts and make the draft stock issue. ``qtys``: lower quantities, by item."""
	from delivery_management.api.drivers import _with_full_rights

	_require(is_approver(), _("Only a System Manager approves spare parts."))
	req = _get(name)
	_require_status(req, PENDING)
	ticket = _ticket_in_repair(req.ticket)
	qtys = qtys or {}
	for r in req.items:
		qty = flt(qtys.get(r.item_code, r.qty))
		if qty < 0 or qty > flt(r.qty) or qty != int(qty):
			frappe.throw(_("Approve between 0 and {0} of {1}.").format(r.qty, r.item_code))
		r.approved_qty = qty
	if not any(flt(r.approved_qty) for r in req.items):
		frappe.throw(_("Nothing to approve: reject the request instead."))
	# The Bin rows are locked: two approvals at once cannot hold the same last unit.
	_check_free(req.items, req.warehouse, exclude=req.name, for_update=True, field="approved_qty")
	account = frappe.get_cached_doc("Delivery Settings").get("spare_parts_expense_account")
	se = frappe.get_doc({
		"doctype": "Stock Entry", "stock_entry_type": "Material Issue", "purpose": "Material Issue",
		"company": frappe.db.get_value("Warehouse", req.warehouse, "company"), "sm_spare_request": req.name,
		"remarks": _("Spare parts for the repair of {0} ({1})").format(req.ticket, req.name),
		"items": [{"item_code": r.item_code, "qty": r.approved_qty, "s_warehouse": req.warehouse,
			**({"expense_account": account} if account else {})} for r in req.items if flt(r.approved_qty)],
	})
	se.flags.dm_spare_parts = True
	with _with_full_rights():
		se.flags.ignore_permissions = True
		se.insert()
	req.update({"status": APPROVED, "issue_entry": se.name, "approver": frappe.session.user, "decided_on": now_datetime()})
	_save(req)
	log.add("Parts approved", support_ticket=req.ticket, items=_log_items(req, "approved_qty"), reference=(REQ, req.name))
	t.add_note(ticket, _("Spare parts approved ({0})").format(req.name), _("Held in {0}: take them and confirm.").format(req.warehouse))
	_notify([req.technician], _("Your spare parts for {0} are approved: take them and confirm").format(req.ticket), req.name)
	return se.name


def reject(name: str, reason: str) -> None:
	_require(is_approver(), _("Only a System Manager rejects spare parts."))
	reason = cstr(reason).strip()
	if not reason:
		frappe.throw(_("Write why it is rejected."))
	req = _get(name)
	_require_status(req, PENDING)
	req.update({"status": REJECTED, "approver": frappe.session.user, "decided_on": now_datetime(), "reject_reason": reason})
	_save(req)
	log.add("Parts rejected", support_ticket=req.ticket, items=_log_items(req, "qty"), note=reason, reference=(REQ, req.name))
	t.add_note(t.get_ticket(req.ticket), _("Spare parts rejected ({0})").format(req.name), reason)
	_notify([req.technician], _("Your spare parts for {0} are rejected: {1}").format(req.ticket, reason), req.name)


def cancel(name: str, reason: str | None = None) -> None:
	"""The technician does not need them any more: the hold ends, the draft is deleted."""
	from delivery_management.api.drivers import _with_full_rights

	req = _get(name)
	_require(req.technician == frappe.session.user, _("Only {0}, who asked, can cancel this request.").format(
		req.technician_name or req.technician))
	_require_status(req, *OPEN)
	entry = req.issue_entry
	req.update({"status": CANCELLED, "issue_entry": None})
	_save(req)
	if entry and frappe.db.get_value("Stock Entry", entry, "docstatus") == 0:
		with _with_full_rights():
			frappe.delete_doc("Stock Entry", entry, ignore_permissions=True, force=True)
	log.add("Parts request cancelled", support_ticket=req.ticket, items=_log_items(req, "qty"),
		note=cstr(reason).strip() or None, reference=(REQ, req.name))
	t.add_note(t.get_ticket(req.ticket), _("Spare parts request cancelled ({0})").format(req.name), reason)


def _serials(value) -> list[str]:
	if isinstance(value, str):
		value = value.replace(",", "\n").split()
	return [cstr(s).strip() for s in value or [] if cstr(s).strip()]


def _check_serials(item_code: str, serials: list[str], qty: float, warehouse: str | None, what: str) -> None:
	"""``qty`` distinct serials of the item, in ``warehouse`` (None = in no store)."""
	if len(serials) != qty or len(set(serials)) != len(serials):
		frappe.throw(_("Scan {0} different serials of {1} ({2} scanned).").format(int(qty), item_code, len(set(serials))),
			title=_("Serial required"))
	found = {r.name: r for r in frappe.get_all("Serial No", filters={"name": ["in", serials]},
		fields=["name", "item_code", "warehouse"])}
	for s in serials:
		row = found.get(s)
		if not row or row.item_code != item_code:
			frappe.throw(_("{0} is not a serial of {1}.").format(s, item_code), title=_("Wrong serial"))
		if (row.warehouse or None) != warehouse:
			frappe.throw(_("Serial {0} is not {1}.").format(s, what), title=_("Wrong serial"))


def take(name: str, serials: dict | None = None) -> str:
	"""The technician took the parts by hand: the draft is submitted, the parts leave the
	store, their stock value goes on the ticket."""
	from delivery_management.api.drivers import _with_full_rights

	req = _get(name)
	_require_owner(req)
	_require_status(req, APPROVED)
	ticket = _ticket_in_repair(req.ticket)
	serials = serials or {}
	has = set(frappe.get_all("Item", filters={"name": ["in", [r.item_code for r in req.items]], "has_serial_no": 1},
		pluck="name"))
	chosen = {}
	for r in req.items:
		if r.item_code in has and flt(r.approved_qty):
			chosen[r.item_code] = _serials(serials.get(r.item_code))
			_check_serials(r.item_code, chosen[r.item_code], flt(r.approved_qty), req.warehouse,
				_("in {0}").format(req.warehouse))
	se = frappe.get_doc("Stock Entry", req.issue_entry)
	if se.docstatus != 0:
		frappe.throw(_("The stock issue {0} is not a draft any more.").format(se.name))
	for row in se.items:  # what was approved, whatever was edited on the draft
		r = next((x for x in req.items if x.item_code == row.item_code), None)
		if not r or flt(row.qty) != flt(r.approved_qty) or row.s_warehouse != req.warehouse:
			frappe.throw(_("The stock issue {0} does not match the approval.").format(se.name))
		if row.item_code in chosen:
			row.use_serial_batch_fields = 1
			row.serial_no = "\n".join(chosen[row.item_code])
	se.flags.dm_spare_parts = True
	with _with_full_rights():
		se.flags.ignore_permissions = True
		se.save()
		se.submit()
	rates = {row.item_code: flt(row.valuation_rate) for row in se.items}
	for r in req.items:
		r.taken_qty = r.approved_qty
		r.rate = rates.get(r.item_code, 0)
		r.serial_no = "\n".join(chosen.get(r.item_code, [])) or None
	_total(req)
	req.update({"status": TAKEN, "taken_on": now_datetime()})
	_save(req)
	cost = update_ticket_cost(req.ticket)
	log.add("Parts taken", support_ticket=req.ticket, items=_log_items(req, "taken_qty", "serial_no"),
		amount=req.total_cost, reference=("Stock Entry", se.name))
	t.add_note(ticket, _("Spare parts taken ({0})").format(req.name),
		_("Cost {0}. Ticket parts cost now {1}.").format(req.total_cost, cost["sm_parts_cost"]))
	return se.name


def return_parts(name: str, rows) -> str:
	"""Unused parts back into the store, at the rate they left it: the ticket cost goes down
	by the same value. ``rows``: ``[{item_code, qty, serials}]``."""
	from delivery_management.api.drivers import _with_full_rights

	req = _get(name)
	_require_owner(req)
	_require_status(req, TAKEN)
	ticket = _ticket_in_repair(req.ticket)
	by_item = {r.item_code: r for r in req.items}
	has = {i for i in frappe.get_all("Item", filters={"name": ["in", list(by_item)], "has_serial_no": 1}, pluck="name")}
	back = []
	for row in rows or []:
		code, qty = cstr(row.get("item_code")), flt(row.get("qty"))
		if not qty:
			continue
		r = by_item.get(code)
		if not r:
			frappe.throw(_("{0} was not taken on {1}.").format(code, req.name))
		left = flt(r.taken_qty) - flt(r.returned_qty)
		if qty < 0 or qty > left or qty != int(qty):
			frappe.throw(_("Return between 1 and {0} of {1}.").format(left, code))
		serials = _serials(row.get("serials")) if code in has else []
		if code in has:
			_check_serials(code, serials, qty, None, _("out of the stores"))
			outside = set(cstr(r.serial_no).split()) - set(cstr(r.returned_serials).split())
			wrong = [s for s in serials if s not in outside]
			if wrong:
				frappe.throw(_("Serial {0} was not taken on {1}.").format(wrong[0], req.name), title=_("Wrong serial"))
		back.append((r, qty, serials))
	if not back:
		frappe.throw(_("Choose what you return."))
	account = frappe.get_cached_doc("Delivery Settings").get("spare_parts_expense_account")
	se = frappe.get_doc({
		"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "purpose": "Material Receipt",
		"company": frappe.db.get_value("Warehouse", req.warehouse, "company"), "sm_spare_request": req.name,
		"remarks": _("Unused spare parts back from the repair of {0} ({1})").format(req.ticket, req.name),
		"items": [{"item_code": r.item_code, "qty": qty, "t_warehouse": req.warehouse, "basic_rate": r.rate,
			"set_basic_rate_manually": 1, "allow_zero_valuation_rate": 0 if flt(r.rate) else 1,
			"use_serial_batch_fields": 1 if serials else 0, "serial_no": "\n".join(serials),
			**({"expense_account": account} if account else {})} for r, qty, serials in back],
	})
	se.flags.dm_spare_parts = True
	with _with_full_rights():
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
	for r, qty, serials in back:
		r.returned_qty = flt(r.returned_qty) + qty
		if serials:
			r.returned_serials = "\n".join(cstr(r.returned_serials).split() + serials)
	_total(req)
	if all(flt(r.returned_qty) >= flt(r.taken_qty) for r in req.items):
		req.status = RETURNED
	_save(req)
	update_ticket_cost(req.ticket)
	items = [{"item_code": r.item_code, "item_name": r.item_name, "qty": qty, "serials": s} for r, qty, s in back]
	value = sum(qty * flt(r.rate) for r, qty, _s in back)
	log.add("Parts returned", support_ticket=req.ticket, items=items, amount=value, reference=("Stock Entry", se.name))
	t.add_note(ticket, _("Spare parts returned ({0})").format(req.name), log.items_text(items))
	return se.name


def set_repair_cost(ticket_name: str, amount) -> dict:
	flow._require_workshop()
	ticket = _ticket_in_repair(ticket_name)
	amount = flt(amount)
	if amount < 0:
		frappe.throw(_("The repair cost cannot be negative."))
	frappe.db.set_value(t.ISSUE, ticket.name, "sm_repair_cost", amount, update_modified=False)
	return update_ticket_cost(ticket.name)


def require_none_open(ticket_name: str) -> None:
	"""The repair is not finished while parts are asked for or held."""
	row = frappe.db.get_value(REQ, {"ticket": ticket_name, "status": ["in", OPEN]}, ["name", "status"], as_dict=True)
	if row:
		frappe.throw(_("Spare part request {0} is still {1}. Take the parts or cancel the request first.").format(
			row.name, _(row.status)), title=_("Spare parts open"))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def ticket_parts(ticket, view: str | None = None) -> dict:
	"""The ticket's requests and costs, with what this person can do on each."""
	user = frappe.session.user
	reqs = frappe.get_all(REQ, filters={"ticket": ticket.name}, order_by="creation desc",
		fields=["name", "status", "technician", "technician_name", "requested_on", "reason", "reject_reason",
			"approver", "decided_on", "total_cost", "issue_entry"])
	rows = frappe.get_all("Spare Part Request Item", filters={"parent": ["in", [r.name for r in reqs]],
		"parenttype": REQ}, fields=["parent", "item_code", "item_name", "qty", "approved_qty", "taken_qty",
		"returned_qty", "rate", "amount", "serial_no", "returned_serials"], order_by="idx") if reqs else []
	serial_items = set(frappe.get_all("Item", filters={"name": ["in", list({r.item_code for r in rows})],
		"has_serial_no": 1}, pluck="name")) if rows else set()
	by_parent: dict[str, list] = {}
	for r in rows:
		r.has_serial = r.item_code in serial_items
		by_parent.setdefault(r.parent, []).append(r)
	workshop = view == "maintenance" and (t.is_admin(user) or t.has_role(t.ROLE_MAINT, user))
	repair = ticket.sm_step == t.STEP_REPAIR
	approver = is_approver(user)
	for r in reqs:
		r["items"] = by_parent.get(r.name, [])
		mine = r.technician == user or approver
		r["can"] = {
			"take": repair and workshop and mine and r.status == APPROVED,
			"cancel": repair and workshop and r.technician == user and r.status in OPEN,
			"return": repair and workshop and mine and r.status == TAKEN,
			"decide": repair and approver and r.status == PENDING,
		}
	return {
		"requests": reqs,
		"parts_cost": flt(ticket.get("sm_parts_cost")),
		"repair_cost": flt(ticket.get("sm_repair_cost")),
		"total_cost": flt(ticket.get("sm_total_cost")),
		"can_request": repair and workshop and bool(store_or_none()),
		"can_set_cost": repair and workshop,
		"open": any(r.status in OPEN for r in reqs),
	}


def search(txt: str | None = None, limit: int = 20) -> list[dict]:
	"""Parts in the store by code, name or barcode, with free qty (one query + one grouped)."""
	warehouse = store()
	txt = cstr(txt).strip()
	like = f"%{txt}%"
	rows = frappe.db.sql("""select b.item_code, i.item_name, i.has_serial_no, i.stock_uom, b.actual_qty
		from `tabBin` b join `tabItem` i on i.name = b.item_code
		where b.warehouse = %(wh)s and b.actual_qty > 0 and (%(txt)s = '' or b.item_code like %(like)s
			or i.item_name like %(like)s or exists (select 1 from `tabItem Barcode` ib
				where ib.parent = i.name and ib.barcode = %(txt)s))
		order by (b.item_code = %(txt)s) desc, i.item_name limit %(limit)s""",
		{"wh": warehouse, "txt": txt, "like": like, "limit": cint(limit) or 20}, as_dict=True)
	stock = stock_of([r.item_code for r in rows], warehouse)
	for r in rows:
		r.update(stock[r.item_code])
	return rows


# ---------------------------------------------------------------------------
# Guards: no other way out of the Spare Parts Store
# ---------------------------------------------------------------------------


def guard_stock_entry(doc, method=None) -> None:
	"""Parts leave the store only by a request's own stock issue (flagged in code, never from
	the desk), so the held quantities are always kept."""
	warehouse = store_or_none()
	if not warehouse or doc.flags.dm_spare_parts:
		return
	if any(row.s_warehouse == warehouse for row in doc.items):
		frappe.throw(_("Parts leave {0} only through a Spare Part Request approved by a System Manager.").format(warehouse),
			title=_("Spare Parts Store"))


def guard_cancel(doc, method=None) -> None:
	if doc.get("sm_spare_request") and not doc.flags.dm_spare_parts:
		frappe.throw(_("This stock entry belongs to {0}: it cannot be cancelled.").format(doc.sm_spare_request),
			title=_("Spare Parts Store"))


def guard_selling(doc, method=None) -> None:
	"""Spare parts are not sold from the workshop store."""
	warehouse = store_or_none()
	if not warehouse or (doc.doctype != "Delivery Note" and not cint(doc.get("update_stock"))):
		return
	if any(row.get("warehouse") == warehouse for row in doc.items):
		frappe.throw(_("{0} is the workshop's Spare Parts Store: nothing is sold from it.").format(warehouse),
			title=_("Spare Parts Store"))
