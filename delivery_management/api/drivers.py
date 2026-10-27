"""Endpoints of the Driver Assignment, Delivery Schedule and Installation Statistics pages.

Access (System Manager = admin in the old system):
* Driver Assignment: System Manager / Delivery Manager.
* Delivery Schedule: drivers see only the Delivery Notes assigned to them; System
  Manager / Delivery Manager see all.
* Installation Statistics: only System Manager sees everyone; others see their own numbers.
"""

from __future__ import annotations

import base64
import re
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, nowdate, strip_html

from delivery_management.drivers import assignment as asg
from delivery_management.drivers.delivery_note import DELIVERED, ON_THE_WAY, sales_order_of
from delivery_management.drivers import stats
from delivery_management.orders import payments

DN = "Delivery Note"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
SIGNATURE_PREFIX = "signature_"
MEDIA_EXTENSIONS = ("jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "mp4", "mov", "webm", "3gp", "mkv")


@contextmanager
def _with_full_rights():
	"""Run ERPNext code that re-checks desk permissions, after our own access check.

	``frappe.set_user`` also replaces the session id and data; they are put back, or
	the caller's browser would be logged out by the response cookie.
	"""
	session = frappe.local.session
	user, sid, data = session.user, session.sid, session.data
	form_dict = frappe.local.form_dict
	frappe.set_user("Administrator")
	try:
		yield user
	finally:
		frappe.set_user(user)
		frappe.local.session.sid = sid
		frappe.local.session.data = data
		frappe.local.form_dict = form_dict


def _require_manager():
	if not asg.can_manage():
		frappe.throw(_("Only a System Manager or Delivery Manager can do this."), frappe.PermissionError)


def _require_staff():
	if not (asg.can_manage() or asg.is_driver()):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_order_access(delivery_note: str) -> None:
	"""Managers: any Delivery Note. Drivers: only those assigned to them."""
	if asg.can_manage():
		return
	if asg.is_driver() and frappe.db.get_value(DN, delivery_note, "dm_driver") == frappe.session.user:
		return
	frappe.throw(_("This order is not assigned to you."), frappe.PermissionError)


def _type(order_type: str) -> str:
	if order_type not in (asg.TYPE_AC, asg.TYPE_NON_AC):
		frappe.throw(_("Unknown order type {0}").format(order_type))
	return order_type


def _date_column(order_type: str) -> str:
	"""AC orders are worked on their installation day."""
	if order_type == asg.TYPE_AC:
		return "coalesce(dn.dm_ac_installation_date, dn.dm_delivery_date)"
	return "dn.dm_delivery_date"


# ---------------------------------------------------------------------------
# Shared row building
# ---------------------------------------------------------------------------

_ROW_FIELDS = """dn.name, dn.customer, dn.customer_name, dn.contact_mobile, dn.docstatus, dn.status, dn.dm_delivery_status,
	dn.dm_delivery_type as order_type, dn.dm_driver as driver, dn.dm_delivery_area as area,
	dn.dm_delivery_date as delivery_date, dn.dm_delivery_time as delivery_time,
	dn.dm_ac_installation_date as ac_date, dn.dm_ac_units as ac_units, dn.dm_block_no as block_no,
	dn.dm_road_no as road_no, dn.dm_building_no as building_no, dn.shipping_address, dn.address_display,
	dn.grand_total, dn.posting_date"""


def format_address(row) -> str:
	parts = [row.get("area")]
	for label, field in ((_("Block"), "block_no"), (_("Road"), "road_no"), (_("Building"), "building_no")):
		if row.get(field):
			parts.append(f"{label} {row.get(field)}")
	parts = [p for p in parts if p]
	if len(parts) > 1:
		return ", ".join(parts)
	text = strip_html((row.get("shipping_address") or row.get("address_display") or "").replace("<br>", ", "))
	text = re.sub(r"\s*,\s*(,\s*)+", ", ", text).strip(" ,\n")
	return text or ", ".join(parts)


def _phones(rows) -> dict:
	customers = list({r.customer for r in rows if r.customer and not r.contact_mobile})
	if not customers:
		return {}
	return dict(frappe.get_all("Customer", filters={"name": ["in", customers]}, fields=["name", "mobile_no"],
		as_list=True))


def _full_names(users) -> dict:
	users = [u for u in set(users) if u]
	if not users:
		return {}
	return {u.name: u.full_name or u.name for u in
		frappe.get_all("User", filters={"name": ["in", users]}, fields=["name", "full_name"])}


def payment_summary(delivery_notes: list[str]) -> dict[str, dict]:
	"""Paid / outstanding of the Sales Invoice(s) of each Delivery Note's order.

	An unpaid order's invoice is a draft: all of it is outstanding. Goods returned are
	not paid money: the total is net of the order's Credit Notes.
	"""
	if not delivery_notes:
		return {}
	links = frappe.db.sql(
		"""select distinct dni.parent as dn, coalesce(nullif(dni.against_sales_invoice, ''), sii.parent) as si
		from `tabDelivery Note Item` dni
		left join `tabSales Invoice Item` sii on sii.sales_order = dni.against_sales_order
			and ifnull(dni.against_sales_order, '') != '' and sii.docstatus < 2
		where dni.parent in %(dns)s""",
		{"dns": tuple(delivery_notes)},
		as_dict=True,
	)
	invoices = list({l.si for l in links if l.si})
	si = {}
	if invoices:
		returned = dict(frappe.db.sql("""select return_against, sum(if(rounded_total = 0, grand_total, rounded_total))
			from `tabSales Invoice` where docstatus = 1 and is_return = 1 and return_against in %(si)s
			group by return_against""", {"si": invoices}))
		for r in frappe.get_all("Sales Invoice", filters={"name": ["in", invoices], "docstatus": ["<", 2],
				"is_return": 0}, fields=["name", "docstatus", "rounded_total", "grand_total", "outstanding_amount",
				"currency"]):
			r.total = flt(r.rounded_total or r.grand_total) + flt(returned.get(r.name))
			r.outstanding = max(flt(r.outstanding_amount), 0) if r.docstatus == 1 else r.total
			si[r.name] = r
	out = {}
	for dn in delivery_notes:
		rows = [si[l.si] for l in links if l.dn == dn and l.si in si]
		total = sum(r.total for r in rows)
		outstanding = sum(r.outstanding for r in rows)
		out[dn] = {
			"invoices": [r.name for r in rows],
			"total": total,
			"paid": max(total - outstanding, 0),
			"outstanding": outstanding,
			"currency": rows[0].currency if rows else None,
			"status": "No Invoice" if not rows else ("Paid" if outstanding <= 0.005 else
				("Unpaid" if outstanding >= total - 0.005 else "Partly Paid")),
		}
	return out


def _finish_rows(rows) -> list[dict]:
	phones = _phones(rows)
	names = _full_names(r.driver for r in rows)
	pay = payment_summary([r.name for r in rows])
	for r in rows:
		r.phone = r.contact_mobile or phones.get(r.customer)
		r.address = format_address(r)
		r.driver_name = names.get(r.driver)
		r.payment = pay.get(r.name)
		r.work_date = (r.ac_date or r.delivery_date) if r.order_type == asg.TYPE_AC else r.delivery_date
		r.delivery_time = r.delivery_time and str(r.delivery_time)
		for f in ("shipping_address", "address_display", "contact_mobile"):
			r.pop(f, None)
	return rows


# ---------------------------------------------------------------------------
# Driver Assignment page
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_areas():
	"""Area names for the page filters (drivers have no read access to Delivery Area)."""
	_require_staff()
	return frappe.get_all("Delivery Area", pluck="name", order_by="name asc", ignore_permissions=True)


@frappe.whitelist()
def get_drivers(order_type):
	_require_staff()
	return asg.drivers_for(_type(order_type))


@frappe.whitelist()
def get_assignment_orders(order_type, search=None, assigned_status=None, driver=None, area=None,
		delivery_date=None, limit=20, offset=0):
	"""Delivery Notes waiting for delivery (draft) of one type, earliest first."""
	_require_manager()
	order_type = _type(order_type)
	date_col = _date_column(order_type)
	conditions = ["dn.docstatus = 0", "dn.is_return = 0", "dn.dm_delivery_type = %(type)s"]
	values = {"type": order_type}
	if assigned_status == "assigned":
		conditions.append("ifnull(dn.dm_driver, '') != ''")
	elif assigned_status == "unassigned":
		conditions.append("ifnull(dn.dm_driver, '') = ''")
	if driver:
		conditions.append("dn.dm_driver = %(driver)s")
		values["driver"] = driver
	if area:
		conditions.append("dn.dm_delivery_area = %(area)s")
		values["area"] = area
	if delivery_date:
		conditions.append(f"{date_col} = %(day)s")
		values["day"] = getdate(delivery_date)
	if search:
		conditions.append("""(dn.name like %(search)s or dn.customer_name like %(search)s
			or dn.contact_mobile like %(search)s or dn.customer like %(search)s)""")
		values["search"] = f"%{search.strip()}%"
	where = " and ".join(conditions)
	total = frappe.db.sql(f"select count(*) from `tabDelivery Note` dn where {where}", values)[0][0]
	rows = frappe.db.sql(
		f"""select {_ROW_FIELDS} from `tabDelivery Note` dn where {where}
		order by {date_col} is null, {date_col} asc, dn.dm_delivery_time asc, dn.name asc
		limit %(limit)s offset %(offset)s""",
		{**values, "limit": cint(limit) or 20, "offset": cint(offset)},
		as_dict=True,
	)
	return {"rows": _finish_rows(rows), "total": total, "untyped": untyped_orders()}


def untyped_orders() -> list[dict]:
	"""Draft Delivery Notes with AC and non-AC items and no Order Type chosen yet."""
	return frappe.get_all(DN, filters={"docstatus": 0, "is_return": 0, "dm_delivery_type": ["in", ["", None]]},
		fields=["name", "customer_name", "posting_date"], order_by="posting_date asc", limit=50)


@frappe.whitelist()
def get_driver_options(delivery_note):
	_require_manager()
	return asg.availability(delivery_note)


@frappe.whitelist(methods=["POST"])
def assign_driver(delivery_note, driver=None):
	_require_manager()
	if not driver:
		return asg.unassign(delivery_note)
	return asg.assign(delivery_note, driver)


@frappe.whitelist()
def get_ac_loads(work_date):
	_require_manager()
	loads, global_max = asg.ac_loads(work_date)
	return {"work_date": getdate(work_date), "max_orders_per_day": global_max, "loads": loads}


@frappe.whitelist(methods=["POST"])
def set_ac_limit(driver, work_date, max_orders=None):
	_require_manager()
	asg.set_day_limit(driver, work_date, max_orders)
	return get_ac_loads(work_date)


# ---------------------------------------------------------------------------
# Delivery Schedule page (drivers)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_schedule(order_type, start_date=None, end_date=None, status=None, search=None, driver=None, area=None):
	"""Assigned Delivery Notes on the way or delivered in a date range. A driver gets only his own."""
	_require_staff()
	order_type = _type(order_type)
	date_col = _date_column(order_type)
	start = getdate(start_date or nowdate())
	end = getdate(end_date or add_months(start, 1))
	conditions = ["dn.docstatus = 1", "dn.is_return = 0", "dn.dm_delivery_type = %(type)s",
		"dn.dm_delivery_status in %(states)s", "ifnull(dn.dm_driver, '') != ''",
		f"{date_col} between %(start)s and %(end)s"]
	values = {"type": order_type, "start": start, "end": end, "states": (ON_THE_WAY, DELIVERED)}
	if not asg.can_manage():
		conditions.append("dn.dm_driver = %(me)s")
		values["me"] = frappe.session.user
	elif driver:
		conditions.append("dn.dm_driver = %(driver)s")
		values["driver"] = driver
	if status == "on_the_way":
		conditions.append("dn.dm_delivery_status = %(state)s")
		values["state"] = ON_THE_WAY
	elif status == "delivered":
		conditions.append("dn.dm_delivery_status = %(state)s")
		values["state"] = DELIVERED
	if area:
		conditions.append("dn.dm_delivery_area = %(area)s")
		values["area"] = area
	if search:
		conditions.append("(dn.name like %(search)s or dn.customer_name like %(search)s or dn.contact_mobile like %(search)s)")
		values["search"] = f"%{search.strip()}%"
	rows = frappe.db.sql(
		f"""select {_ROW_FIELDS} from `tabDelivery Note` dn where {' and '.join(conditions)}
		order by {date_col} asc, dn.dm_delivery_time asc, dn.name asc""",
		values,
		as_dict=True,
	)
	return {"rows": _finish_rows(rows), "start_date": start, "end_date": end, "can_manage": asg.can_manage()}


@frappe.whitelist()
def get_order_detail(delivery_note):
	_require_order_access(delivery_note)
	dn = frappe.get_doc(DN, delivery_note)
	row = frappe.db.sql(f"select {_ROW_FIELDS} from `tabDelivery Note` dn where dn.name = %s", delivery_note,
		as_dict=True)[0]
	row = _finish_rows([row])[0]
	row.items = [{"item_code": i.item_code, "item_name": i.item_name, "qty": i.qty, "uom": i.uom,
		"serial_no": i.serial_no} for i in dn.items]
	row.packed_items = [{"parent_item": p.parent_item, "item_code": p.item_code, "item_name": p.item_name,
		"qty": p.qty} for p in (dn.get("packed_items") or [])]
	row.sales_order = sales_order_of(dn)
	row.notes = frappe.db.get_value("Sales Order", row.sales_order, "posa_notes") if row.sales_order and \
		frappe.db.has_column("Sales Order", "posa_notes") else None
	row.attachments = frappe.get_all("File", filters={"attached_to_doctype": DN, "attached_to_name": dn.name},
		fields=["file_name", "file_url", "creation"], order_by="creation desc")
	row.modes_of_payment = modes_of_payment(dn.company)
	row.print_format = frappe.db.get_value("Property Setter", {"doc_type": DN, "property": "default_print_format"},
		"value") or "Standard"
	return row


def modes_of_payment(company) -> list[str]:
	return frappe.get_all("Mode of Payment Account", filters={"company": company, "default_account": ["is", "set"],
		"parenttype": "Mode of Payment"}, pluck="parent", distinct=True, order_by="parent asc")


@frappe.whitelist(methods=["POST"])
def record_payment(delivery_note, amount, mode_of_payment):
	"""Payment Entry for the order's invoice (the old "Record Payment" on delivery). An
	unpaid order's draft invoice is submitted first."""
	_require_order_access(delivery_note)
	summary = payment_summary([delivery_note]).get(delivery_note, {})
	if not summary.get("invoices"):
		frappe.throw(_("No Sales Invoice is linked to this order."))
	if summary["outstanding"] <= 0.005:
		frappe.throw(_("This order is already fully paid."))
	si = next(name for name in summary["invoices"] if frappe.db.get_value("Sales Invoice", name, "docstatus") == 0
		or flt(frappe.db.get_value("Sales Invoice", name, "outstanding_amount")) > 0)

	# ERPNext / HRMS check desk permissions on the invoice while building and validating
	# the Payment Entry; a driver has none on invoices. His access to this order was
	# checked above, so the entry is made with full rights and then credited to him.
	with _with_full_rights() as user:
		pe = payments.collect(si, amount, mode_of_payment, delivery_note,
			_("Collected on delivery of {0} by {1}").format(delivery_note, user))
		frappe.db.set_value("Payment Entry", pe.name, {"owner": user, "modified_by": user}, update_modified=False)
	return {"payment_entry": pe.name, "payment": payment_summary([delivery_note])[delivery_note]}


@frappe.whitelist(methods=["POST"])
def upload_attachment(delivery_note, filename, filedata):
	"""Attach a photo / file or the customer's signature to the Delivery Note."""
	_require_order_access(delivery_note)
	if "," in filedata[:100]:
		filedata = filedata.split(",", 1)[1]
	content = base64.b64decode(filedata)
	if len(content) > MAX_ATTACHMENT_BYTES:
		frappe.throw(_("The file is too large (max 25 MB)."))
	file = frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"attached_to_doctype": DN,
		"attached_to_name": delivery_note,
		"content": content,
		"is_private": 1,
	})
	file.flags.ignore_permissions = True
	file.insert()
	return {"file_url": file.file_url, "attached_to": f"{DN} {delivery_note}"}


@frappe.whitelist(methods=["POST"])
def mark_delivered(delivery_note):
	"""The driver confirms an On the way Delivery Note Delivered, after uploading a photo
	or video and the customer's signature (the office uses ``mark_delivered_by_office``)."""
	_require_order_access(delivery_note)
	_require_on_the_way(delivery_note)
	files = frappe.get_all("File", filters={"attached_to_doctype": DN, "attached_to_name": delivery_note},
		pluck="file_name")
	signed = any(f.startswith(SIGNATURE_PREFIX) for f in files)
	media = any(not f.startswith(SIGNATURE_PREFIX) and f.rsplit(".", 1)[-1].lower() in MEDIA_EXTENSIONS
		for f in files)
	missing = ([] if media else [_("a photo or video of the delivery")]) + \
		([] if signed else [_("the customer's signature")])
	if missing:
		frappe.throw(_("Upload {0} first.").format(_(" and ").join(missing)), title=_("Not delivered yet"))

	return _set_delivered(delivery_note)


@frappe.whitelist(methods=["POST"])
def mark_delivered_by_office(delivery_note):
	"""Delivered from the Delivery Note form (System Manager / Delivery Manager), without the
	driver's photo and signature."""
	if not asg.can_manage():
		frappe.throw(_("Only a Delivery Manager can mark an order delivered from the office."), frappe.PermissionError)
	_require_on_the_way(delivery_note)
	return _set_delivered(delivery_note)


def _require_on_the_way(delivery_note):
	if frappe.db.get_value(DN, delivery_note, "dm_delivery_status") != ON_THE_WAY:
		frappe.throw(_("Only an order that is On the way can be marked as delivered."))


def _set_delivered(delivery_note):
	frappe.db.set_value(DN, delivery_note, "dm_delivery_status", DELIVERED)
	return {"dm_delivery_status": DELIVERED}


# ---------------------------------------------------------------------------
# Installation Statistics page
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_installation_stats(start_date, end_date, technician=None, unit="all"):
	_require_staff()
	see_all = asg.is_admin()
	if not see_all:
		# Only a System Manager sees everyone; any other user sees only his own numbers.
		if technician and technician != frappe.session.user:
			frappe.throw(_("You can only view your own installation statistics."), frappe.PermissionError)
		technician = frappe.session.user
	return {
		"rows": stats.installation_stats(getdate(start_date), getdate(end_date), technician, unit),
		"technicians": [] if not see_all else sorted(
			{d.user: d for d in asg.drivers_for(asg.TYPE_NON_AC) + asg.drivers_for(asg.TYPE_AC)}.values(),
			key=lambda d: (d.full_name or "").lower()),
		"can_manage": see_all,
	}
