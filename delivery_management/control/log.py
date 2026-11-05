"""Order Log: the logistic and money steps of each order, for the Order Control page.

Our services call ``add`` at the step itself (assign, send out, deliver, take off, bring back,
receive, POS return), in the same transaction: a step that fails leaves no row. Money is
logged from the documents (Sales Invoice payments, Payment Entry) so a payment made in the
plain ERPNext forms is in the log too. An order is its Sales Order; a branch sale without one
is its Sales Invoice.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, now_datetime

LOG = "Order Log"
NO_REMARKS = ("", "No Remarks")


def add(event: str, *, sales_order=None, sales_invoice=None, delivery_note=None, driver=None, amount=None,
		mode_of_payment=None, items=None, reference=None, note=None) -> None:
	if delivery_note and not sales_order:
		from delivery_management.drivers.delivery_note import sales_order_of

		sales_order = sales_order_of(frappe.get_doc("Delivery Note", delivery_note))
	party = frappe.db.get_value("Sales Order", sales_order, ["customer", "customer_name"], as_dict=True) if sales_order \
		else frappe.db.get_value("Sales Invoice", sales_invoice, ["customer", "customer_name"], as_dict=True) \
		if sales_invoice else None
	frappe.get_doc({
		"doctype": LOG, "event": event, "at": now_datetime(), "user": frappe.session.user, "driver": driver,
		"sales_order": sales_order, "sales_invoice": sales_invoice, "delivery_note": delivery_note,
		"customer": party and party.customer, "customer_name": party and party.customer_name,
		"amount": amount, "mode_of_payment": mode_of_payment, "items": items_text(items) if isinstance(items, list) else items,
		"reference_doctype": reference and reference[0], "reference_name": reference and reference[1], "note": note,
	}).insert(ignore_permissions=True)


def items_text(items: list) -> str | None:
	"""``[{item_name, qty, serials}]`` → "TV 65 × 1 (SN1, SN2)" one per line."""
	return "\n".join(
		f"{i.get('item_name') or i.get('item_code')} × {flt(i.get('qty')):g}"
		+ (f" ({', '.join(i['serials'])})" if i.get("serials") else "")
		for i in items
	) or None


def order_of_invoice(invoice: str) -> str | None:
	return frappe.db.get_value("Sales Invoice Item", {"parent": invoice, "sales_order": ["is", "set"]}, "sales_order")


# ---------------------------------------------------------------- document events


def on_sales_order_submit(doc, method=None):
	add("Order created", sales_order=doc.name, amount=flt(doc.rounded_total or doc.grand_total),
		items=[{"item_name": r.item_name, "qty": r.qty} for r in doc.items])


def on_sales_order_cancel(doc, method=None):
	add("Order cancelled", sales_order=doc.name)


def on_invoice_submit(doc, method=None):
	if doc.is_return:
		original = doc.return_against
		order = order_of_invoice(original) if original else None
		add("Credit note", sales_order=order, sales_invoice=original, amount=-flt(doc.rounded_total or doc.grand_total),
			items=[{"item_name": r.item_name, "qty": -flt(r.qty)} for r in doc.items], reference=("Sales Invoice", doc.name),
			note=None if (doc.remarks or "") in NO_REMARKS else doc.remarks)
		for p in doc.get("payments") or []:
			if flt(p.amount) < 0:
				add("Refund", sales_order=order, sales_invoice=original, amount=-flt(p.amount),
					mode_of_payment=p.mode_of_payment, reference=("Sales Invoice", doc.name))
		return
	order = order_of_invoice(doc.name)
	if not order:
		add("Store sale", sales_invoice=doc.name, amount=flt(doc.rounded_total or doc.grand_total),
			items=[{"item_name": r.item_name, "qty": r.qty} for r in doc.items], reference=("Sales Invoice", doc.name))
	paid = [p for p in doc.get("payments") or [] if flt(p.amount) > 0]
	change = flt(doc.get("change_amount"))
	for i, p in enumerate(paid):
		# The change given back comes out of the first (cash) payment.
		add("Payment", sales_order=order, sales_invoice=doc.name, amount=flt(p.amount) - (change if i == 0 else 0),
			mode_of_payment=p.mode_of_payment, reference=("Sales Invoice", doc.name))


def on_invoice_cancel(doc, method=None):
	invoice = doc.return_against if doc.is_return else doc.name
	add("Invoice cancelled", sales_order=order_of_invoice(invoice), sales_invoice=invoice,
		reference=("Sales Invoice", doc.name))


def _payment_target(doc) -> tuple[str | None, str | None]:
	for ref in doc.get("references") or []:
		if ref.reference_doctype == "Sales Order":
			return ref.reference_name, None
		if ref.reference_doctype == "Sales Invoice":
			invoice = frappe.db.get_value("Sales Invoice", ref.reference_name, "return_against") or ref.reference_name
			return order_of_invoice(invoice), invoice
	return None, None


def on_payment_submit(doc, method=None):
	if doc.party_type != "Customer":
		return
	order, invoice = _payment_target(doc)
	add("Payment" if doc.payment_type == "Receive" else "Refund", sales_order=order, sales_invoice=invoice,
		amount=flt(doc.paid_amount), mode_of_payment=doc.mode_of_payment, reference=("Payment Entry", doc.name),
		note=None if (doc.remarks or "") in NO_REMARKS else doc.remarks)


def on_payment_cancel(doc, method=None):
	if doc.party_type != "Customer":
		return
	order, invoice = _payment_target(doc)
	add("Payment cancelled", sales_order=order, sales_invoice=invoice, amount=flt(doc.paid_amount),
		mode_of_payment=doc.mode_of_payment, reference=("Payment Entry", doc.name))


def on_delivery(doc, method=None):
	"""Delivery Note ``on_submit``: the driver delivered (a return note is logged by its POS return)."""
	if doc.is_return:
		return
	add("Delivered", delivery_note=doc.name, driver=doc.get("dm_driver"),
		items=[{"item_name": r.item_name, "qty": r.qty, "serials": (r.serial_no or "").split()} for r in doc.items],
		reference=("Delivery Note", doc.name))
