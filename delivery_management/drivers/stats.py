"""Installation Statistics: units each driver / AC Tech delivered, from submitted Delivery Notes.

Port of the old ``InstallationStatsService``:

* Only **submitted** Delivery Notes with a driver count (submitted = delivered /
  installed; the old ``is_delivered`` / ``is_ac_delivered``). Returns and
  cancelled notes do not count.
* The date is the day the order was worked: the AC installation day for AC orders,
  the delivery day for Non AC orders (the posting date when the order had none).
* AC units = quantity of AC items; Non AC units = quantity of the other stock items
  (AC accessories count only when the order has no AC unit; items of a service
  Item Group and the delivery charge are never units). Bundles count by their components.
* ``unit`` = ``ac`` / ``non_ac`` keeps only drivers with units of that kind.
* No caching: every call reads the database.
"""

from __future__ import annotations

import frappe

from delivery_management.slots.cart import unit_counts


def installation_stats(start_date, end_date, technician: str | None = None, unit: str = "all") -> list[dict]:
	conditions = ["dn.docstatus = 1", "dn.is_return = 0", "ifnull(dn.dm_driver, '') != ''",
		"""(case when dn.dm_delivery_type = 'AC'
			then coalesce(dn.dm_ac_installation_date, dn.dm_delivery_date, dn.posting_date)
			else coalesce(dn.dm_delivery_date, dn.posting_date) end) between %(start)s and %(end)s"""]
	values = {"start": start_date, "end": end_date}
	if technician:
		conditions.append("dn.dm_driver = %(tech)s")
		values["tech"] = technician
	notes = frappe.db.sql(
		f"""select dn.name, dn.dm_driver as driver from `tabDelivery Note` dn where {' and '.join(conditions)}""",
		values,
		as_dict=True,
	)
	if not notes:
		return []
	lines: dict[str, list] = {}
	for row in frappe.get_all("Delivery Note Item", filters={"parent": ["in", [n.name for n in notes]],
			"parenttype": "Delivery Note"}, fields=["parent", "item_code", "qty", "stock_qty", "conversion_factor"]):
		lines.setdefault(row.parent, []).append(row)

	per_driver: dict[str, dict] = {}
	for note in notes:
		ac_units, non_ac_units = unit_counts(lines.get(note.name, []))
		entry = per_driver.setdefault(note.driver, {"technician": note.driver, "ac_units": 0, "non_ac_units": 0,
			"orders": 0, "delivery_notes": []})
		entry["ac_units"] += ac_units
		entry["non_ac_units"] += non_ac_units
		entry["orders"] += 1
		entry["delivery_notes"].append(note.name)

	names = {u.name: u.full_name or u.name for u in frappe.get_all("User",
		filters={"name": ["in", list(per_driver)]}, fields=["name", "full_name"])}
	rows = []
	for entry in per_driver.values():
		entry["full_name"] = names.get(entry["technician"], entry["technician"])
		if unit == "ac":
			if not entry["ac_units"]:
				continue
			entry["non_ac_units"] = 0
		elif unit == "non_ac":
			if not entry["non_ac_units"]:
				continue
			entry["ac_units"] = 0
		entry["total_units"] = entry["ac_units"] + entry["non_ac_units"]
		rows.append(entry)
	rows.sort(key=lambda r: (r["full_name"] or "").lower())
	return rows
