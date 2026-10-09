"""Small, pure helpers for slot dates/times.

Frappe hands Time values back in different shapes depending on the path
(``datetime.timedelta`` from the DB, ``str`` from forms/JSON, ``datetime.time``
from Python callers). Everything in the engine goes through these helpers so a
slot is always identified by the same canonical ``HH:MM:SS`` string.
"""

import datetime

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

WEEKDAY_FIELDS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def to_time_str(value) -> str:
	"""Normalise any time-like value to ``HH:MM:SS``."""
	if value is None or value == "":
		frappe.throw(_("Delivery time is required."))

	if isinstance(value, datetime.timedelta):
		total = int(value.total_seconds())
		if not 0 <= total < 24 * 3600:
			frappe.throw(_("Invalid delivery time {0}.").format(value))
		return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"

	if isinstance(value, datetime.datetime):
		value = value.time()

	if isinstance(value, datetime.time):
		return value.strftime("%H:%M:%S")

	text = str(value).strip().split(".")[0]
	parts = text.split(":")
	try:
		hours = int(parts[0])
		minutes = int(parts[1]) if len(parts) > 1 else 0
		seconds = int(parts[2]) if len(parts) > 2 else 0
		return datetime.time(hours, minutes, seconds).strftime("%H:%M:%S")
	except (ValueError, IndexError):
		frappe.throw(_("Invalid delivery time {0}.").format(value))


def to_date(value) -> datetime.date:
	if not value:
		frappe.throw(_("Delivery date is required."))
	return getdate(value)


def weekday_field(date) -> str:
	return WEEKDAY_FIELDS[to_date(date).weekday()]


def resolve_now(now=None) -> datetime.datetime:
	"""``now`` is injectable so the engine is deterministic under test."""
	return get_datetime(now) if now else now_datetime()


def slot_datetime(date, time) -> datetime.datetime:
	return datetime.datetime.combine(to_date(date), datetime.time.fromisoformat(to_time_str(time)))


def is_too_soon(date, time, lead_hours: float, now=None) -> bool:
	"""True when the slot is in the past or starts inside the lead-time window.

	The boundary is inclusive: with a 2h lead at 15:00, both 16:00 and 17:00
	are blocked and 18:00 is the first bookable slot.
	"""
	cutoff = resolve_now(now) + datetime.timedelta(hours=float(lead_hours or 0))
	return slot_datetime(date, time) <= cutoff
