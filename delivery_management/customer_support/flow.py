"""The support journey after the ticket is opened: the two trips, the workshop and the stock.

* A **trip** (the pickup, then the return) books a delivery slot (date, area, time) in the
  same TV slots as the orders, and is given a TV Driver on the Driver Assignment page. The
  pickup slot stays booked after the pickup: the return books one more slot.
* The customer's item is **not our stock**: it enters the stores at zero value (a
  serialised item; one without serial is only followed on the ticket). It goes
  into the transit store when the driver fetches it, to the workshop store when the
  workshop receives it, back to the transit store when the workshop sends it out, and out
  of the stores when the customer gets it back.
* A serialised item never moves without its serial: the serial is typed / scanned and must
  be the ticket's one, of this item, and in the store it leaves (or in no store, coming in).
* Every step is logged in the Order Log (Order Control page).

Each step locks the ticket and checks its current step first, so a double click or two
people at once cannot run a step twice.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_time, getdate, now_datetime

from delivery_management.control import log
from delivery_management.customer_support import tickets as t
from delivery_management.slots import engine
from delivery_management.slots.settings import get_settings

ISSUE = t.ISSUE
# Files attached to the ticket: the name says what the file is (seen in the Issue's attachments).
REPAIR_PREFIX = "Workshop repair proof"  # the workshop's proof that the item is repaired
CUSTOMER_PREFIX = "Return hand-over photo-video"  # photo / video when the customer gets the item back
SIGNATURE_PREFIX = "Customer signature at return"  # the customer's signature at the hand-over
FILE_KINDS = {"repair": REPAIR_PREFIX, "customer": CUSTOMER_PREFIX, "signature": SIGNATURE_PREFIX}
# Slot bookings of a ticket, told apart by their remarks.
PICKUP_NOTE = "Support pickup"
RETURN_NOTE = "Support return"
MEDIA_EXTENSIONS = ("jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "mp4", "mov", "webm", "3gp", "mkv")
MAX_FILE_BYTES = 25 * 1024 * 1024


def _lock(name: str):
	ticket = t.get_ticket(name, for_update=True)
	t.require_view(ticket)
	return ticket


def _set_step(ticket, step: str) -> None:
	ticket.sm_step = step
	ticket.sm_stage = t.STAGE_OF_STEP.get(step, t.STAGE_MAINT if step != t.STEP_CLOSED else ticket.sm_stage)


def _require(condition: bool, message: str) -> None:
	if not condition:
		frappe.throw(message, frappe.PermissionError)


def _is_trip_driver(ticket, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return bool(ticket.sm_driver) and (ticket.sm_driver == user or t.is_admin(user))


def _require_driver(ticket) -> None:
	"""Nobody goes to the customer without a driver: the office assigns one first."""
	if not ticket.sm_driver:
		frappe.throw(_("No driver is assigned to this trip. The delivery office assigns one in Driver Assignment."),
			title=_("No driver"))
	_require(_is_trip_driver(ticket), _("This trip is not assigned to you."))


def _require_workshop() -> None:
	_require(t.is_admin() or t.has_role(t.ROLE_MAINT), _("Only the workshop (Maintenance Technician) can do this."))


def _item_line(ticket, serial=None) -> list[dict]:
	return [{"item_code": ticket.sm_item_code, "item_name": frappe.db.get_value("Item", ticket.sm_item_code, "item_name"),
		"qty": 1, "serials": [serial] if serial else []}]


# ---------------------------------------------------------------------------
# Trips: slot and TV Driver
# ---------------------------------------------------------------------------


def slot_options(area: str, date) -> list[dict]:
	return [{"time": str(s["delivery_time"])[:5], "delivery_time": str(s["delivery_time"]),
		"available": bool(s["available"]), "remaining": s.get("remaining")}
		for s in engine.get_delivery_slots(area, getdate(date))]


def book_trip(name: str, area: str, delivery_date, delivery_time, note: str | None = None) -> None:
	"""Customer service books the pickup (from Customer Service) or the return (from Ready
	for Return) in a delivery slot, as the POS books an order. The pickup slot stays booked
	when the return is booked: both trips use a car."""
	_require(t.is_admin() or t.has_role(t.ROLE_CS), _("Only customer service can book a visit."))
	ticket = _lock(name)
	t.require_step(ticket, t.STEP_CS, t.STEP_READY)
	if not (area and delivery_date and delivery_time):
		frappe.throw(_("Choose the date, the area and the time."))
	note = cstr(note).strip()
	if not note:
		frappe.throw(_("Write your note: what you did and why."))
	pickup = ticket.sm_step == t.STEP_CS
	# A slot a driver went to stays booked (it used a car); every trip books its own.
	engine.reserve(ISSUE, ticket.name, area=area, delivery_date=getdate(delivery_date), delivery_time=delivery_time,
		customer=ticket.customer, contact_phone=ticket.sm_customer_phone, add_to_existing=True,
		remarks=f"{PICKUP_NOTE if pickup else RETURN_NOTE} {ticket.name}")
	ticket.sm_area = area
	ticket.sm_trip_date = getdate(delivery_date)
	ticket.sm_trip_time = get_time(delivery_time)
	ticket.sm_driver = None
	ticket.sm_not_home_count = 0
	_set_step(ticket, t.STEP_PICKUP if pickup else t.STEP_RETURN)
	t._save(ticket)
	when = f"{area} · {getdate(delivery_date)} {str(delivery_time)[:5]}"
	log.add("Visit booked" if pickup else "Return booked", support_ticket=ticket.name, note=f"{when} · {note}")
	t.add_note(ticket, (_("Assigned to tech: {0}") if pickup else _("Return booked: {0}")).format(when), note)


def _busy_drivers(ticket) -> set[str]:
	"""TV Drivers with a TV order or another ticket trip that day within the driver gap."""
	from delivery_management.drivers import assignment as asg

	return asg.busy_tv_drivers(ticket.sm_trip_date, ticket.sm_trip_time, asg.conflict_window_hours(),
		exclude_ticket=ticket.name)


def driver_options(name: str) -> dict:
	from delivery_management.drivers import assignment as asg

	ticket = t.get_ticket(name)
	busy = _busy_drivers(ticket)
	return {
		"ticket": ticket.name, "delivery_date": ticket.sm_trip_date,
		"delivery_time": ticket.sm_trip_time and str(ticket.sm_trip_time), "window_hours": asg.conflict_window_hours(),
		"current_driver": ticket.sm_driver,
		"drivers": [{**d, "available": d.user not in busy, "label": "" if d.user not in busy else _("unavailable")}
			for d in asg.drivers_for(asg.TYPE_NON_AC)],
	}


def assign_driver(name: str, driver: str | None) -> None:
	"""The delivery office gives the trip a TV Driver (or takes him off)."""
	_require(t.is_office(), _("Only a System Manager or Delivery Manager can do this."))
	ticket = t.get_ticket(name, for_update=True)
	t.require_step(ticket, *t.TRIP_STEPS)
	before = ticket.sm_driver
	if not driver:
		if ticket.sm_step == t.STEP_OUT:
			frappe.throw(_("The item is out with {0}: change the driver instead.").format(before))
		ticket.sm_driver = None
		t._save(ticket)
		if before:
			log.add("Driver removed", support_ticket=ticket.name, driver=before)
		return
	if not frappe.db.get_value("User", driver, "enabled") or not t.has_role(t.ROLE_TECH, driver):
		frappe.throw(_("{0} is not a TV Driver.").format(driver))
	# Two people assigning the same driver at once: the second one waits and re-checks.
	frappe.db.sql("select name from `tabUser` where name = %s for update", driver)
	if driver != before and driver in _busy_drivers(ticket):
		frappe.throw(_("This driver already has another order too close to this time."))
	ticket.sm_driver = driver
	if ticket.sm_step == t.STEP_PICKUP:
		ticket.sm_technician = driver
	t._save(ticket)
	if driver != before:
		log.add("Ticket driver changed" if before else "Ticket driver assigned", support_ticket=ticket.name,
			driver=driver, note=_("Was {0}").format(frappe.utils.get_fullname(before)) if before else None)


def customer_not_home(name: str, note: str | None = None) -> None:
	"""The customer was not there. At a pickup customer service books again. On a return the
	driver brings the item back: the stock keeper scans it into the Customer Items Store
	(``receive_back``), then customer service books again."""
	ticket = _lock(name)
	t.require_step(ticket, t.STEP_PICKUP, t.STEP_OUT)
	_require_driver(ticket)
	note = cstr(note).strip()
	if not note:
		frappe.throw(_("Write your visit note first."))
	driver = ticket.sm_driver
	pickup = ticket.sm_step == t.STEP_PICKUP
	# The slot stays booked: the driver went.
	ticket.sm_not_home_count = cint(ticket.sm_not_home_count) + 1
	ticket.sm_trip_date = None
	ticket.sm_trip_time = None
	if pickup:
		ticket.sm_driver = None
		_set_step(ticket, t.STEP_CS)
	else:  # the item is still in the transit store with the driver until the stock keeper takes it
		_set_step(ticket, t.STEP_BACK)
	t._save(ticket)
	log.add("Customer not home", support_ticket=ticket.name, driver=driver, note=note)
	t.add_note(ticket, _("Customer not home ({0})").format(_("pickup") if pickup else _("return")),
		note if pickup else _("{0}. The driver brings the item back to the Customer Items Store.").format(note))


def receive_back(name: str, serial: str | None = None) -> str | None:
	"""The stock keeper takes the item the driver brought back: transit store -> main store,
	serial scanned. Customer service then books a new return slot."""
	ticket = _lock(name)
	t.require_step(ticket, t.STEP_BACK)
	if has_serial(ticket.sm_item_code) and not cstr(serial).strip():
		frappe.throw(_("Scan the serial of the item."), title=_("Serial required"))
	serial = _serial_to_move(ticket, serial, ticket.sm_warehouse)
	entry = _move(ticket, "Material Transfer", ticket.sm_warehouse, main_store(), serial)
	driver = ticket.sm_driver
	ticket.sm_driver = None
	_set_step(ticket, t.STEP_READY)
	t._save(ticket)
	log.add("Received back in store", support_ticket=ticket.name, driver=driver, items=_item_line(ticket, serial),
		reference=("Stock Entry", entry) if entry else None)
	t.add_note(ticket, _("Received back in {0}").format(ticket.sm_warehouse),
		_("Customer service books a new return slot."))
	return entry


# ---------------------------------------------------------------------------
# Stock: the customer's item, at zero value, always with its serial
# ---------------------------------------------------------------------------


def _store(field: str, label: str) -> str:
	store = frappe.db.get_single_value("Delivery Settings", field)
	if not store:
		frappe.throw(_("Set the {0} in Delivery Settings (Support tab) first.").format(_(label)))
	return store


def _transit() -> str:
	"""Customer items with a driver: their own transit store, never mixed with our goods."""
	return _store("support_transit_warehouse", "Customer Items Transit Store")


def _workshop() -> str:
	store = get_settings().support_workshop_warehouse
	if not store:
		frappe.throw(_("Set the Workshop Store in Delivery Settings (Support tab) first."))
	return store


def main_store() -> str:
	"""Where the stock keeper keeps a customer item brought back (customer not home)."""
	return _store("support_store_warehouse", "Customer Items Store")


def in_workshop(ticket) -> bool:
	return bool(ticket.sm_warehouse) and ticket.sm_warehouse == get_settings().support_workshop_warehouse


def require_sender(ticket) -> None:
	"""Who hands the item to the return driver: the workshop from its store, the stock keeper
	from the Customer Items Store."""
	if in_workshop(ticket):
		_require_workshop()
	elif not is_stock_keeper():
		frappe.throw(_("Only the stock keeper sends items out of {0}.").format(ticket.sm_warehouse),
			frappe.PermissionError)


STOCK_ROLES = ("System Manager", "Delivery Manager", "Stock Manager", "Stock User")


def is_stock_keeper(user: str | None = None) -> bool:
	return t.is_admin(user) or bool(set(STOCK_ROLES) & set(frappe.get_roles(user)))


def has_serial(item_code: str) -> bool:
	return bool(cint(frappe.db.get_value("Item", item_code, "has_serial_no")))


def _serial_to_move(ticket, serial: str | None, source: str | None, can_change: bool = False) -> str | None:
	"""The serial of this move, checked; None for an item without serials.

	* It is required for a serialised item, and must be the ticket's serial once it has one.
	* Coming in (``source`` None) it must not be in any store; a new serial is created.
	* Moving / going out it must be in ``source``.
	"""
	item_code = ticket.sm_item_code
	if not cint(frappe.db.get_value("Item", item_code, "is_stock_item")):
		frappe.throw(_("{0} is not a stock item: it cannot enter the stores.").format(item_code))
	if not has_serial(item_code):
		return None
	serial = cstr(serial).strip() or cstr(ticket.sm_serial_no).strip()
	if not serial:
		frappe.throw(_("{0} has serial numbers: scan or type the serial of the item.").format(item_code),
			title=_("Serial required"))
	if ticket.sm_serial_no and serial != ticket.sm_serial_no and not can_change:
		frappe.throw(_("Serial {0} is not the serial of this ticket ({1}).").format(serial, ticket.sm_serial_no),
			title=_("Wrong serial"))
	row = frappe.db.get_value("Serial No", serial, ["item_code", "warehouse"], as_dict=True)
	if row and row.item_code != item_code:
		frappe.throw(_("Serial {0} is a serial of {1}, not of {2}.").format(serial, row.item_code, item_code))
	if source is None:
		if row and row.warehouse:
			frappe.throw(_("Serial {0} is already in the store {1}.").format(serial, row.warehouse))
	elif not row or row.warehouse != source:
		frappe.throw(_("Serial {0} is not in {1}.").format(serial, source))
	return serial


def _move(ticket, purpose: str, source: str | None, target: str | None, serial: str | None) -> str | None:
	"""One unit of the ticket's item, at zero value, as a submitted Stock Entry: every unit
	shows in the stock reports. The customer stores hold only customer items, so a zero
	value never changes the value of our own stock."""
	from delivery_management.api.drivers import _with_full_rights

	company = frappe.db.get_value("Warehouse", source or target, "company")
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": purpose,
		"purpose": purpose,
		"company": company,
		"remarks": _("Support ticket {0}: customer's item, not our stock").format(ticket.name),
		"items": [{
			"item_code": ticket.sm_item_code,
			"qty": 1,
			"s_warehouse": source,
			"t_warehouse": target,
			"basic_rate": 0,
			"allow_zero_valuation_rate": 1,
			"use_serial_batch_fields": 1 if serial else 0,
			"serial_no": serial or "",
		}],
	})
	# The person's access to this ticket and step was checked; ERPNext re-checks desk
	# rights on the Stock Entry, which technicians do not have.
	with _with_full_rights():
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
	ticket.sm_warehouse = target
	return se.name


def fetch_item(name: str, serial: str | None = None, report: str | None = None,
		completion_status: str = "Partially Completed") -> str:
	"""The driver takes the item from the customer: it enters the transit store. His visit
	report of this visit is saved with it."""
	ticket = _lock(name)
	t.require_step(ticket, t.STEP_PICKUP)
	_require_driver(ticket)
	if not cstr(report).strip():
		frappe.throw(_("Write your visit report first, then take the item."), title=_("Report required"))
	# At the customer the driver may correct the serial typed by customer service.
	serial = _serial_to_move(ticket, serial, None, can_change=True)
	t.log_technician_visit(ticket, completion_status, report)
	entry = _move(ticket, "Material Receipt", None, _transit(), serial)
	if serial and serial != ticket.sm_serial_no:
		ticket.sm_serial_no = serial
		ticket.sm_warranty_expiry = ticket.sm_warranty_expiry or t.warranty_expiry(serial)
	driver = ticket.sm_driver
	ticket.sm_driver = None
	_set_step(ticket, t.STEP_TO_WORKSHOP)
	t._save(ticket)
	log.add("Item picked up", support_ticket=ticket.name, driver=driver, items=_item_line(ticket, serial),
		reference=("Stock Entry", entry) if entry else None)
	t.add_note(ticket, _("Item taken to Goods in Transit"), serial and _("Serial {0}").format(serial))
	return entry


IN_STORE_STEPS = (t.STEP_TO_WORKSHOP, t.STEP_REPAIR, t.STEP_READY, t.STEP_RETURN, t.STEP_OUT, t.STEP_BACK)


def can_resolve(ticket, view: str, user: str | None = None) -> bool:
	"""Resolve is on the three Support pages, for the person who works the ticket there."""
	user = user or frappe.session.user
	if t.is_resolved(ticket) or (ticket.get("sm_step") or t.STEP_CS) == t.STEP_CLOSED:
		return False
	if view == "cs":  # only before the ticket goes to a driver: then it is out of their hands
		return (ticket.get("sm_step") or t.STEP_CS) == t.STEP_CS and (
			t.is_admin(user) or (t.has_role(t.ROLE_CS, user) and ticket.sm_cs_user == user))
	if view == "technician":
		return (ticket.sm_step == t.STEP_PICKUP and bool(ticket.sm_driver)
			and (ticket.sm_driver == user or t.is_admin(user)))
	if view == "maintenance":  # in the workshop it must be repaired: no resolve there
		return False
	return False


def resolve_ticket(name: str, view: str, note: str, serial: str | None = None) -> str | None:
	"""The ticket ends at any step. Booked slots are freed. If the customer's item is in our
	stores, it leaves them (with its serial), as it is given back."""
	ticket = _lock(name)
	_require(can_resolve(ticket, view), _("You cannot resolve this ticket here."))
	note = cstr(note).strip()
	if not note:
		frappe.throw(_("Write how it was resolved."))
	entry = item = None
	if view == "technician":  # the driver's note is his visit report
		t.log_technician_visit(ticket, "Fully Completed", note)
	if ticket.sm_step in IN_STORE_STEPS and ticket.sm_warehouse:
		serial = _serial_to_move(ticket, serial, ticket.sm_warehouse)
		item = _item_line(ticket, serial)
		entry = _move(ticket, "Material Issue", ticket.sm_warehouse, None, serial)
	event = "Resolved at the customer" if ticket.sm_step == t.STEP_PICKUP else "Ticket resolved"
	driver = ticket.sm_driver
	_close(ticket)
	log.add(event, support_ticket=ticket.name, driver=driver, note=note, items=item,
		reference=("Stock Entry", entry) if entry else None)
	t.add_note(ticket, _("Resolved"), note)
	return entry


def receive_in_workshop(name: str, serial: str | None = None) -> str:
	"""The workshop takes the item from the driver: transit store -> workshop store. The
	serial is scanned again: from here the workshop answers for it."""
	_require_workshop()
	ticket = _lock(name)
	t.require_step(ticket, t.STEP_TO_WORKSHOP)
	serial = _serial_to_move(ticket, serial, ticket.sm_warehouse)
	entry = _move(ticket, "Material Transfer", ticket.sm_warehouse, _workshop(), serial)
	_set_step(ticket, t.STEP_REPAIR)
	t._save(ticket)
	log.add("Received in workshop", support_ticket=ticket.name, items=_item_line(ticket, serial),
		reference=("Stock Entry", entry) if entry else None)
	t.add_note(ticket, _("Received in the workshop"))
	return entry


def files_of(name: str, prefix: str) -> list[str]:
	return frappe.get_all("File", filters={"attached_to_doctype": ISSUE, "attached_to_name": name,
		"file_name": ["like", f"{prefix}%"]}, pluck="file_name")


def _has_media(name: str, prefix: str) -> bool:
	return any(f.rsplit(".", 1)[-1].lower() in MEDIA_EXTENSIONS for f in files_of(name, prefix))


def finish_repair(name: str, result: str, report: str) -> None:
	"""The workshop is done: repaired (or not repairable), with its report and a photo /
	video proving it. Customer service then books the return."""
	_require_workshop()
	ticket = _lock(name)
	t.require_step(ticket, t.STEP_REPAIR)
	if result not in ("Repaired", "Not repairable"):
		frappe.throw(_("Choose Repaired or Not repairable."))
	report = cstr(report).strip()
	if not report:
		frappe.throw(_("A workshop report is required."))
	from delivery_management.customer_support import spare_parts

	spare_parts.require_none_open(ticket.name)
	if not _has_media(ticket.name, REPAIR_PREFIX):
		frappe.throw(_("Upload a photo or video that proves the repair first."), title=_("Proof required"))
	ticket.sm_repair_result = result
	ticket.sm_maint_report = report
	ticket.sm_maint_report_by = frappe.session.user
	_set_step(ticket, t.STEP_READY)
	t._save(ticket)
	log.add(result, support_ticket=ticket.name, note=report)
	t.add_note(ticket, _("Workshop: {0}").format(_(result)), report)


def send_out(name: str, serial: str | None = None) -> str:
	"""The driver of the return came: the item leaves its store (the workshop's, or the main
	store after a customer was not home) into the transit store, serial scanned."""
	ticket = _lock(name)
	t.require_step(ticket, t.STEP_RETURN)
	require_sender(ticket)
	if not ticket.sm_driver:
		frappe.throw(_("No driver is assigned to the return trip. The delivery office assigns one in Driver Assignment first."))
	source = ticket.sm_warehouse
	serial = _serial_to_move(ticket, serial, source)
	entry = _move(ticket, "Material Transfer", source, _transit(), serial)
	_set_step(ticket, t.STEP_OUT)
	t._save(ticket)
	log.add("Sent out from workshop" if source == get_settings().support_workshop_warehouse else "Sent out from store",
		support_ticket=ticket.name, driver=ticket.sm_driver,
		items=_item_line(ticket, serial), reference=("Stock Entry", entry) if entry else None)
	t.add_note(ticket, _("Sent out from {0} to {1}").format(source, frappe.utils.get_fullname(ticket.sm_driver)))
	return entry


def complete_return(name: str, note: str | None = None) -> str:
	"""The customer has the item back (signature and a photo / video uploaded): it leaves the
	transit store and the ticket ends."""
	ticket = _lock(name)
	t.require_step(ticket, t.STEP_OUT)
	_require_driver(ticket)
	missing = ([] if _has_media(ticket.name, CUSTOMER_PREFIX) else [_("a photo or video of the hand-over")]) + \
		([] if files_of(ticket.name, SIGNATURE_PREFIX) else [_("the customer's signature")])
	if missing:
		frappe.throw(_("Upload {0} first.").format(_(" and ").join(missing)), title=_("Not delivered yet"))
	serial = _serial_to_move(ticket, ticket.sm_serial_no, ticket.sm_warehouse)
	entry = _move(ticket, "Material Issue", ticket.sm_warehouse, None, serial)
	_close(ticket)
	log.add("Returned to customer", support_ticket=ticket.name, driver=ticket.sm_driver,
		items=_item_line(ticket, serial), reference=("Stock Entry", entry) if entry else None)
	t.add_note(ticket, _("Returned to the customer"), note)
	return entry


def _close(ticket) -> None:
	ticket.status = "Resolved"
	ticket.sm_resolved_by = frappe.session.user
	_set_step(ticket, t.STEP_CLOSED)
	t._save(ticket)


def attach(name: str, kind: str, filename: str, content: bytes) -> str:
	"""A file on the ticket: the workshop's repair proof, or the hand-over photo / video and
	the customer's signature."""
	if kind not in FILE_KINDS:
		frappe.throw(_("Unknown file kind {0}").format(kind))
	ticket = t.get_ticket(name)
	t.require_view(ticket)
	if kind == "repair":
		_require_workshop()
		t.require_step(ticket, t.STEP_REPAIR)
	else:
		t.require_step(ticket, t.STEP_OUT)
		_require_driver(ticket)
	if kind != "signature" and cstr(filename).rsplit(".", 1)[-1].lower() not in MEDIA_EXTENSIONS:
		frappe.throw(_("Upload a photo or a video."))
	if len(content) > MAX_FILE_BYTES:
		frappe.throw(_("The file is too large (max 25 MB)."))
	ext = cstr(filename).rsplit(".", 1)[-1].lower() if "." in cstr(filename) else "png"
	count = len(files_of(ticket.name, FILE_KINDS[kind])) + 1
	file = frappe.get_doc({"doctype": "File", "file_name": f"{FILE_KINDS[kind]} {count} - {ticket.name}.{ext}",
		"attached_to_doctype": ISSUE, "attached_to_name": ticket.name, "content": content, "is_private": 1})
	file.flags.ignore_permissions = True
	file.insert()
	return file.file_url
