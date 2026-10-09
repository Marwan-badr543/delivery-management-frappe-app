"""Turn a POS Awesome payload into ERPNext documents.

``Invoice``: the customer takes the goods at the branch.
    One Sales Invoice with ``update_stock = 1`` from the branch warehouse.
    Serials are picked automatically from that warehouse. No delivery slot.

``Order``: delivery to the customer.
    1. A Sales Order from the main store, with serials picked and reserved
       (marked Inactive). On submit, the existing server script creates the
       draft Delivery Note, and ``events.copy_allocated_serials_to_delivery_note``
       puts the same serials on it.
    2. The delivery slot and/or AC capacity, booked against the Sales Order.
    3. The POS Sales Invoice (``update_stock = 0``), linked line by line to the
       Sales Order with the same serials, then paid and submitted by POS
       Awesome's own ``submit_invoice``, so payments, credit and change behave
       exactly as before.

Everything runs in the request transaction. If any step fails (slot just got
full, serial gone, payment error), none of the documents or bookings persist.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from delivery_management.orders import address as address_utils
from delivery_management.orders.stock import (
	MODE_INVOICE,
	MODE_ORDER,
	allocate_serials,
	assert_in_stock,
	set_serial_status,
)
from delivery_management.slots import profile_cart
from delivery_management.slots.settings import get_settings
from delivery_management.slots.timeutils import to_time_str

ORDER_TYPE_INVOICE = "Invoice"
ORDER_TYPE_ORDER = "Order"


def build_booking(items, delivery: dict | None) -> tuple[dict, dict]:
	"""Validate the chosen schedule against the cart.

	Returns ``(cart_profile_dict, reserve_kwargs)``. ``reserve_kwargs`` is empty
	when the cart needs no capacity (e.g. services only).
	"""
	delivery = delivery or {}
	profile = profile_cart(items)
	booking: dict = {}

	if profile.has_non_ac:
		missing = [label for key, label in (("area", _("Area")), ("delivery_date", _("Delivery Date")),
			("delivery_time", _("Delivery Time"))) if not delivery.get(key)]
		if missing:
			frappe.throw(_("Please choose the delivery {0}.").format(", ".join(missing)), title=_("Delivery Slot"))
		booking.update(
			area=delivery["area"],
			delivery_date=getdate(delivery["delivery_date"]),
			delivery_time=to_time_str(delivery["delivery_time"]),
		)
	if profile.has_ac:
		if not delivery.get("ac_date"):
			frappe.throw(_("Please choose the AC installation date."), title=_("AC Installation"))
		booking.update(ac_date=getdate(delivery["ac_date"]), ac_units=profile.ac_units)
	return profile.as_dict(), booking


# ---------------------------------------------------------------------------
# Invoice (branch pickup)
# ---------------------------------------------------------------------------


def submit_pos_invoice(invoice: dict, data: dict, delivery: dict | None = None) -> dict:
	warehouse = frappe.db.get_value("POS Profile", invoice.get("pos_profile"), "warehouse")
	if not warehouse:
		frappe.throw(_("POS Profile {0} has no warehouse.").format(invoice.get("pos_profile")))

	if cint(invoice.get("is_return")):
		# Returns keep POS Awesome's own logic: they follow the original invoice's
		# stock path (a returned delivery Order must not receive stock into the branch).
		return _submit_with_posawesome(invoice, data)

	items = invoice.get("items") or []
	for row in items:
		row["warehouse"] = warehouse
		_drop_client_serials(row)
	invoice.update(set_warehouse=warehouse, update_stock=1, posa_delivery_date=None, dm_order_type=ORDER_TYPE_INVOICE)

	assert_in_stock(items, warehouse, MODE_INVOICE)
	allocate_serials(items, warehouse)
	_apply_address(invoice, delivery)
	invoice["sales_team"] = sales_team_of_current_user()
	return _submit_with_posawesome(invoice, data)


# ---------------------------------------------------------------------------
# Order (delivery)
# ---------------------------------------------------------------------------


def submit_pos_order(invoice: dict, data: dict, delivery: dict | None, unpaid: bool = False) -> dict:
	"""``unpaid``: the POS "Save/New" button. Everything is submitted the same way,
	but no payment is taken; the invoice stays Unpaid (the usual case: the customer
	pays on delivery)."""
	if cint(invoice.get("is_return")):
		frappe.throw(_("Returns cannot be placed as delivery Orders."))
	delivery = delivery or {}
	main_store = get_settings().main_warehouse
	if not main_store:
		frappe.throw(_("Set the Main Store in Delivery Settings first."))
	store_company = frappe.db.get_value("Warehouse", main_store, "company")
	if store_company != invoice.get("company"):
		frappe.throw(
			_("POS Profile {0} belongs to company {1}, but the main store {2} belongs to {3}. "
				"Use a POS Profile of {3} for delivery Orders.").format(
				invoice.get("pos_profile"), invoice.get("company"), main_store, store_company),
			title=_("Wrong POS Profile"),
		)

	items = invoice.get("items") or []
	if not items:
		frappe.throw(_("There are no items."))

	profile, booking = build_booking(items, delivery)
	assert_in_stock(items, main_store, MODE_ORDER)
	address_name = _apply_address(invoice, delivery, required=bool(booking))

	invoice["sales_team"] = sales_team_of_current_user()
	so = _make_sales_order(invoice, items, main_store, booking, profile, address_name, delivery)
	allocate_serials(so.items, main_store, target_field="dm_serial_nos")
	so.insert(ignore_permissions=True)
	set_serial_status(_serials_of(so.items, "dm_serial_nos"), "Inactive")
	# Books the slot / AC day from the schedule fields (sales_order.before_submit).
	with quiet_messages():
		so.submit()

	# so.items were built from ``items`` in the same order, so index i matches i.
	for row, so_item in zip(items, so.items):
		_drop_client_serials(row)
		row.update(
			sales_order=so.name,
			so_detail=so_item.name,
			warehouse=main_store,
			serial_no=so_item.dm_serial_nos or None,
		)
	invoice.update(
		update_stock=0,
		set_warehouse=main_store,
		posa_delivery_date=None,
		dm_order_type=ORDER_TYPE_ORDER,
		**_schedule_fields(booking, profile),
	)
	if unpaid:
		_make_unpaid(invoice, data)
	result = _submit_with_posawesome(invoice, data)
	result["sales_order"] = so.name
	return result


def _make_sales_order(invoice, items, warehouse, booking, profile, address_name, delivery):
	dates = [d for d in (booking.get("delivery_date"), booking.get("ac_date")) if d]
	delivery_date = max(dates) if dates else getdate(nowdate())
	so = frappe.new_doc("Sales Order")
	so.update(
		{
			"customer": invoice.get("customer"),
			"company": invoice.get("company"),
			"transaction_date": invoice.get("posting_date") or nowdate(),
			"delivery_date": delivery_date,
			"order_type": "Sales",
			"currency": invoice.get("currency"),
			"selling_price_list": invoice.get("selling_price_list"),
			"set_warehouse": warehouse,
			# Rates come from the POS screen; do not let pricing rules re-price them.
			"ignore_pricing_rule": 1,
			"taxes_and_charges": invoice.get("taxes_and_charges"),
			"apply_discount_on": invoice.get("apply_discount_on") or "Grand Total",
			"additional_discount_percentage": flt(invoice.get("additional_discount_percentage")),
			"discount_amount": flt(invoice.get("discount_amount")),
			"customer_address": address_name,
			"shipping_address_name": address_name,
			"posa_notes": invoice.get("posa_notes"),
			"dm_order_type": ORDER_TYPE_ORDER,
			**_address_fields(delivery),
			**_schedule_fields(booking, profile),
		}
	)
	for row in items:
		so.append(
			"items",
			{
				"item_code": row.get("item_code"),
				"qty": flt(row.get("qty")),
				"uom": row.get("uom"),
				"conversion_factor": flt(row.get("conversion_factor")) or 1,
				"price_list_rate": flt(row.get("price_list_rate")),
				"discount_percentage": flt(row.get("discount_percentage")),
				"discount_amount": flt(row.get("discount_amount")),
				"rate": flt(row.get("rate")),
				"delivery_date": delivery_date,
				"warehouse": warehouse,
				"posa_row_id": row.get("posa_row_id"),
				"posa_notes": row.get("posa_notes"),
			},
		)
	for row in invoice.get("sales_team") or []:
		so.append("sales_team", dict(row))
	for tax in invoice.get("taxes") or []:
		so.append(
			"taxes",
			{
				k: tax.get(k)
				for k in ("charge_type", "row_id", "account_head", "description", "rate", "cost_center",
					"included_in_print_rate", "tax_amount")
			},
		)
	return so


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def already_submitted(invoice: dict, data: dict | None = None) -> dict | None:
	"""A retried request (network drop, double click) for a sale that already went through.

	POS Awesome V15 replays a submitted invoice by its request id, but an Order would
	first get a second Sales Order and slot booking here. Return the first result instead.
	"""
	data = data or {}
	request_id = (invoice.get("posa_client_request_id") or data.get("client_request_id")
		or data.get("idempotency_key"))
	name = None
	if request_id and frappe.db.has_column("Sales Invoice", "posa_client_request_id"):
		name = frappe.db.get_value("Sales Invoice", {"posa_client_request_id": request_id, "docstatus": 1}, "name")
	if not name and invoice.get("name"):
		name = frappe.db.get_value("Sales Invoice", {"name": invoice["name"], "docstatus": 1}, "name")
	if not name:
		return None
	sales_order = frappe.db.get_value("Sales Invoice Item", {"parent": name, "sales_order": ["is", "set"]},
		"sales_order")
	result = {"name": name, "status": 1, "docstatus": 1, "doctype": "Sales Invoice", "replayed": True}
	if sales_order:
		result["sales_order"] = sales_order
	return result


def sales_team_of_current_user() -> list[dict]:
	"""The salesman is whoever places the sale in the POS (no manual choice).

	The logged-in user is always recorded as the document owner. When the user is
	linked to an Employee (Employee.user_id), and that Employee to a Sales Person,
	the Sales Person also gets 100% on the order and invoice, for sales reports.
	"""
	employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user, "status": "Active"}, "name")
	sales_person = employee and frappe.db.get_value(
		"Sales Person", {"employee": employee, "enabled": 1, "is_group": 0}, "name"
	)
	return [{"sales_person": sales_person, "allocated_percentage": 100}] if sales_person else []


def _apply_address(invoice: dict, delivery: dict | None, required: bool = False) -> str | None:
	values = address_utils.normalise(delivery)
	if required and not address_utils.is_complete(values):
		frappe.throw(
			_("Area, Block No, Road No and Building No are required for delivery orders."),
			title=_("Delivery Address"),
		)
	if address_utils.is_empty(values):
		return None
	name = address_utils.upsert_customer_address(invoice.get("customer"), delivery)
	invoice.update(customer_address=name, shipping_address_name=name, **_address_fields(delivery))
	return name


class quiet_messages:
	"""Drop informational pop-ups raised during a POS submit.

	The production server script "Delivery Note From Sales Order" calls
	``frappe.msgprint("Delivery Note ... has been created")``. In the POS that desk
	dialog fights POS Awesome V15 for keyboard focus and freezes the browser. Errors
	still raise as usual; only the informational messages are removed.
	"""

	def __enter__(self):
		self.start = len(frappe.local.message_log or [])

	def __exit__(self, exc_type, exc, tb):
		if exc_type is None and frappe.local.message_log:
			del frappe.local.message_log[self.start:]
		return False


def _drop_client_serials(row: dict) -> None:
	"""Serials are picked here, never by the POS: POS Awesome V15 pre-fills serials from
	its own list, which knows nothing about orders that reserved them (Inactive)."""
	# Set to empty (not removed): the saved draft may already hold serials in the database.
	row.update(serial_no=None, serial_and_batch_bundle=None, use_serial_batch_fields=1)
	row.pop("serial_no_selected", None)


def _make_unpaid(invoice: dict, data: dict) -> None:
	"""No money taken now: zero every payment row and drop change/credit handling."""
	for payment in invoice.get("payments") or []:
		payment["amount"] = 0
		payment["base_amount"] = 0
	invoice.update(paid_amount=0, base_paid_amount=0, change_amount=0, base_change_amount=0,
		write_off_amount=0, base_write_off_amount=0)
	if not invoice.get("due_date") or getdate(invoice["due_date"]) < getdate(invoice.get("posting_date") or nowdate()):
		invoice["due_date"] = invoice.get("posting_date") or nowdate()
	for key in ("credit_change", "redeemed_customer_credit", "customer_credit_dict"):
		data.pop(key, None)


def _address_fields(delivery: dict | None) -> dict:
	values = address_utils.normalise(delivery)
	return {
		"dm_delivery_area": values["dm_delivery_area"],
		"dm_block_no": values["dm_block_no"],
		"dm_road_no": values["dm_road_no"],
		"dm_building_no": values["dm_building_no"],
	}


def _schedule_fields(booking: dict, profile: dict) -> dict:
	"""The booked schedule. The area is left out when there is no delivery slot
	(AC only), so the address area from ``_address_fields`` stays."""
	fields = {
		"dm_delivery_area": booking.get("area"),
		"dm_delivery_date": booking.get("delivery_date"),
		"dm_delivery_time": booking.get("delivery_time"),
		"dm_ac_installation_date": booking.get("ac_date"),
		"dm_ac_units": cint(profile.get("ac_units")),
	}
	if not fields["dm_delivery_area"]:
		del fields["dm_delivery_area"]
	return fields


def _serials_of(rows, field: str) -> list[str]:
	from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

	return [sn for row in rows for sn in get_serial_nos(row.get(field) or "")]


def _submit_with_posawesome(invoice: dict, data: dict) -> dict:
	from posawesome.posawesome.api.invoice_processing.creation import submit_invoice

	# Always synchronous (submit_in_background=False): the picked serials and the booked
	# slot must commit together with a *submitted* invoice. In background mode the
	# invoice would stay a draft and another sale could take the same serial meanwhile.
	return submit_invoice(
		json.dumps(invoice, default=str), json.dumps(data or {}, default=str), submit_in_background=False
	)
