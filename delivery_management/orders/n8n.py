"""Send POS sales to the n8n webhook (customer messages), like the old system.

Every minute, POS Sales Invoices (Invoice or Order) submitted more than the delay
ago (Delivery Settings, default 5 minutes, so late edits are in) and not sent yet
are posted to the webhook as form data, with the invoice PDF and, for an Order,
the Delivery Note PDF. ``dm_n8n_sent`` is ticked only after n8n answers OK, so a
failed send is tried again the next minute (for one day at most).
"""

from __future__ import annotations

import requests

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, format_time, now_datetime

from delivery_management.orders import tv_accounts
from delivery_management.orders.service import ORDER_TYPE_ORDER
from delivery_management.slots.cart import KIND_AC, classify_items

SENT_FIELD = "dm_n8n_sent"
GIVE_UP_AFTER_HOURS = 24


def send_pending() -> None:
	"""Scheduler job (every minute)."""
	settings = frappe.get_cached_doc("Delivery Settings")
	url = settings.get("n8n_webhook_url")
	if not url:
		return
	now = now_datetime()
	delay = cint(settings.get("n8n_delay_minutes"))
	pending = frappe.get_all("Sales Invoice", pluck="name", order_by="creation", filters={
		"docstatus": 1, "is_return": 0, SENT_FIELD: 0, "dm_order_type": ["is", "set"],
		"creation": ["between", [add_to_date(now, hours=-GIVE_UP_AFTER_HOURS), add_to_date(now, minutes=-delay)]],
	})
	for name in pending:
		try:
			send(name, url)
		except Exception:
			frappe.log_error(_("Sales Invoice {0} not sent to n8n").format(name),
				reference_doctype="Sales Invoice", reference_name=name)
		frappe.db.commit()


def send(name: str, url: str) -> None:
	doc = frappe.get_doc("Sales Invoice", name)
	files = {"invoice_file_pdf": (f"INV-{name}.pdf", _pdf("Sales Invoice", name), "application/pdf")}
	delivery_note = _delivery_note(doc)
	if delivery_note:
		files["dn_file_pdf"] = (f"DN-{delivery_note}.pdf", _pdf("Delivery Note", delivery_note), "application/pdf")
	response = requests.post(url, data=payload(doc), files=files, timeout=60)
	response.raise_for_status()
	doc.db_set(SENT_FIELD, 1, update_modified=False)


def payload(doc) -> dict[str, str]:
	"""The old system's fields, every value a string."""
	is_order = doc.dm_order_type == ORDER_TYPE_ORDER
	paid = [p.mode_of_payment for p in doc.payments if p.amount] or [p.mode_of_payment for p in doc.payments]
	data = {
		"customer_phone": _phone(doc.contact_mobile),
		"customer_name": doc.customer_name,
		"total": doc.total,
		"modified_by": doc.modified_by,
		"creation_date": doc.posting_date,
		"creation_time": doc.posting_time,
		"invoice_number": doc.name,
		"status": doc.status,
		"territory": doc.territory,
		"pos_profile": doc.pos_profile,
		"xtream_info": doc.posa_notes,
		"notes": doc.posa_notes,
		"products": ", ".join(row.item_code for row in doc.items),
		"quantity": ", ".join(str(row.qty) for row in doc.items),
		"payment_method": paid[0] if paid else "",
		"item_categories": str(_categories(doc)),
		"delivery_method": "delivery" if is_order else "pickup",
		"branch": "" if is_order else frappe.db.get_value("Warehouse", doc.set_warehouse, "warehouse_name"),
		"address": doc.dm_delivery_area or doc.address_display,
		"area": doc.dm_delivery_area,
		"location": "",
		"block_no": doc.dm_block_no,
		"road_no": doc.dm_road_no,
		"building_no": doc.dm_building_no,
		"delivery_date": doc.dm_delivery_date or doc.dm_ac_installation_date,
		"delivery_time": format_time(doc.dm_delivery_time, "hh:mm a") if doc.dm_delivery_time else "",
	}
	return {k: "" if v is None else str(v) for k, v in data.items()}


def _phone(mobile: str | None) -> str:
	digits = "".join(c for c in (mobile or "") if c.isdigit())
	if not digits:
		return ""
	return digits if digits.startswith("973") and len(digits) > 8 else f"973{digits}"


def _categories(doc) -> list[str]:
	codes = [row.item_code for row in doc.items]
	categories = []
	if KIND_AC in classify_items(codes).values():
		categories.append("AC")
	if tv_accounts.get_tv_item_codes(codes):
		categories.append("TV")
	return categories


def _delivery_note(doc) -> str | None:
	orders = {row.sales_order for row in doc.items if row.sales_order}
	if not orders:
		return None
	notes = frappe.get_all("Delivery Note Item", pluck="parent", order_by="creation", limit=1,
		filters={"against_sales_order": ["in", list(orders)], "docstatus": ["<", 2]})
	return notes[0] if notes else None


def _pdf(doctype: str, name: str) -> bytes:
	return frappe.get_print(doctype, name, as_pdf=True)
