"""Serial reservation: a serial in a warehouse is ``Inactive`` while an open document holds it.

Replaces the production server scripts "invalidate serial after insert delivery note" and
"active serial after delete delivery note". ERPNext ignores the status; the app picks and
counts only ``Active`` serials (``orders.stock``), so a held serial is never sold twice.

Holders of a serial:

* a **draft Delivery Note** (item rows, bundle components, or a draft Serial and Batch
  Bundle of it);
* a **submitted Sales Order** that has no Delivery Note yet (its ``dm_serial_nos`` and
  packed bundle serials). Once a Delivery Note exists, the Delivery Note is the holder.

The status is never set from one event alone: after every change the serials involved
(before and after the change) are re-checked against all holders (:func:`sync`), so a
row removed from a draft makes its serial Active again and a new row makes its serial
Inactive. Serials that left the warehouse (delivered) are not touched.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

DN = "Delivery Note"
SO = "Sales Order"
ACTIVE = "Active"
INACTIVE = "Inactive"


# ---------------------------------------------------------------------------
# Serials of a document
# ---------------------------------------------------------------------------


def serials_of(doc) -> set[str]:
	"""Every serial a Delivery Note / Sales Order lists (rows, bundles, packed items)."""
	if not doc:
		return set()
	serials: set[str] = set()
	bundles = []
	for row in list(doc.get("items") or []) + list(doc.get("packed_items") or []):
		serials.update(get_serial_nos(row.get("serial_no") or ""))
		serials.update(get_serial_nos(row.get("dm_serial_nos") or ""))
		if row.get("serial_and_batch_bundle"):
			bundles.append(row.get("serial_and_batch_bundle"))
	if bundles:
		serials.update(frappe.get_all("Serial and Batch Entry", filters={"parent": ["in", bundles],
			"serial_no": ["is", "set"]}, pluck="serial_no"))
	return serials


# ---------------------------------------------------------------------------
# Who holds a serial
# ---------------------------------------------------------------------------


def holders(serials) -> dict[str, set[tuple[str, str]]]:
	"""``{serial: {(doctype, name), ...}}`` of the open documents holding each serial."""
	wanted = set(serials or ())
	found: dict[str, set] = {sn: set() for sn in wanted}
	if not wanted:
		return found

	def add(text, doctype, name):
		for sn in get_serial_nos(text or ""):
			if sn in wanted:
				found[sn].add((doctype, name))

	# Draft Delivery Notes: rows and bundle components.
	for r in frappe.db.sql("""select dni.parent, dni.serial_no from `tabDelivery Note Item` dni
			join `tabDelivery Note` dn on dn.name = dni.parent
			where dn.docstatus = 0 and dn.is_return = 0 and ifnull(dni.serial_no, '') != ''""", as_dict=True):
		add(r.serial_no, DN, r.parent)
	for r in frappe.db.sql("""select pi.parent, pi.serial_no from `tabPacked Item` pi
			join `tabDelivery Note` dn on dn.name = pi.parent
			where pi.parenttype = 'Delivery Note' and dn.docstatus = 0 and dn.is_return = 0
			and ifnull(pi.serial_no, '') != ''""", as_dict=True):
		add(r.serial_no, DN, r.parent)
	for r in frappe.db.sql("""select sbb.voucher_no, sbe.serial_no from `tabSerial and Batch Entry` sbe
			join `tabSerial and Batch Bundle` sbb on sbb.name = sbe.parent
			join `tabDelivery Note` dn on dn.name = sbb.voucher_no
			where sbb.voucher_type = 'Delivery Note' and sbb.is_cancelled = 0 and dn.docstatus = 0
			and dn.is_return = 0 and sbe.serial_no in %(sn)s""", {"sn": tuple(wanted)}, as_dict=True):
		add(r.serial_no, DN, r.voucher_no)

	# Submitted Sales Orders still waiting for their Delivery Note.
	no_dn = """so.docstatus = 1 and so.status not in ('Closed', 'Completed')
		and not exists (select 1 from `tabDelivery Note Item` d where d.against_sales_order = so.name
			and d.docstatus < 2)"""
	for r in frappe.db.sql(f"""select soi.parent, soi.dm_serial_nos as serial_no from `tabSales Order Item` soi
			join `tabSales Order` so on so.name = soi.parent
			where {no_dn} and ifnull(soi.dm_serial_nos, '') != ''""", as_dict=True):
		add(r.serial_no, SO, r.parent)
	for r in frappe.db.sql(f"""select pi.parent, pi.serial_no from `tabPacked Item` pi
			join `tabSales Order` so on so.name = pi.parent
			where pi.parenttype = 'Sales Order' and {no_dn} and ifnull(pi.serial_no, '') != ''""", as_dict=True):
		add(r.serial_no, SO, r.parent)
	return found


def sync(serials) -> None:
	"""Set each serial still in a warehouse to Inactive when held, Active when not."""
	serials = {sn for sn in (serials or ()) if sn}
	if not serials:
		return
	held = holders(serials)
	for row in frappe.get_all("Serial No", filters={"name": ["in", list(serials)]},
			fields=["name", "status", "warehouse"]):
		if not row.warehouse or row.status not in (ACTIVE, INACTIVE):
			continue  # delivered / expired: not ours to change
		status = INACTIVE if held.get(row.name) else ACTIVE
		if row.status != status:
			frappe.db.set_value("Serial No", row.name, "status", status, update_modified=False)


def resync_all() -> int:
	"""Repair: re-check every Inactive serial in a warehouse (frees serials left Inactive
	by documents that no longer hold them). Returns how many were checked."""
	serials = frappe.get_all("Serial No", filters={"status": INACTIVE, "warehouse": ["is", "set"]}, pluck="name")
	for i in range(0, len(serials), 500):
		sync(serials[i:i + 500])
	return len(serials)


# ---------------------------------------------------------------------------
# Delivery Note
# ---------------------------------------------------------------------------


def _is_open_dn(doc) -> bool:
	return doc.docstatus == 0 and not cint(doc.get("is_return"))


def fill_delivery_note_serials(doc, method=None):
	"""validate: every serialised row of a draft gets exactly ``qty`` serials.

	Missing serials are picked (oldest Active first) from the row's warehouse; when the
	qty went down, the extra serials are dropped (and become Active on save). Rows using a
	Serial and Batch Bundle are left to ERPNext.
	"""
	if not _is_open_dn(doc):
		return
	from delivery_management.orders.stock import allocate_packed_serials, pick_serials

	rows = [r for r in doc.items if r.item_code and not r.get("serial_and_batch_bundle")]
	serialised = set(frappe.get_all("Item", filters={"name": ["in", [r.item_code for r in rows] or [""]],
		"has_serial_no": 1}, pluck="name"))
	mine = _own_serials(doc)
	taken: list[str] = []
	for row in rows:
		if row.item_code not in serialised:
			continue
		need = cint(row.stock_qty or row.qty)
		current = [sn for sn in get_serial_nos(row.serial_no or "") if sn not in taken][: max(need, 0)]
		if len(current) < need and row.warehouse:
			# Serials this draft already held stay first in line (they are Inactive for us).
			again = [sn for sn in mine.get(row.item_code, []) if sn not in taken and sn not in current]
			current += again[: need - len(current)]
			if len(current) < need:
				current += pick_serials(row.item_code, row.warehouse, need - len(current), exclude=taken + current)
		taken += current
		row.serial_no = "\n".join(current)
		row.use_serial_batch_fields = 1
	allocate_packed_serials(doc)


def _own_serials(doc) -> dict[str, list[str]]:
	"""Serials the saved version of this draft holds, by item (re-used before new picks)."""
	if doc.is_new():
		return {}
	out: dict[str, list[str]] = {}
	for r in frappe.get_all("Delivery Note Item", filters={"parent": doc.name, "parenttype": DN},
			fields=["item_code", "serial_no"], order_by="idx asc"):
		out.setdefault(r.item_code, []).extend(get_serial_nos(r.serial_no or ""))
	return out


def check_delivery_note_serials(doc, method=None):
	"""validate: a serial may be held by one open document only."""
	if not _is_open_dn(doc):
		return
	own_orders = {(SO, r.get("against_sales_order")) for r in doc.items if r.get("against_sales_order")}
	allowed = own_orders | {(DN, doc.name)}
	clash = []
	for sn, docs in holders(serials_of(doc)).items():
		other = docs - allowed
		if other:
			clash.append(_("{0} (held by {1})").format(sn, ", ".join(f"{dt} {name}" for dt, name in sorted(other))))
	if clash:
		frappe.throw(_("These serial numbers are already used by another open document:") + "<br>"
			+ "<br>".join(clash), title=_("Serial Already Used"))


def on_delivery_note_change(doc, method=None):
	"""on_update / on_submit / on_cancel: re-check serials before and after the change."""
	before = doc.get_doc_before_save() if method == "on_update" else None
	serials = serials_of(doc) | serials_of(before)
	if before:
		# Order rows removed from the draft: the order must not take their serials back.
		kept = {r.get("so_detail") for r in doc.items}
		serials |= _release_order_rows([r.get("so_detail") for r in before.items
			if r.get("so_detail") and r.get("so_detail") not in kept])
	sync(serials)


def on_delivery_note_delete(doc, method=None):
	"""after_delete: the serials are free again, including the ones the Sales Order had
	reserved for these rows (they are cleared, so the order does not take them back)."""
	serials = serials_of(doc) | _release_order_rows([r.get("so_detail") for r in doc.items])
	sync(serials)


def _release_order_rows(so_details) -> set[str]:
	"""Clear the serials a Sales Order reserved for these rows; returns them (to re-check)."""
	so_details = [d for d in so_details if d]
	serials: set[str] = set()
	if not so_details:
		return serials
	for row in frappe.get_all("Sales Order Item", filters={"name": ["in", so_details]}, fields=["name", "dm_serial_nos"]):
		if row.dm_serial_nos:
			serials.update(get_serial_nos(row.dm_serial_nos))
			frappe.db.set_value("Sales Order Item", row.name, "dm_serial_nos", None, update_modified=False)
	for row in frappe.get_all("Packed Item", filters={"parenttype": SO, "parent_detail_docname": ["in", so_details]},
			fields=["name", "serial_no"]):
		if row.serial_no:
			serials.update(get_serial_nos(row.serial_no))
			frappe.db.set_value("Packed Item", row.name, "serial_no", None, update_modified=False)
	return serials


# ---------------------------------------------------------------------------
# Sales Order
# ---------------------------------------------------------------------------


def on_sales_order_change(doc, method=None):
	"""on_submit / on_cancel / after_delete of a Sales Order."""
	sync(serials_of(doc))
