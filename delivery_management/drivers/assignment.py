"""Driver / AC Tech assignment of Delivery Notes (port of the old driver_schedule service).

Rules, as in the old system:

* A Delivery Note has one **Order Type**: ``AC`` (installation) or ``Non AC`` (TV and
  other goods). AC orders go to users with the **AC Tech** role, Non AC orders to
  users with the **TV Driver** role.
* **Non AC** is booked by time: a driver is unavailable when another of his orders on
  the same day is within the conflict window of this order's time (0.5 h when the
  person assigning is a System Manager, 1 h otherwise). An order without a time
  conflicts with everything that day.
* **AC** is booked per day: an AC Tech is unavailable once he has his daily maximum
  of AC orders (Delivery Settings, or a per-day *AC Tech Day Limit*; 0 = day off).
* Every assignment is one row in **Driver Schedule** (the old driver_schedule table);
  availability is always counted from those rows, never from a stored counter.

System Manager here = admin in the old system.
"""

from __future__ import annotations

import datetime

import frappe
from frappe import _
from frappe.utils import cint, get_time, getdate

from delivery_management.slots.cart import profile_cart
from delivery_management.slots.settings import get_settings

ROLE_AC = "AC Tech"
ROLE_TV = "TV Driver"
DRIVER_ROLES = (ROLE_AC, ROLE_TV)
TYPE_AC = "AC"
TYPE_NON_AC = "Non AC"
ROLE_FOR_TYPE = {TYPE_AC: ROLE_AC, TYPE_NON_AC: ROLE_TV}

ADMIN_ROLE = "System Manager"
MANAGER_ROLES = (ADMIN_ROLE, "Delivery Manager")

DN = "Delivery Note"
ASSIGNMENT = "Driver Schedule"
DAY_LIMIT = "AC Tech Day Limit"  # child table ``ac_tech_day_limits`` of Delivery Settings
SETTINGS = "Delivery Settings"
_DAY_LIMIT_PARENT = {"parent": SETTINGS, "parenttype": SETTINGS, "parentfield": "ac_tech_day_limits"}


# ---------------------------------------------------------------------------
# Who is who
# ---------------------------------------------------------------------------


def is_admin(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or ADMIN_ROLE in frappe.get_roles(user)


def can_manage(user: str | None = None) -> bool:
	"""May assign drivers and see every driver's orders."""
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(MANAGER_ROLES) & set(frappe.get_roles(user)))


def is_driver(user: str | None = None) -> bool:
	return bool(set(DRIVER_ROLES) & set(frappe.get_roles(user or frappe.session.user)))


def conflict_window_hours(user: str | None = None) -> float:
	"""Delivery Settings: TV Driver Gap (System Manager / everyone else)."""
	settings = get_settings()
	return settings.tv_driver_gap_hours_manager if is_admin(user) else settings.tv_driver_gap_hours


def drivers_for(order_type: str) -> list[dict]:
	"""Enabled users with the role that serves ``order_type``, by full name."""
	role = ROLE_FOR_TYPE.get(order_type)
	if not role:
		return []
	return frappe.db.sql(
		"""select u.name as user, coalesce(nullif(u.full_name, ''), u.name) as full_name
		from `tabUser` u join `tabHas Role` r on r.parent = u.name and r.parenttype = 'User'
		where r.role = %s and u.enabled = 1 and u.name not in ('Administrator', 'Guest')
		group by u.name order by full_name, u.name""",
		role,
		as_dict=True,
	)


# ---------------------------------------------------------------------------
# Order type and schedule of a Delivery Note
# ---------------------------------------------------------------------------


def order_type_of(items) -> str | None:
	"""``AC``, ``Non AC``, or ``None`` when the lines mix AC units with other goods.

	Same classification as the POS (Delivery Settings AC item groups, bundles by their
	components): accessories sold with AC units belong to the installation, non-stock
	lines (delivery charge, services) never decide the type.
	"""
	profile = profile_cart(items)
	if profile.has_ac and profile.other_stock_items:
		return None
	return TYPE_AC if profile.has_ac else TYPE_NON_AC


def schedule_of(dn) -> tuple[datetime.date | None, datetime.time | None, str | None]:
	"""(date, time, area) the driver works this order. AC uses the installation day."""
	if dn.get("dm_delivery_type") == TYPE_AC:
		day = dn.get("dm_ac_installation_date") or dn.get("dm_delivery_date")
		return (getdate(day) if day else None), None, dn.get("dm_delivery_area")
	day = dn.get("dm_delivery_date")
	time = dn.get("dm_delivery_time")
	return (getdate(day) if day else None), (get_time(time) if time else None), dn.get("dm_delivery_area")


def _minutes(t: datetime.time) -> int:
	return t.hour * 60 + t.minute


def schedules_conflict(scheduled, target, window_hours: float) -> bool:
	"""True when two TV orders cannot share one driver. A missing time always conflicts."""
	if not scheduled or not target:
		return True
	scheduled, target = get_time(scheduled), get_time(target)
	return abs(_minutes(scheduled) - _minutes(target)) <= int(window_hours * 60)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def busy_tv_drivers(day, time, window_hours: float, exclude_delivery_note: str | None = None) -> set[str]:
	rows = frappe.get_all(
		ASSIGNMENT,
		filters={"order_type": TYPE_NON_AC, "delivery_date": day,
			"delivery_note": ["!=", exclude_delivery_note or ""]},
		fields=["driver", "delivery_time"],
	)
	return {r.driver for r in rows if schedules_conflict(r.delivery_time, time, window_hours)}


def effective_max_orders(override: int | None, global_max: int | None) -> int | None:
	"""A per-day row always wins (0 = day off); otherwise the global limit. None = no limit."""
	if override is None:
		return global_max
	return max(0, cint(override))


def global_ac_max() -> int | None:
	return get_settings().ac_tech_max_orders_per_day or None


def ac_loads(work_date, exclude_delivery_note: str | None = None) -> tuple[list[dict], int | None]:
	"""One entry per AC Tech for ``work_date`` and the global limit (None = no limit)."""
	work_date = getdate(work_date)
	global_max = global_ac_max()
	counts = dict(
		frappe.db.sql(
			f"""select driver, count(*) from `tab{ASSIGNMENT}`
			where order_type = %s and delivery_date = %s and delivery_note != %s group by driver""",
			(TYPE_AC, work_date, exclude_delivery_note or ""),
		)
	)
	overrides = dict(
		frappe.get_all(DAY_LIMIT, filters={**_DAY_LIMIT_PARENT, "work_date": work_date}, fields=["driver", "max_orders"],
			as_list=True)
	)
	loads = []
	for d in drivers_for(TYPE_AC):
		assigned = cint(counts.get(d.user))
		max_orders = effective_max_orders(overrides.get(d.user), global_max)
		loads.append({
			"user": d.user,
			"full_name": d.full_name,
			"assigned_orders": assigned,
			"max_orders": max_orders,
			"is_full": max_orders is not None and assigned >= max_orders,
			# A limit of 0 is a deliberate "not working today", not a day that filled up.
			"is_day_off": max_orders == 0,
			"has_override": d.user in overrides,
		})
	return loads, global_max


def availability(delivery_note: str, user: str | None = None) -> dict:
	"""Drivers who can serve this Delivery Note, each with ``available`` and a label."""
	dn = frappe.get_doc(DN, delivery_note)
	order_type = dn.get("dm_delivery_type")
	day, time, _area = schedule_of(dn)
	result = {
		"delivery_note": dn.name,
		"order_type": order_type,
		"delivery_date": day,
		"delivery_time": time and str(time),
		"window_hours": conflict_window_hours(user),
		"max_orders_per_day": None,
		"current_driver": dn.get("dm_driver"),
		"drivers": [],
		"message": None,
	}
	if not order_type:
		result["message"] = _("Choose the Order Type (AC / Non AC) on the Delivery Note first.")
		return result
	if not day:
		result["message"] = _("The Delivery Note has no delivery date.")
		return result

	if order_type == TYPE_AC:
		loads, result["max_orders_per_day"] = ac_loads(day, exclude_delivery_note=dn.name)
		for load in loads:
			available = not load["is_full"]
			if load["is_day_off"]:
				label = _("day off")
			elif load["max_orders"] is not None:
				label = f"{load['assigned_orders']}/{load['max_orders']}" + (f" {_('full')}" if load["is_full"] else "")
			else:
				label = _("{0} orders").format(load["assigned_orders"])
			result["drivers"].append({**load, "available": available, "label": label})
		return result

	busy = busy_tv_drivers(day, time, result["window_hours"], exclude_delivery_note=dn.name)
	for d in drivers_for(TYPE_NON_AC):
		available = d.user not in busy
		result["drivers"].append({**d, "available": available, "label": "" if available else _("unavailable")})
	return result


def check_assignable(dn, driver: str, user: str | None = None) -> None:
	"""Throw unless ``driver`` may take this Delivery Note (role, then availability)."""
	order_type = dn.get("dm_delivery_type")
	if not order_type:
		frappe.throw(_("Choose the Order Type (AC / Non AC) of {0} first.").format(dn.name))
	if not frappe.db.get_value("User", driver, "enabled"):
		frappe.throw(_("User {0} is not enabled.").format(driver))
	role = ROLE_FOR_TYPE[order_type]
	if role not in frappe.get_roles(driver):
		label = _("AC Tech") if order_type == TYPE_AC else _("TV Driver")
		frappe.throw(_("{0} is not a {1}. Give the user the {2} role first.").format(driver, label, role))
	day, time, _area = schedule_of(dn)
	if not day:
		frappe.throw(_("Delivery Note {0} has no delivery date; it is needed before assigning a driver.").format(dn.name))

	if order_type == TYPE_AC:
		override = frappe.db.get_value(DAY_LIMIT, {**_DAY_LIMIT_PARENT, "driver": driver, "work_date": day}, "max_orders")
		max_orders = effective_max_orders(override, global_ac_max())
		if max_orders is None:
			return
		if max_orders == 0:
			frappe.throw(_("This AC Tech is marked unavailable (day off) on {0}.").format(frappe.format(day, "Date")))
		assigned = frappe.db.count(ASSIGNMENT, {"order_type": TYPE_AC, "driver": driver, "delivery_date": day,
			"delivery_note": ["!=", dn.name]})
		if assigned >= max_orders:
			frappe.throw(_("This AC Tech already has {0} of {1} allowed orders on {2}.").format(
				assigned, max_orders, frappe.format(day, "Date")))
		return

	if driver in busy_tv_drivers(day, time, conflict_window_hours(user), exclude_delivery_note=dn.name):
		frappe.throw(_("This driver already has another order too close to this delivery time."))


# ---------------------------------------------------------------------------
# Assign / unassign
# ---------------------------------------------------------------------------


def _lock_driver(driver: str) -> None:
	"""Serialize assignments of one driver, so two people cannot both take his last free spot."""
	frappe.db.sql("select name from `tabUser` where name = %s for update", driver)


def assign(delivery_note: str, driver: str, user: str | None = None) -> dict:
	dn = frappe.get_doc(DN, delivery_note, for_update=True)
	if dn.docstatus == 2:
		frappe.throw(_("Delivery Note {0} is cancelled.").format(dn.name))
	_lock_driver(driver)
	check_assignable(dn, driver, user)
	frappe.db.set_value(DN, dn.name, "dm_driver", driver)
	dn.dm_driver = driver
	sync_assignment(dn)
	return {"delivery_note": dn.name, "driver": driver}


def unassign(delivery_note: str) -> dict:
	frappe.db.set_value(DN, delivery_note, "dm_driver", None)
	remove_assignment(delivery_note)
	return {"delivery_note": delivery_note, "driver": None}


def sync_assignment(dn) -> None:
	"""Make the Driver Schedule row equal to the Delivery Note (or remove it)."""
	if not dn.get("dm_driver") or not dn.get("dm_delivery_type") or dn.docstatus == 2:
		remove_assignment(dn.name)
		return
	day, time, area = schedule_of(dn)
	values = {
		"order_type": dn.dm_delivery_type,
		"driver": dn.dm_driver,
		"delivery_date": day,
		"delivery_time": time,
		"area": area,
		"customer_name": dn.get("customer_name"),
	}
	if frappe.db.exists(ASSIGNMENT, dn.name):
		frappe.db.set_value(ASSIGNMENT, dn.name, values)
	else:
		frappe.get_doc({"doctype": ASSIGNMENT, "delivery_note": dn.name, **values}).insert(ignore_permissions=True)


def remove_assignment(delivery_note: str) -> None:
	if frappe.db.exists(ASSIGNMENT, delivery_note):
		frappe.delete_doc(ASSIGNMENT, delivery_note, ignore_permissions=True, force=True)


def set_day_limit(driver: str, work_date, max_orders) -> None:
	"""Per-day limit of one AC Tech, kept in Delivery Settings > AC Tech Day Limits.
	``max_orders`` None/'' = follow the global limit again.

	Only that one row is written (never the whole settings document), under a lock on the
	driver, so two managers editing different days or technicians cannot undo each other.
	"""
	work_date = getdate(work_date)
	_lock_driver(driver)
	name = frappe.db.get_value(DAY_LIMIT, {**_DAY_LIMIT_PARENT, "driver": driver, "work_date": work_date})
	if max_orders in (None, ""):
		if name:
			frappe.db.delete(DAY_LIMIT, {"name": name})
	else:
		if cint(max_orders) < 0:
			frappe.throw(_("The limit cannot be negative."))
		if ROLE_AC not in frappe.get_roles(driver):
			frappe.throw(_("{0} does not have the {1} role.").format(driver, ROLE_AC))
		if name:
			frappe.db.set_value(DAY_LIMIT, name, "max_orders", cint(max_orders))
		else:
			idx = cint(frappe.db.sql(f"select max(idx) from `tab{DAY_LIMIT}` where parent = %s", SETTINGS)[0][0]) + 1
			row = frappe.get_doc({"doctype": DAY_LIMIT, **_DAY_LIMIT_PARENT, "idx": idx, "driver": driver,
				"work_date": work_date, "max_orders": cint(max_orders)})
			row.db_insert()
	frappe.db.set_value(SETTINGS, SETTINGS, "modified", frappe.utils.now(), update_modified=False)
	frappe.clear_document_cache(SETTINGS, SETTINGS)
