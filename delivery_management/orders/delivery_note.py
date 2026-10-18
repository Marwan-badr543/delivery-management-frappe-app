"""Draft Delivery Note for every submitted Sales Order.

Replaces the production server script "Delivery Note From Sales Order" (disabled by
the ``disable_replaced_server_scripts`` patch). Differences from the script:

* Built with ERPNext's own mapper (``make_delivery_note``), so the Delivery Note gets
  the customer *ID* (the script used the customer name, which broke for two customers
  with the same name), the order's price list, currency, taxes, addresses and the
  ``against_sales_order`` / ``so_detail`` links that ERPNext uses to track delivery.
* Made once per order: nothing is made when the order already has a Delivery Note
  (draft or submitted), or when nothing is left to deliver.
* Controlled from Delivery Settings (on/off, and limited to one company if set).
* An order with AC units *and* TV / other goods gets **two** Delivery Notes: one
  ``AC`` note (AC units, their accessories) for the AC Tech on the installation day,
  and one ``Non AC`` note (the other goods, delivery charge, services) for the TV
  Driver in the delivery slot. Each note carries only its own schedule.

The schedule / Order Type are put on it by ``drivers.delivery_note.before_insert``;
ERPNext picks the serials reserved for the order.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from delivery_management.drivers import assignment as asg
from delivery_management.drivers.delivery_note import OTHER_TYPE_FIELDS
from delivery_management.slots import profile_cart
from delivery_management.slots.settings import get_settings


def create_from_sales_order(doc, method=None):
	"""Sales Order ``on_submit``. Returns the names of the Delivery Notes made."""
	if not should_create(doc):
		return []
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	dn = make_delivery_note(doc.name, kwargs={"for_reserved_stock": False, "skip_item_mapping": False})
	if not dn.get("items"):
		return []
	names = []
	for note in split_by_order_type(dn):
		note.flags.ignore_permissions = True
		note.insert()
		names.append(note.name)
	frappe.msgprint(
		_("Delivery Note {0} has been created for {1}.").format(
			", ".join(frappe.utils.get_link_to_form("Delivery Note", n) for n in names),
			frappe.bold(doc.customer_name or doc.customer),
		),
		indicator="green",
		alert=True,
	)
	return names


def split_by_order_type(dn) -> list:
	"""``[dn]``, or ``[ac_note, non_ac_note]`` when the lines mix AC units with other goods.

	AC note: lines with AC units (a bundle holding an AC unit too) and AC accessories.
	Non AC note: everything else (TV / other stock goods, delivery charge, services).
	"""
	if asg.order_type_of(dn.items):
		return [dn]
	ac_rows, other_rows = [], []
	for row in dn.items:
		profile = profile_cart([row])
		(ac_rows if profile.has_ac or profile.accessory_items else other_rows).append(row)
	if not ac_rows or not other_rows:
		return [dn]
	ac_note = frappe.copy_doc(dn)
	notes = []
	for note, rows, order_type in ((ac_note, ac_rows, asg.TYPE_AC), (dn, other_rows, asg.TYPE_NON_AC)):
		keep = {r.so_detail for r in rows}
		note.set("items", [r for r in note.items if r.so_detail in keep])
		note.set("packed_items", [])  # rebuilt from the remaining lines on validate
		for i, r in enumerate(note.items, 1):
			r.idx = i
		note.dm_delivery_type = order_type
		for field in OTHER_TYPE_FIELDS[order_type]:
			note.set(field, None)
		notes.append(note)
	return notes


def should_create(so) -> bool:
	settings = get_settings()
	if not settings.auto_create_delivery_note:
		return False
	if settings.auto_delivery_note_company and so.company != settings.auto_delivery_note_company:
		return False
	if cint(so.get("skip_delivery_note")):
		return False
	return not frappe.db.sql(
		"""select 1 from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
		where dni.against_sales_order = %s and dn.docstatus < 2 limit 1""",
		so.name,
	)
