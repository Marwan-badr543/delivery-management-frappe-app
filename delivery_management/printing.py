"""What the "Show Satellite Delivery Note" print format shows beyond the note's own fields
(Jinja method ``dm_delivery_note_print``)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from delivery_management.drivers import assignment as asg
from delivery_management.returns.service import _row_serials, net_qty

SIGNATURE_PREFIX = "signature_"


def dm_delivery_note_print(doc) -> dict:
	"""``driver``: who delivers / installs and their mobile. ``rows``: each line with what came
	back and what the customer received. ``returned``: the goods that came back, by return
	note. ``signature``: the customer's signature uploaded on delivery."""
	return {
		"driver": _driver(doc),
		"rows": [{"row": r, "serials": _row_serials(r), "returned": flt(r.qty) - net_qty(r), "received": net_qty(r)}
			for r in doc.items],
		"returned": [] if doc.is_return or doc.docstatus != 1 else _returned(doc.name),
		"signature": frappe.db.get_value("File", {"attached_to_doctype": "Delivery Note", "attached_to_name": doc.name,
			"file_name": ["like", f"{SIGNATURE_PREFIX}%"]}, "file_url", order_by="creation desc"),
	}


def _driver(doc) -> dict | None:
	if not doc.get("dm_driver"):
		return None
	user = frappe.db.get_value("User", doc.dm_driver, ["full_name", "mobile_no", "phone"], as_dict=True) or {}
	return {"label": _("AC Technician") if doc.get("dm_delivery_type") == asg.TYPE_AC else _("Driver"),
		"name": user.get("full_name") or doc.dm_driver, "mobile": user.get("mobile_no") or user.get("phone")}


def _returned(delivery_note: str) -> list[dict]:
	rows = []
	for name in frappe.get_all("Delivery Note", filters={"return_against": delivery_note, "docstatus": 1,
			"is_return": 1}, pluck="name", order_by="posting_date, creation"):
		ret = frappe.get_doc("Delivery Note", name)
		for r in ret.items:
			rows.append({"note": ret.name, "date": ret.posting_date, "item_code": r.item_code, "item_name": r.item_name,
				"qty": -flt(r.qty), "uom": r.uom, "serials": _row_serials(r),
				"reason": _("Returned by the customer") if ret.get("dm_customer_return") else _("Came back to the store")})
	return rows
