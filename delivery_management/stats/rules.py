"""Sales Commission Rules on the User: one row per month (targets and commission range)."""

import frappe
from frappe import _
from frappe.utils import flt, getdate


def validate_user_rules(doc, method=None):
	"""User ``validate``: each rule is a whole month, no month twice, Min ≤ Max."""
	seen = {}
	for row in doc.get("dm_commission_rules") or []:
		if not row.month:
			continue
		row.month = getdate(row.month).replace(day=1)
		if row.month in seen:
			frappe.throw(_("Commission Rules: row {0} and row {1} are for the same month ({2}). One row per month.")
				.format(seen[row.month], row.idx, row.month.strftime("%B %Y")))
		seen[row.month] = row.idx
		low, high = flt(row.min_percent), flt(row.max_percent)
		if low < 0 or high > 100:
			frappe.throw(_("Commission Rules row {0}: Commission % must be between 0 and 100.").format(row.idx))
		if low > high:
			frappe.throw(_("Commission Rules row {0}: Min Commission % cannot be more than Max.").format(row.idx))
	rows = doc.get("dm_commission_rules") or []
	rows.sort(key=lambda r: getdate(r.month) if r.month else getdate("1900-01-01"))
	for i, row in enumerate(rows, 1):
		row.idx = i
