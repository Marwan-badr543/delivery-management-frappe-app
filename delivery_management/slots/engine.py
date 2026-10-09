"""Delivery slot engine: the single place that books and frees delivery capacity.

Two kinds of capacity are managed:

* **Delivery slots** (non-AC goods): one booking per order per
  ``(area, date, time)``. Capacity is the area's ``cars_number``.
* **AC days**: AC installations are booked per day, counted in units (an order
  with 3 AC items uses 3). Capacity is the date override, or the global value
  in Delivery Settings.

Data model and invariants
-------------------------
``Delivery Slot Reservation`` is the ledger and the source of truth. Each
booking is one row that records exactly what was taken. Releasing reads the
ledger and never recomputes from the current items, so a later change to an
item or item group cannot make the counters drift.

``Delivery Slot Counter`` / ``AC Day Capacity`` are running totals of the ledger
(``rebuild_from_ledger`` can always regenerate them). Their rows are named
deterministically, so two concurrent first bookings of the same slot hit the
primary key instead of creating duplicates, and every change happens under
``SELECT ... FOR UPDATE``.

``Delivery Disabled Slot`` holds blocked slots. Rows have a ``source``:

* ``Capacity`` rows are owned by the engine and are added or removed only when
  the count crosses capacity.
* ``Manual`` rows are owned by the admin and are never touched by a recompute,
  so cancelling an order cannot silently reopen a slot the admin closed.

The engine never commits. Callers (a request, a doc event, a test) own the
transaction, so an order and its booking succeed or fail together.

Lock order is always AC day first, then the delivery slot, which avoids
deadlocks between concurrent mixed orders.
"""

from __future__ import annotations

import datetime

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from delivery_management.slots.settings import get_settings
from delivery_management.slots.timeutils import (
	is_too_soon,
	resolve_now,
	to_date,
	to_time_str,
	weekday_field,
)

SLOT_DELIVERY = "Delivery"
SLOT_AC = "AC"
SOURCE_CAPACITY = "Capacity"
SOURCE_MANUAL = "Manual"
STATUS_RESERVED = "Reserved"
STATUS_RELEASED = "Released"

COUNTER = "Delivery Slot Counter"
DISABLED = "Delivery Disabled Slot"
AC_DAY = "AC Day Capacity"
LEDGER = "Delivery Slot Reservation"
AREA = "Delivery Area"
WORK_HOUR = "Delivery Work Hour"

# Machine-readable reasons, shared by the POS, the admin page and the tests.
REASON_AREA_DISABLED = "area_disabled"
REASON_NOT_WORKING = "not_a_working_hour"
REASON_TOO_SOON = "too_soon"
REASON_PAST = "past_date"
REASON_MANUAL = "manually_disabled"
REASON_FULL = "full"
REASON_NOT_ENOUGH = "not_enough_capacity"


class SlotUnavailableError(frappe.ValidationError):
	"""Raised when a booking would break a capacity or availability rule."""


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def slot_key(area: str, date, time) -> str:
	return f"{area}|{to_date(date).isoformat()}|{to_time_str(time)[:5]}"


def disabled_key(source: str, area: str, date, time) -> str:
	return f"{source}|{slot_key(area, date, time)}"


def ac_day_key(date) -> str:
	return to_date(date).isoformat()


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_area(area: str) -> frappe._dict:
	row = frappe.db.get_value(AREA, area, ["name", "enabled", "cars_number"], as_dict=True)
	if not row:
		frappe.throw(_("Delivery Area {0} does not exist.").format(frappe.bold(area)), SlotUnavailableError)
	row.cars_number = max(cint(row.cars_number), 0)
	return row


def get_working_hours(area: str, date) -> list[str]:
	"""Configured slot start times for ``area`` on the weekday of ``date``."""
	hours = frappe.get_all(
		WORK_HOUR,
		filters={"area": area, weekday_field(date): 1},
		pluck="hour",
	)
	return sorted({to_time_str(h) for h in hours})


def get_delivery_slots(area: str, date, now=None) -> list[dict]:
	"""Every working slot of ``area`` on ``date``, with availability and why.

	Used by the POS picker and the admin page. ``reserve`` re-checks the same
	rules under a lock, so this listing is advisory and the booking decides.
	"""
	date = to_date(date)
	area_row = get_area(area)
	lead = get_settings().slot_lead_time_hours

	counts = {
		to_time_str(r.delivery_time): cint(r.reserved_count)
		for r in frappe.get_all(
			COUNTER,
			filters={"area": area, "delivery_date": date},
			fields=["delivery_time", "reserved_count"],
		)
	}
	blocks: dict[str, set[str]] = {}
	for r in frappe.get_all(
		DISABLED, filters={"area": area, "delivery_date": date}, fields=["delivery_time", "source"]
	):
		blocks.setdefault(to_time_str(r.delivery_time), set()).add(r.source)

	slots = []
	for time in get_working_hours(area, date):
		reserved = counts.get(time, 0)
		reasons = _delivery_reasons(area_row, date, time, reserved, blocks.get(time, set()), lead, now)
		slots.append(
			{
				"time": time[:5],
				"delivery_time": time,
				"reserved": reserved,
				"capacity": area_row.cars_number,
				"remaining": max(area_row.cars_number - reserved, 0),
				"manually_disabled": SOURCE_MANUAL in blocks.get(time, set()),
				"available": not reasons,
				"reasons": reasons,
			}
		)
	return slots


def _delivery_reasons(area_row, date, time, reserved, sources, lead, now) -> list[str]:
	reasons = []
	if not area_row.enabled:
		reasons.append(REASON_AREA_DISABLED)
	if is_too_soon(date, time, lead, now):
		reasons.append(REASON_TOO_SOON)
	if SOURCE_MANUAL in sources:
		reasons.append(REASON_MANUAL)
	if reserved >= area_row.cars_number or SOURCE_CAPACITY in sources:
		reasons.append(REASON_FULL)
	return reasons


def get_ac_day_status(date, units: int = 0, now=None) -> dict:
	"""AC capacity of one day. ``units`` is what the caller wants to book."""
	date = to_date(date)
	units = max(cint(units), 0)
	row = frappe.db.get_value(
		AC_DAY,
		ac_day_key(date),
		["reserved_units", "manually_disabled", "has_capacity_override", "capacity_override"],
		as_dict=True,
	) or frappe._dict()

	capacity = _ac_capacity(row)
	reserved = cint(row.reserved_units)
	reasons = []
	if date < resolve_now(now).date():
		reasons.append(REASON_PAST)
	if cint(row.manually_disabled):
		reasons.append(REASON_MANUAL)
	if reserved >= capacity:
		reasons.append(REASON_FULL)
	elif reserved + units > capacity:
		reasons.append(REASON_NOT_ENOUGH)

	return {
		"date": date.isoformat(),
		"reserved": reserved,
		"capacity": capacity,
		"remaining": max(capacity - reserved, 0),
		"has_capacity_override": bool(cint(row.has_capacity_override)),
		"manually_disabled": bool(cint(row.manually_disabled)),
		"available": not reasons,
		"reasons": reasons,
	}


def get_ac_calendar(start_date=None, days: int | None = None, units: int = 0, now=None) -> list[dict]:
	start = to_date(start_date) if start_date else resolve_now(now).date()
	days = cint(days) or get_settings().booking_window_days
	return [get_ac_day_status(start + datetime.timedelta(days=i), units, now) for i in range(days)]


def _ac_capacity(row) -> int:
	if cint(row.get("has_capacity_override")):
		return max(cint(row.get("capacity_override")), 0)
	return get_settings().global_ac_daily_capacity


def get_reservations(reference_doctype: str, reference_name: str, status: str | None = STATUS_RESERVED):
	filters = {"reference_doctype": reference_doctype, "reference_name": reference_name}
	if status:
		filters["status"] = status
	return frappe.get_all(
		LEDGER,
		filters=filters,
		fields=["name", "slot_type", "status", "area", "delivery_date", "delivery_time", "qty", "forced"],
		order_by="creation asc",
	)


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


def reserve(
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	*,
	area: str | None = None,
	delivery_date=None,
	delivery_time=None,
	ac_date=None,
	ac_units: int = 0,
	force: bool = False,
	now=None,
	customer: str | None = None,
	contact_phone: str | None = None,
	remarks: str | None = None,
) -> list[str]:
	"""Book capacity for one document. Returns the ledger row names.

	* Pass ``area``/``delivery_date``/``delivery_time`` to book a delivery slot.
	* Pass ``ac_date``/``ac_units`` to book AC installation capacity.
	* Pass both for a mixed order.

	The booking is all-or-nothing within the caller's transaction. ``force``
	(admin only) skips the availability rules and may exceed capacity; the row
	is flagged ``forced``. Booking a document that already holds capacity is
	refused; use :func:`reschedule` to move it.
	"""
	wants_delivery = bool(area or delivery_date or delivery_time)
	wants_ac = bool(ac_date or cint(ac_units))
	if not (wants_delivery or wants_ac):
		frappe.throw(_("Nothing to reserve: no delivery slot or AC day given."))
	if bool(reference_doctype) != bool(reference_name):
		frappe.throw(_("Reference DocType and Reference Name must be given together."))
	if reference_doctype:
		requested = {SLOT_AC} if wants_ac else set()
		requested |= {SLOT_DELIVERY} if wants_delivery else set()
		held = {r.slot_type for r in get_reservations(reference_doctype, reference_name)}
		if held & requested:
			frappe.throw(
				_("{0} {1} already has a {2} reservation. Reschedule it instead.").format(
					_(reference_doctype), reference_name, ", ".join(sorted(held & requested))
				),
				SlotUnavailableError,
			)

	common = dict(
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		customer=customer,
		contact_phone=contact_phone,
		remarks=remarks,
		forced=1 if force else 0,
	)
	created = []
	# Lock order: AC day first, then the delivery slot.
	if wants_ac:
		created.append(_reserve_ac(ac_date, ac_units, force, now, common))
	if wants_delivery:
		created.append(_reserve_delivery(area, delivery_date, delivery_time, force, now, common))
	return created


def _reserve_ac(ac_date, ac_units, force, now, common) -> str:
	date = to_date(ac_date)
	units = cint(ac_units)
	if units <= 0:
		frappe.throw(_("AC units to reserve must be greater than zero."))

	row = _lock_ac_day(date)
	capacity = _ac_capacity(row)
	reserved = cint(row.reserved_units)

	if not force:
		if date < resolve_now(now).date():
			_unavailable(_("AC installation date {0} is in the past.").format(date))
		if cint(row.manually_disabled):
			_unavailable(_("AC installations are closed on {0}.").format(date))
		if reserved + units > capacity:
			_unavailable(
				_("Not enough AC installation capacity on {0}: {1} of {2} units booked, {3} requested.").format(
					date, reserved, capacity, units
				)
			)

	_write_ac_day(row.name, reserved + units, capacity)
	return _insert_ledger(SLOT_AC, delivery_date=date, qty=units, **common)


def _reserve_delivery(area, delivery_date, delivery_time, force, now, common) -> str:
	if not (area and delivery_date and delivery_time):
		frappe.throw(_("Area, delivery date and delivery time are all required to book a delivery slot."))
	date = to_date(delivery_date)
	time = to_time_str(delivery_time)
	area_row = get_area(area)

	if time not in get_working_hours(area, date):
		if not force or not frappe.db.exists(WORK_HOUR, {"area": area, "hour": time}):
			_unavailable(_("{0} is not a working slot for area {1} on {2}.").format(time[:5], area, date))

	counter = _lock_counter(area, date, time)
	reserved = cint(counter.reserved_count)

	if not force:
		if not area_row.enabled:
			_unavailable(_("Delivery area {0} is disabled.").format(area))
		if is_too_soon(date, time, get_settings().slot_lead_time_hours, now):
			_unavailable(_("The {0} slot on {1} is too soon or already passed.").format(time[:5], date))
		if _row_exists(DISABLED, disabled_key(SOURCE_MANUAL, area, date, time)):
			_unavailable(_("The {0} slot on {1} in {2} is closed.").format(time[:5], date, area))
		if reserved + 1 > area_row.cars_number:
			_unavailable(
				_("The {0} slot on {1} in {2} is full ({3}/{4}).").format(
					time[:5], date, area, reserved, area_row.cars_number
				)
			)

	_write_counter(counter.name, area, date, time, reserved + 1, area_row.cars_number)
	return _insert_ledger(
		SLOT_DELIVERY, area=area, delivery_date=date, delivery_time=time, qty=1, **common
	)


def release(reference_doctype: str, reference_name: str) -> int:
	"""Free every active booking of a document. Safe to call twice (returns 0)."""
	names = frappe.get_all(
		LEDGER,
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"status": STATUS_RESERVED,
		},
		pluck="name",
	)
	return _release_rows(names)


def release_reservation(reservation: str) -> bool:
	"""Free one ledger row (used for manual admin bookings)."""
	return bool(_release_rows([reservation]))


def reschedule(reference_doctype: str, reference_name: str, **booking) -> list[str]:
	"""Move a document's booking. Old capacity is freed before the new one is checked,
	so moving within the same full slot works; if the new booking fails, the
	caller's transaction rolls the release back too."""
	release(reference_doctype, reference_name)
	return reserve(reference_doctype, reference_name, **booking)


def move_reservation(
	reservation: str, *, area=None, delivery_date=None, delivery_time=None, ac_date=None, force=False, now=None
) -> str:
	"""Move one booking (e.g. only the AC day of a mixed order) to a new slot.

	The same owner, customer and quantity are kept. Returns the new ledger row.
	"""
	row = frappe.db.get_value(
		LEDGER,
		reservation,
		["name", "status", "slot_type", "qty", "reference_doctype", "reference_name", "customer",
			"contact_phone", "remarks"],
		as_dict=True,
	)
	if not row or row.status != STATUS_RESERVED:
		frappe.throw(_("Reservation {0} is not active.").format(reservation))
	_release_rows([row.name])
	common = dict(
		customer=row.customer, contact_phone=row.contact_phone, remarks=row.remarks, force=force, now=now
	)
	if row.slot_type == SLOT_AC:
		booked = reserve(row.reference_doctype, row.reference_name, ac_date=ac_date, ac_units=row.qty, **common)
	else:
		booked = reserve(row.reference_doctype, row.reference_name, area=area, delivery_date=delivery_date,
			delivery_time=delivery_time, **common)
	return booked[0]


def _release_rows(names: list[str]) -> int:
	if not names:
		return 0
	rows = frappe.db.sql(
		f"""select name, slot_type, status, area, delivery_date, delivery_time, qty
		from `tab{LEDGER}` where name in %(names)s for update""",
		{"names": tuple(names)},
		as_dict=True,
	)
	# Same lock order as reserve: AC days before delivery slots.
	rows = sorted((r for r in rows if r.status == STATUS_RESERVED), key=lambda r: r.slot_type != SLOT_AC)
	for r in rows:
		if r.slot_type == SLOT_AC:
			day = _lock_ac_day(r.delivery_date)
			_write_ac_day(day.name, max(cint(day.reserved_units) - cint(r.qty), 0), _ac_capacity(day))
		else:
			counter = _lock_counter(r.area, r.delivery_date, r.delivery_time)
			_write_counter(
				counter.name,
				r.area,
				r.delivery_date,
				r.delivery_time,
				max(cint(counter.reserved_count) - cint(r.qty), 0),
				get_area(r.area).cars_number,
			)
		frappe.db.set_value(
			LEDGER,
			r.name,
			{"status": STATUS_RELEASED, "released_on": now_datetime(), "released_by": frappe.session.user},
		)
	return len(rows)


# ---------------------------------------------------------------------------
# Admin operations
# ---------------------------------------------------------------------------


def set_slot_manual_block(area: str, date, time, blocked: bool, remarks: str | None = None) -> None:
	"""Close or reopen a slot by hand. Reopening cannot override a full slot."""
	get_area(area)
	_lock_counter(area, date, time)  # serialise with concurrent bookings of this slot
	_set_block_row(SOURCE_MANUAL, area, date, time, blocked, remarks)


def set_ac_day_manual_block(date, blocked: bool) -> None:
	row = _lock_ac_day(date)
	frappe.db.set_value(AC_DAY, row.name, "manually_disabled", 1 if blocked else 0)


def set_ac_day_capacity(date, capacity: int | None) -> None:
	"""Set a date-specific AC capacity, or ``None`` to fall back to the global value."""
	if capacity is not None and cint(capacity) < 0:
		frappe.throw(_("AC capacity cannot be negative."))
	row = _lock_ac_day(date)
	has_override = capacity is not None
	frappe.db.set_value(
		AC_DAY,
		row.name,
		{"has_capacity_override": 1 if has_override else 0, "capacity_override": cint(capacity) if has_override else 0},
	)
	row.update(has_capacity_override=has_override, capacity_override=cint(capacity))
	_write_ac_day(row.name, cint(row.reserved_units), _ac_capacity(row))


def resync_area(area: str, from_date=None) -> None:
	"""Recompute 'full' state after an area's cars or enabled flag changed."""
	from_date = to_date(from_date) if from_date else now_datetime().date()
	cars = get_area(area).cars_number
	for c in frappe.get_all(
		COUNTER,
		filters={"area": area, "delivery_date": [">=", from_date]},
		fields=["delivery_date", "delivery_time"],
		order_by="delivery_date asc, delivery_time asc",
	):
		locked = _lock_counter(area, c.delivery_date, c.delivery_time)
		_write_counter(locked.name, area, c.delivery_date, c.delivery_time, cint(locked.reserved_count), cars)


def resync_ac_days(from_date=None) -> None:
	"""Recompute 'full' state after the global AC capacity changed."""
	from_date = to_date(from_date) if from_date else now_datetime().date()
	for date in frappe.get_all(
		AC_DAY, filters={"delivery_date": [">=", from_date]}, pluck="delivery_date", order_by="delivery_date asc"
	):
		row = _lock_ac_day(date)
		_write_ac_day(row.name, cint(row.reserved_units), _ac_capacity(row))


def rebuild_from_ledger(from_date=None) -> dict:
	"""Repair tool: regenerate every counter from the ledger.

	Counters are only a cache of the ledger, so this is always safe. Run with
	``bench --site <site> execute delivery_management.slots.engine.rebuild_from_ledger``.
	"""
	from_date = to_date(from_date) if from_date else now_datetime().date()
	delivery_totals: dict[tuple, int] = {}
	ac_totals: dict[datetime.date, int] = {}
	for r in frappe.get_all(
		LEDGER,
		filters={"status": STATUS_RESERVED, "delivery_date": [">=", from_date]},
		fields=["slot_type", "area", "delivery_date", "delivery_time", "qty"],
	):
		if r.slot_type == SLOT_AC:
			ac_totals[r.delivery_date] = ac_totals.get(r.delivery_date, 0) + cint(r.qty)
		else:
			key = (r.area, r.delivery_date, to_time_str(r.delivery_time))
			delivery_totals[key] = delivery_totals.get(key, 0) + cint(r.qty)

	for c in frappe.get_all(
		COUNTER, filters={"delivery_date": [">=", from_date]}, fields=["area", "delivery_date", "delivery_time"]
	):
		delivery_totals.setdefault((c.area, c.delivery_date, to_time_str(c.delivery_time)), 0)
	for d in frappe.get_all(AC_DAY, filters={"delivery_date": [">=", from_date]}, pluck="delivery_date"):
		ac_totals.setdefault(d, 0)

	for (area, date, time), total in delivery_totals.items():
		counter = _lock_counter(area, date, time)
		_write_counter(counter.name, area, date, time, total, get_area(area).cars_number)
	for date, total in ac_totals.items():
		day = _lock_ac_day(date)
		_write_ac_day(day.name, total, _ac_capacity(day))

	return {"delivery_slots": len(delivery_totals), "ac_days": len(ac_totals)}


# ---------------------------------------------------------------------------
# Internals: locked rows and state writes
# ---------------------------------------------------------------------------


def _unavailable(message: str):
	frappe.throw(message, SlotUnavailableError, title=_("Slot Unavailable"))


def _lock_counter(area, date, time) -> frappe._dict:
	date, time = to_date(date), to_time_str(time)
	return _lock_or_create(
		COUNTER,
		slot_key(area, date, time),
		"name, reserved_count",
		dict(area=area, delivery_date=date, delivery_time=time, reserved_count=0),
	)


def _lock_ac_day(date) -> frappe._dict:
	date = to_date(date)
	return _lock_or_create(
		AC_DAY,
		ac_day_key(date),
		"name, delivery_date, reserved_units, manually_disabled, has_capacity_override, capacity_override",
		dict(delivery_date=date, reserved_units=0),
	)


def _lock_or_create(doctype: str, name: str, columns: str, values: dict) -> frappe._dict:
	"""Return the row locked ``FOR UPDATE``, creating it first if needed.

	A missing row is inserted *before* it is locked: ``SELECT ... FOR UPDATE`` on a
	missing key takes a gap lock, and two such gap locks followed by two inserts
	is a deadlock. Inserting first makes the second transaction wait on the
	duplicate key until the first commits. It then gets a duplicate-key error
	inside a savepoint, undoes only that insert, and locks the winner's row.
	"""
	if not _row_exists(doctype, name):
		_insert_if_missing(doctype, name, **values)
	row = _select_for_update(doctype, name, columns)
	if not row:
		frappe.throw(_("Could not lock {0} {1}.").format(doctype, name))
	return row


def _row_exists(doctype: str, name: str) -> bool:
	"""Non-locking (snapshot) read; may be stale, callers must tolerate that."""
	return bool(frappe.db.exists(doctype, name))


def _select_for_update(doctype: str, name: str, columns: str):
	rows = frappe.db.sql(
		f"select {columns} from `tab{doctype}` where name = %s for update", (name,), as_dict=True
	)
	return rows[0] if rows else None


def _insert_if_missing(doctype: str, name: str, **values) -> None:
	"""Insert, treating "another transaction just inserted it" as success."""
	savepoint = "dm_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		_insert(doctype, name, **values)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		frappe.db.rollback(save_point=savepoint)


def _insert(doctype: str, name: str, **values):
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.name = name
	doc.flags.name_set = True
	doc.insert(ignore_permissions=True, set_name=name)
	return doc


def _write_counter(name, area, date, time, reserved: int, cars: int) -> None:
	is_full = reserved >= cars
	frappe.db.set_value(
		COUNTER, name, {"reserved_count": reserved, "capacity": cars, "is_full": 1 if is_full else 0}
	)
	_set_block_row(SOURCE_CAPACITY, area, date, time, is_full, _("Full: {0}/{1} cars booked").format(reserved, cars))


def _set_block_row(source: str, area, date, time, blocked: bool, remarks: str | None = None) -> None:
	"""Add or remove one ``Delivery Disabled Slot`` row. The caller holds the counter lock.

	Blind, idempotent writes: no snapshot read can be stale under REPEATABLE READ,
	and no locking read of a missing row takes gap locks, which deadlock concurrent
	inserts.
	"""
	name = disabled_key(source, area, date, time)
	if blocked:
		_insert_if_missing(DISABLED, name, area=area, delivery_date=to_date(date),
			delivery_time=to_time_str(time), source=source, remarks=remarks)
	else:
		frappe.db.delete(DISABLED, {"name": name})


def _write_ac_day(name, reserved: int, capacity: int) -> None:
	frappe.db.set_value(
		AC_DAY,
		name,
		{"reserved_units": reserved, "effective_capacity": capacity, "is_full": 1 if reserved >= capacity else 0},
	)


def _insert_ledger(slot_type: str, **values) -> str:
	doc = frappe.get_doc({"doctype": LEDGER, "slot_type": slot_type, "status": STATUS_RESERVED, **values})
	doc.insert(ignore_permissions=True)
	return doc.name
