"""Installation Statistics: units each driver / AC Tech delivered, from submitted Delivery Notes.

Port of the old ``InstallationStatsService``:

* Only **submitted** Delivery Notes with a driver count (submitted = delivered /
  installed; the old ``is_delivered`` / ``is_ac_delivered``). Cancelled notes do not count.
* Goods returned while their note was still On the way (Return Delivery Note "Returned
  Before Delivery") were never delivered or installed: they are taken off the note, and a
  note with nothing left is no order. Goods returned after delivery still count: the work
  was done.
* The date is the day the order was worked: the AC installation day for AC orders,
  the delivery day for Non AC orders (the posting date when the order had none).
* AC units = quantity of AC items; Non AC units = quantity of the other stock items
  (AC accessories count only when the order has no AC unit; items of a service
  Item Group and the delivery charge are never units). Bundles count by their components.
* ``unit`` = ``ac`` / ``non_ac`` keeps only drivers with units of that kind.
* Unpaid = AC units of the notes not paid yet on an AC Tech Payment x the AC Tech's
  "AC Unit Installation Cost" (User). What is paid keeps the cost it was paid at.
* No caching: every call reads the database.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from delivery_management.slots.cart import unit_counts

WORK_DATE = """(case when dn.dm_delivery_type = 'AC'
	then coalesce(dn.dm_ac_installation_date, dn.dm_delivery_date, dn.posting_date)
	else coalesce(dn.dm_delivery_date, dn.posting_date) end)"""


def delivered_notes(start_date=None, end_date=None, technician: str | None = None, order_type: str | None = None,
		names: list[str] | None = None) -> list[dict]:
	"""Delivered notes with a driver: name, driver, order type and work date, oldest first."""
	# Submitted notes still On the way are not delivered yet (older notes have no Delivery Status).
	conditions = ["dn.docstatus = 1", "ifnull(dn.dm_delivery_status, '') != 'On the way'", "dn.is_return = 0",
		"ifnull(dn.dm_driver, '') != ''"]
	if start_date:
		conditions.append(f"{WORK_DATE} >= %(start)s")
	if end_date:
		conditions.append(f"{WORK_DATE} <= %(end)s")
	if technician:
		conditions.append("dn.dm_driver = %(tech)s")
	if order_type:
		conditions.append("dn.dm_delivery_type = %(type)s")
	if names is not None:
		conditions.append("dn.name in %(names)s")
	return frappe.db.sql(
		f"""select dn.name, dn.dm_driver as driver, dn.dm_delivery_type as order_type, {WORK_DATE} as work_date
		from `tabDelivery Note` dn where {' and '.join(conditions)} order by work_date, dn.name""",
		{"start": start_date, "end": end_date, "tech": technician, "type": order_type, "names": tuple(names or [""])},
		as_dict=True,
	)


def units_by_note(names: list[str]) -> dict[str, tuple[int, float]]:
	"""``{note: (ac_units, non_ac_units)}``, the goods returned before delivery taken off.
	A note they emptied is left out (no order)."""
	if not names:
		return {}
	lines: dict[str, list] = {}
	for row in frappe.get_all("Delivery Note Item", filters={"parent": ["in", names], "parenttype": "Delivery Note"},
			fields=["parent", "item_code", "qty", "stock_qty", "conversion_factor"]):
		lines.setdefault(row.parent, []).append(row)
	returned: set[str] = set()
	for row in frappe.db.sql(
		"""select ret.return_against as parent, dni.item_code, dni.qty, dni.stock_qty, dni.conversion_factor
		from `tabDelivery Note Item` dni join `tabDelivery Note` ret on ret.name = dni.parent
		where ret.docstatus = 1 and ret.is_return = 1 and ret.dm_returned_before_delivery = 1
		and ret.return_against in %(notes)s""",
		{"notes": tuple(names)},
		as_dict=True,
	):
		lines.setdefault(row.parent, []).append(row)  # negative quantities
		returned.add(row.parent)
	units = {}
	for name in names:
		ac_units, non_ac_units = unit_counts(lines.get(name, []))
		if name in returned and ac_units <= 0 and non_ac_units <= 0:
			continue
		units[name] = (ac_units, non_ac_units)
	return units


def paid_notes(names: list[str]) -> set[str]:
	"""Notes already paid on a submitted AC Tech Payment."""
	if not names:
		return set()
	return set(frappe.get_all("AC Tech Payment Item", filters={"delivery_note": ["in", names], "docstatus": 1},
		pluck="delivery_note"))


def installation_stats(start_date, end_date, technician: str | None = None, unit: str = "all") -> list[dict]:
	notes = delivered_notes(start_date, end_date, technician)
	if not notes:
		return []
	units = units_by_note([n.name for n in notes])
	paid = paid_notes(list(units))

	per_driver: dict[str, dict] = {}
	for note in notes:
		if note.name not in units:
			continue
		ac_units, non_ac_units = units[note.name]
		entry = per_driver.setdefault(note.driver, {"technician": note.driver, "ac_units": 0, "non_ac_units": 0,
			"unpaid_ac_units": 0, "orders": 0, "delivery_notes": []})
		entry["ac_units"] += ac_units
		entry["non_ac_units"] += non_ac_units
		if note.name not in paid:
			entry["unpaid_ac_units"] += ac_units
		entry["orders"] += 1
		entry["delivery_notes"].append(note.name)

	users = {u.name: u for u in frappe.get_all("User", filters={"name": ["in", list(per_driver)]},
		fields=["name", "full_name", "dm_ac_unit_cost"])}
	rows = []
	for entry in per_driver.values():
		u = users.get(entry["technician"]) or frappe._dict()
		entry["full_name"] = u.full_name or entry["technician"]
		if unit == "ac":
			if not entry["ac_units"]:
				continue
			entry["non_ac_units"] = 0
		elif unit == "non_ac":
			if not entry["non_ac_units"]:
				continue
			entry["ac_units"] = entry["unpaid_ac_units"] = 0
		entry["total_units"] = entry["ac_units"] + entry["non_ac_units"]
		entry["unpaid"] = flt(entry.pop("unpaid_ac_units") * flt(u.dm_ac_unit_cost), 3)
		rows.append(entry)
	rows.sort(key=lambda r: (r["full_name"] or "").lower())
	return rows
