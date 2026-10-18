"""Doc events that keep bookings consistent with the documents."""

import frappe
from delivery_management.slots import release


def on_sales_order_cancel(doc, method=None):
	release(doc.doctype, doc.name)


def on_sales_order_trash(doc, method=None):
	release(doc.doctype, doc.name)


def inherit_header_from_sales_order(dn, method=None):
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
