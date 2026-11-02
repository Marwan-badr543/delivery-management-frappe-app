"""Endpoints of the Stock Keeper and Goods in Transit pages (``transit.service``).

The stock keeper (Stock User / Stock Manager) and the delivery office (System Manager /
Delivery Manager) send an order's goods out with its driver and receive them back.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

from delivery_management.api.drivers import format_address
from delivery_management.drivers import assignment as asg
from delivery_management.drivers.delivery_note import RETURNED
from delivery_management.transit import service as transit

ROLES = ("System Manager", "Delivery Manager", "Stock Manager", "Stock User")
DN = "Delivery Note"


def _require_stock_keeper():
	if frappe.session.user != "Administrator" and not set(ROLES) & set(frappe.get_roles()):
		frappe.throw(_("Only the stock keeper or the delivery office can do this."), frappe.PermissionError)


@frappe.whitelist()
def get_stores():
	_require_stock_keeper()
	main, transit_store = transit.stores()
	return {"main": main, "transit": transit_store}


# ---------------------------------------------------------------------------
# Send out
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_to_send(search=None, work_date=None):
	"""Draft order notes still in the store with a driver assigned, earliest first."""
	_require_stock_keeper()
	day = "coalesce(dn.dm_ac_installation_date, dn.dm_delivery_date)"
	conditions = ["dn.docstatus = 0", "dn.is_return = 0", "ifnull(dn.dm_delivery_status, '') = ''",
		"ifnull(dn.dm_driver, '') != ''"]
	values = {}
	if work_date:
		conditions.append(f"{day} = %(day)s")
		values["day"] = getdate(work_date)
	if search:
		conditions.append("(dn.name like %(q)s or dn.customer_name like %(q)s or dn.contact_mobile like %(q)s)")
		values["q"] = f"%{search.strip()}%"
	rows = frappe.db.sql(
		f"""select dn.name, dn.customer_name, dn.dm_delivery_type as order_type, dn.dm_driver as driver,
			dn.dm_delivery_area as area, dn.dm_block_no as block_no, dn.dm_road_no as road_no,
			dn.dm_building_no as building_no, dn.shipping_address, dn.address_display,
			{day} as work_date, dn.dm_delivery_time as delivery_time, dn.total_qty
		from `tabDelivery Note` dn where {' and '.join(conditions)}
		order by {day} is null, {day}, dn.dm_delivery_time, dn.name limit 200""",
		values,
		as_dict=True,
	)
	return [_row(r) for r in rows]


def _row(r) -> dict:
	r.address = format_address(r)
	r.driver_name = frappe.utils.get_fullname(r.driver) if r.driver else None
	r.delivery_time = r.delivery_time and str(r.delivery_time)
	for f in ("shipping_address", "address_display", "block_no", "road_no", "building_no"):
		r.pop(f, None)
	return r


@frappe.whitelist()
def get_note_to_send(delivery_note):
	"""The stock lines to hand over, and how many serials each needs."""
	_require_stock_keeper()
	dn = frappe.get_doc(DN, delivery_note)
	return {
		"name": dn.name, "customer_name": dn.customer_name, "order_type": dn.dm_delivery_type,
		"driver": dn.dm_driver, "driver_name": frappe.utils.get_fullname(dn.dm_driver) if dn.dm_driver else None,
		"status": dn.dm_delivery_status, "docstatus": dn.docstatus,
		"lines": [{"key": line.key, "item_code": line.item_code, "item_name": line.item_name, "qty": line.qty,
			"uom": line.stock_uom, "has_serial_no": line.has_serial_no, "bundle_item": line.bundle_item,
			"warehouse": line.warehouse} for line in transit.stock_lines(dn)],
	}


@frappe.whitelist()
def scan_serial(serial_no, warehouse):
	"""The item of a scanned serial in ``warehouse`` (the server checks the scans again)."""
	_require_stock_keeper()
	r = frappe.db.get_value("Serial No", (serial_no or "").strip(), ["name", "item_code", "warehouse", "status"],
		as_dict=True)
	if not r:
		return {"problem": _("Serial {0} does not exist.").format(serial_no)}
	if r.warehouse != warehouse or r.status != "Active":
		return {"problem": _("Serial {0} is not in {1}.").format(r.name, warehouse)}
	return {"serial_no": r.name, "item_code": r.item_code}


@frappe.whitelist(methods=["POST"])
def send_out(delivery_note, scans):
	_require_stock_keeper()
	scans = frappe.parse_json(scans) if isinstance(scans, str) else scans
	return {"transit": transit.dispatch(delivery_note, scans or {})}


# ---------------------------------------------------------------------------
# Receive back / Goods in Transit
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_transit(search=None, driver=None, status=None, coming_back_only=0):
	"""Every Delivery Transit with its lines: out, delivered, back in the store and still
	coming back."""
	_require_stock_keeper()
	filters = {}
	if driver:
		filters["driver"] = driver
	if status:
		filters["status"] = status
	or_filters = None
	if search:
		q = f"%{search.strip()}%"
		or_filters = {"name": ["like", q], "customer_name": ["like", q], "mobile_no": ["like", q],
			"driver_name": ["like", q]}
	names = frappe.get_all(transit.TRANSIT, filters=filters, or_filters=or_filters, pluck="name",
		order_by="out_at asc", limit=500)
	out = []
	for name in names:
		doc = frappe.get_doc(transit.TRANSIT, name)
		dn = frappe.get_doc(DN, doc.delivery_note)
		waiting = transit.pending(doc, dn)
		if frappe.utils.cint(coming_back_only) and not waiting:
			continue
		out.append({
			**{f: doc.get(f) for f in ("name", "delivery_note", "sales_order", "status", "delivery_type", "customer",
				"customer_name", "mobile_no", "address", "driver", "driver_name", "work_date", "out_at", "out_entry",
				"back_entries")},
			"out_by": frappe.utils.get_fullname(doc.out_by),
			"note_status": dn.dm_delivery_status, "whole_order_back": dn.dm_delivery_status == RETURNED,
			"items": [{"name": r.name, "item_code": r.item_code, "item_name": r.item_name, "uom": r.stock_uom,
				"bundle_item": r.bundle_item, "qty": flt(r.qty), "delivered_qty": flt(r.delivered_qty),
				"received_qty": flt(r.received_qty), "serial_no": r.serial_no,
				"coming_back": waiting[r.name].qty if r.name in waiting else 0,
				"coming_back_serials": waiting[r.name].serials if r.name in waiting else [],
				"has_serial_no": frappe.get_cached_value("Item", r.item_code, "has_serial_no")} for r in doc.items],
		})
	return out


@frappe.whitelist()
def get_drivers():
	_require_stock_keeper()
	return sorted({d.user: d for d in asg.drivers_for(asg.TYPE_NON_AC) + asg.drivers_for(asg.TYPE_AC)}.values(),
		key=lambda d: (d.full_name or "").lower())


@frappe.whitelist(methods=["POST"])
def receive(transit_name, scans, reserve_again=0):
	_require_stock_keeper()
	scans = frappe.parse_json(scans) if isinstance(scans, str) else scans
	return {"stock_entry": transit.receive(transit_name, scans or {}, bool(frappe.utils.cint(reserve_again)))}
