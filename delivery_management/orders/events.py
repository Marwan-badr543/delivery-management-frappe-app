"""Doc events that keep bookings and serials consistent with the documents."""

import frappe
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

from delivery_management.slots import release


def on_sales_order_cancel(doc, method=None):
	release(doc.doctype, doc.name)


def on_sales_order_trash(doc, method=None):
	release(doc.doctype, doc.name)


def copy_allocated_serials_to_delivery_note(doc, method=None):
	"""Put the serials picked for the Sales Order on its Delivery Note.

	ERPNext's mapper does not copy ``dm_serial_nos`` (a custom field of the order).
	Filling them here, before insert, keeps the order's serials and not a FIFO pick.
	"""
	_inherit_header_from_sales_order(doc)
	for item in doc.items:
		if item.serial_no or not item.get("so_detail"):
			continue
		allocated = frappe.db.get_value("Sales Order Item", item.so_detail, "dm_serial_nos")
		serials = get_serial_nos(allocated or "")
		if serials and len(serials) == int(item.stock_qty or item.qty or 0):
			item.serial_no = "\n".join(serials)


def copy_packed_serials_to_delivery_note(doc, method=None):
	"""Bundle components: copy the serials reserved on the Sales Order's packing list.

	Runs on validate, after ERPNext has (re)built ``packed_items``; rows that already
	have serials keep them.
	"""
	if doc.docstatus != 0 or not doc.get("packed_items"):
		return
	so_detail = {i.name: (i.get("against_sales_order"), i.get("so_detail")) for i in doc.items}
	for row in doc.packed_items:
		if row.serial_no:
			continue
		so, detail = so_detail.get(row.parent_detail_docname) or (None, None)
		if not (so and detail):
			continue
		reserved = frappe.db.get_value("Packed Item", {"parent": so, "parenttype": "Sales Order",
			"parent_detail_docname": detail, "item_code": row.item_code}, "serial_no")
		serials = get_serial_nos(reserved or "")
		if serials and len(serials) == int(row.qty or 0):
			row.serial_no = "\n".join(serials)
			row.use_serial_batch_fields = 1


def pick_packed_serials_for_branch_sale(doc, method=None):
	"""A POS branch Invoice (stock leaves the branch now) of a bundle: pick its components' serials."""
	if doc.docstatus != 0 or not doc.get("update_stock") or doc.get("is_return"):
		return
	if doc.get("dm_order_type") != "Invoice" or not doc.get("packed_items"):
		return
	from delivery_management.orders.stock import allocate_packed_serials

	allocate_packed_serials(doc)


def _inherit_header_from_sales_order(dn):
	"""Make a Delivery Note built from one POS order agree with that order.

	Kept from when the production server script built the Delivery Note with
	``customer = doc.customer_name`` (the name, not the ID) and default price list.
	The app's own Delivery Note (ERPNext mapper) already agrees; this still fixes a
	Delivery Note made by hand from one POS order. Only DNs of orders placed through
	this app (``dm_order_type`` set) are touched.
	"""
	orders = {i.get("against_sales_order") for i in dn.items if i.get("against_sales_order")}
	if len(orders) != 1:
		return
	so = frappe.db.get_value(
		"Sales Order",
		orders.pop(),
		["customer", "currency", "conversion_rate", "selling_price_list", "price_list_currency",
			"plc_conversion_rate", "dm_order_type", "shipping_address_name", "customer_address"],
		as_dict=True,
	)
	if not so or not so.dm_order_type:
		return
	dn.update({
		"customer": so.customer,
		"currency": so.currency,
		"conversion_rate": so.conversion_rate,
		"selling_price_list": so.selling_price_list,
		"price_list_currency": so.price_list_currency,
		"plc_conversion_rate": so.plc_conversion_rate,
		"customer_address": dn.customer_address or so.customer_address,
		"shipping_address_name": dn.shipping_address_name or so.shipping_address_name,
	})
