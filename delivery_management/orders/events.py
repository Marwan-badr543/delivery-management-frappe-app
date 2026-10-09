"""Doc events that keep bookings and serials consistent with the documents."""

import frappe
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

from delivery_management.orders.stock import set_serial_status
from delivery_management.slots import release


def on_sales_order_cancel(doc, method=None):
	release(doc.doctype, doc.name)
	_reactivate_reserved_serials(doc)


def on_sales_order_trash(doc, method=None):
	release(doc.doctype, doc.name)


def copy_allocated_serials_to_delivery_note(doc, method=None):
	"""Put the serials picked for the Sales Order on its Delivery Note.

	The production server script builds the Delivery Note without serials. Filling
	them here, before insert, also lets the existing after-insert hooks mark them
	Inactive as before.
	"""
	_inherit_header_from_sales_order(doc)
	for item in doc.items:
		if item.serial_no or not item.get("so_detail"):
			continue
		allocated = frappe.db.get_value("Sales Order Item", item.so_detail, "dm_serial_nos")
		serials = get_serial_nos(allocated or "")
		if serials and len(serials) == int(item.stock_qty or item.qty or 0):
			item.serial_no = "\n".join(serials)


def _inherit_header_from_sales_order(dn):
	"""Make a Delivery Note built from one POS order agree with that order.

	The production server script sets ``customer = doc.customer_name`` (the
	*name*, not the ID) and leaves currency and price list to the defaults. That
	breaks for two customers with the same name (IDs "Ali", "Ali-1") and for any
	non-default price list. This only applies to DNs of orders placed through
	this app (``dm_order_type`` set); other DNs are untouched.
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


def _reactivate_reserved_serials(so):
	"""Return the order's reserved serials to sale, unless another live document still holds them."""
	serials = [sn for item in so.items for sn in get_serial_nos(item.get("dm_serial_nos") or "")]
	if not serials:
		return
	still_held = set(
		frappe.db.sql_list(
			"""select sn.name from `tabSerial No` sn
			where sn.name in %(serials)s and (
				ifnull(sn.warehouse, '') = '' or sn.status != 'Inactive'
				or exists (select 1 from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
					where dn.docstatus < 2 and dni.serial_no like concat('%%', sn.name, '%%')))""",
			{"serials": tuple(serials)},
		)
	)
	set_serial_status([sn for sn in serials if sn not in still_held], "Active")
