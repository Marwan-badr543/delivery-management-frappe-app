"""Customer returns and exchanges, made by the cashier from the POS.

One call does everything a return needs, in one transaction:

* **Stock comes back the way it left.** A branch sale (the invoice moved the stock) gets a
  Credit Note *with stock* into the receiving store. A delivery Order gets a Return
  Delivery Note for the goods the customer brings back (from the Delivery Notes they
  left on, at the cost they left with), and its draft Delivery Note loses the goods that
  never left (cancelled).
* **Money:** a Credit Note for the returned and cancelled lines (no stock for an Order).
  It first clears what the customer still owes on the sale; the rest is the customer's:
  refunded in cash on the Credit Note itself (a negative cash payment in the cashier's
  shift, so the closing counts it once) or kept as credit, which POS Awesome offers as
  Customer Credit on the next sale (exchange).
  An unpaid Order still has a *draft* invoice (``orders.service``): the lines simply come
  off it, no Credit Note.
* **The Sales Order is never cancelled; its lines are not to deliver any more.** ERPNext
  counts goods brought back in ``returned_qty`` (Return Delivery Note); goods cancelled
  before delivery are added to it too and remembered in ``dm_cancelled_qty``. With
  Selling Settings "Don't Reserve Sales Order Qty on Sales Return" (set by the app) the
  store stops reserving both at once. When nothing is left to deliver the order is
  closed and its delivery / AC bookings not used yet are freed.
* **TV accounts** of the TVs given back are deleted (``orders.tv_accounts``).

Everything is recorded on a **Customer Return** that links the documents made.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from delivery_management.drivers import assignment as asg
from delivery_management.orders import tv_accounts
from delivery_management.slots.cart import profile_cart

CR = "Customer Return"
SI = "Sales Invoice"
SOI = "Sales Order Item"
DN = "Delivery Note"
SRE = "Stock Reservation Entry"
EPSILON = 1e-6


# ---------------------------------------------------------------------------
# What can be returned
# ---------------------------------------------------------------------------


def get_sale(invoice: str) -> dict:
	"""The sale as the return screen shows it: each line with what can still be given
	back (``max_return``: with the customer) or cancelled (``max_cancel``: never left
	the store), and the serials still with the customer."""
	si = frappe.get_doc(SI, invoice)
	_check_returnable(si)
	credited = _credited_qty(si.name) if si.docstatus == 1 else {}
	so_items = _sales_order_items(si)
	# What one unit is worth with tax and invoice discount, to show the money on screen.
	ratio = flt(si.rounded_total or si.grand_total) / flt(si.net_total) if flt(si.net_total) else 1
	lines = []
	for row in si.items:
		open_qty = flt(row.qty) - credited.get(row.name, 0)
		soi = so_items.get(row.so_detail)
		if soi:
			with_customer = max(flt(soi.delivered_qty), 0)
			not_delivered = max(flt(soi.qty) - flt(soi.delivered_qty) - flt(soi.returned_qty), 0)
			max_return, max_cancel = min(open_qty, with_customer), min(open_qty, not_delivered)
			serials = _serials_out(_delivered_rows(row.so_detail)) if soi.has_serial_no else []
		else:
			max_return, max_cancel = (open_qty if cint(si.update_stock) else 0), 0
			serials = _serials_out([row]) if _has_serial(row.item_code) else []
		lines.append({
			"si_detail": row.name, "so_detail": row.so_detail, "item_code": row.item_code,
			"item_name": row.item_name, "uom": row.uom, "conversion_factor": flt(row.conversion_factor) or 1,
			"qty": flt(row.qty), "rate": flt(row.net_rate),
			"unit_value": flt(row.net_amount) / flt(row.qty) * ratio if flt(row.qty) else 0,
			"returned_before": credited.get(row.name, 0), "max_return": max(max_return, 0),
			"max_cancel": max(max_cancel, 0), "serial_nos": serials,
		})
	return {
		"name": si.name, "customer": si.customer, "customer_name": si.customer_name,
		"posting_date": str(si.posting_date), "company": si.company, "currency": si.currency,
		"grand_total": flt(si.rounded_total or si.grand_total),
		# A draft (unpaid Order) is still owed in full.
		"outstanding_amount": flt(si.outstanding_amount if si.docstatus == 1 else si.rounded_total or si.grand_total),
		"is_order": bool(so_items), "is_draft": si.docstatus == 0, "update_stock": cint(si.update_stock),
		"sales_order": next((s.parent for s in so_items.values()), None), "lines": lines,
	}


def is_order_draft(si) -> bool:
	"""The draft invoice of an unpaid delivery Order (not a POS cart still being filled)."""
	return si.docstatus == 0 and any(r.get("sales_order") for r in si.items)


def _check_returnable(si) -> None:
	if si.docstatus != 1 and not is_order_draft(si):
		frappe.throw(_("Invoice {0} is not submitted.").format(si.name))
	if cint(si.is_return):
		frappe.throw(_("{0} is already a return; open the original sale instead.").format(si.name))


def _credited_qty(invoice: str) -> dict[str, float]:
	"""``{original invoice row: qty already credited}`` by submitted Credit Notes."""
	rows = frappe.db.sql(
		"""select item.sales_invoice_item, sum(-item.qty) as qty
		from `tabSales Invoice Item` item join `tabSales Invoice` cn on cn.name = item.parent
		where cn.docstatus = 1 and cn.is_return = 1 and cn.return_against = %s
		group by item.sales_invoice_item""",
		invoice,
		as_dict=True,
	)
	return {r.sales_invoice_item: flt(r.qty) for r in rows}


def _sales_order_items(si) -> dict:
	details = [r.so_detail for r in si.items if r.get("so_detail")]
	if not details:
		return {}
	rows = frappe.db.sql(
		"""select soi.name, soi.parent, soi.item_code, soi.qty, soi.delivered_qty, soi.returned_qty,
			soi.conversion_factor, item.has_serial_no
		from `tabSales Order Item` soi join `tabItem` item on item.name = soi.item_code
		where soi.name in %(names)s""",
		{"names": tuple(details)},
		as_dict=True,
	)
	return {r.name: r for r in rows}


def _has_serial(item_code: str) -> bool:
	return bool(frappe.get_cached_value("Item", item_code, "has_serial_no"))


def _row_serials(row) -> list[str]:
	from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
	from erpnext.stock.serial_batch_bundle import get_serial_nos as bundle_serials

	if row.get("serial_and_batch_bundle"):
		return bundle_serials(row.serial_and_batch_bundle)
	return get_serial_nos(row.get("serial_no") or "")


def _serials_out(rows) -> list[str]:
	"""Serials sold on ``rows`` that are still with the customer (not back in a store)."""
	serials = [s for row in rows for s in _row_serials(row)]
	if not serials:
		return []
	out = set(frappe.get_all("Serial No", filters={"name": ["in", serials], "warehouse": ["is", "not set"]},
		pluck="name"))
	return [s for s in serials if s in out]


def _delivered_rows(so_detail: str) -> list:
	"""Submitted (not return) Delivery Note rows of an order line, oldest first."""
	return frappe.db.sql(
		"""select dni.name, dni.parent, dni.qty, dni.serial_no, dni.serial_and_batch_bundle
		from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
		where dn.docstatus = 1 and dn.is_return = 0 and dni.so_detail = %s
		order by dn.posting_date, dn.creation""",
		so_detail,
		as_dict=True,
	)


# ---------------------------------------------------------------------------
# The return
# ---------------------------------------------------------------------------


def make_return(data: dict) -> dict:
	"""``data``: ``sales_invoice``, ``pos_profile``, ``lines`` (``si_detail``, ``returned_qty``,
	``cancelled_qty``, ``serial_nos``), ``warehouse`` (receiving store, default the POS
	profile's), ``refund_amount`` (cash now; the rest stays as credit) or ``refund_all``
	(everything the customer paid for these goods), ``reason``,
	``notes``, ``request_id`` (a retried request returns the first result)."""
	data = frappe._dict(data)
	if data.request_id and (done := frappe.db.get_value(CR, {"request_id": data.request_id})):
		return summary(done)

	# Two cashiers (or a double click) returning from the same sale wait for each other.
	frappe.db.sql("select name from `tabSales Invoice` where name = %s for update", data.sales_invoice)
	si = frappe.get_doc(SI, data.sales_invoice)
	sale = get_sale(si.name)
	profile = frappe.get_cached_doc("POS Profile", data.pos_profile)
	if profile.company != si.company:
		frappe.throw(_("Invoice {0} belongs to company {1}, not to this POS ({2}).").format(
			si.name, si.company, profile.company))
	warehouse = _receiving_warehouse(data.warehouse or profile.warehouse, si.company)
	shift = open_shift(profile.name)
	plan = _plan(sale, data.lines or [])

	# The Credit Note is drafted first: it checks the refund before any stock moves.
	owed = sale["outstanding_amount"]
	refund = None if cint(data.refund_all) else flt(data.refund_amount, 3)
	cn = None if sale["is_draft"] else _credit_note(si, plan, warehouse, shift, profile, refund, owed)
	accounts = tv_accounts.remove_from_sale(si.name, sale["sales_order"], tv_accounts.tv_units(
		[{"item_code": p.item_code, "qty": p.returned_qty + p.cancelled_qty, "conversion_factor": p.conversion_factor}
			for p in plan]))
	if not cn:
		# Nothing was paid yet: the lines come off the bill (before the order may close),
		# nothing is given back.
		value, invoice = _take_off_draft(si.name, plan)
		cleared, refund, credit = value, 0, 0
	return_notes = []
	if not cint(si.update_stock):
		return_notes = _return_delivered(plan, warehouse)
		_cancel_undelivered(plan)
	if cn:
		cn.submit()
		_clear_debt(si.name, cn.name)
		value = -flt(cn.rounded_total or cn.grand_total)
		cleared = owed - max(flt(frappe.db.get_value(SI, si.name, "outstanding_amount")), 0)
		refund = -flt(cn.paid_amount)
		credit = -flt(frappe.db.get_value(SI, cn.name, "outstanding_amount"))
		invoice = si.name

	if sale["sales_order"]:
		settle_order(sale["sales_order"])

	doc = frappe.get_doc({
		"doctype": CR, "sales_invoice": invoice, "sales_order": sale["sales_order"], "customer": si.customer,
		"posting_date": nowdate(), "company": si.company, "currency": si.currency, "pos_profile": profile.name,
		"pos_opening_shift": shift, "warehouse": warehouse, "reason": data.reason, "notes": data.notes,
		"return_amount": value, "cleared_amount": cleared,
		"refunded_amount": refund, "mode_of_payment": cn.payments[0].mode_of_payment if refund else None,
		"credit_amount": max(credit, 0), "credit_note": cn.name if cn else None,
		"return_delivery_notes": "\n".join(return_notes) or None, "tv_accounts": "\n".join(accounts) or None,
		"request_id": data.request_id or None,
		"items": [{"item_code": p.item_code, "item_name": p.item_name, "returned_qty": p.returned_qty,
			"cancelled_qty": p.cancelled_qty, "rate": p.rate, "amount": p.rate * (p.returned_qty + p.cancelled_qty),
			"serial_no": "\n".join(p.serial_nos) or None, "si_detail": p.si_detail, "so_detail": p.so_detail}
			for p in plan],
	})
	doc.insert(ignore_permissions=True)
	return summary(doc.name)


def summary(name: str) -> dict:
	doc = frappe.get_doc(CR, name)
	return {
		"name": doc.name, "customer": doc.customer, "customer_name": doc.customer_name,
		"credit_note": doc.credit_note,
		"return_delivery_notes": (doc.return_delivery_notes or "").split("\n") if doc.return_delivery_notes else [],
		"tv_accounts": (doc.tv_accounts or "").split("\n") if doc.tv_accounts else [],
		"return_amount": flt(doc.return_amount), "cleared_amount": flt(doc.cleared_amount),
		"refunded_amount": flt(doc.refunded_amount), "credit_amount": flt(doc.credit_amount),
		"currency": doc.currency,
	}


def _receiving_warehouse(warehouse: str | None, company: str) -> str:
	values = warehouse and frappe.db.get_value("Warehouse", warehouse, ["company", "is_group", "disabled"], as_dict=True)
	if not values or values.company != company or values.is_group or values.disabled:
		frappe.throw(_("Choose the store that receives the goods (a store of {0}).").format(company))
	return warehouse


def open_shift(profile: str) -> str:
	shift = frappe.db.get_value("POS Opening Shift", {"pos_profile": profile, "user": frappe.session.user,
		"status": "Open", "docstatus": 1}, "name")
	if not shift:
		frappe.throw(_("Open a POS shift first: the money goes in the cashier's shift."), title=_("No open shift"))
	return shift


def _plan(sale: dict, lines: list) -> list:
	"""The requested lines, checked against what the sale allows."""
	by_detail = {line["si_detail"]: line for line in sale["lines"]}
	plan = []
	for req in lines:
		req = frappe._dict(req)
		line = by_detail.get(req.si_detail)
		if not line:
			frappe.throw(_("That line is not on invoice {0}.").format(sale["name"]))
		returned, cancelled = flt(req.returned_qty), flt(req.cancelled_qty)
		if returned < 0 or cancelled < 0:
			frappe.throw(_("Quantities cannot be negative."))
		if not returned and not cancelled:
			continue
		label = f"{line['item_code']}: "
		if returned > line["max_return"] + EPSILON:
			frappe.throw(label + _("only {0} can be returned (sold {1}, already returned {2}).").format(
				_qty(line["max_return"]), _qty(line["qty"]), _qty(line["returned_before"])), title=_("Too many returned"))
		if cancelled > line["max_cancel"] + EPSILON:
			frappe.throw(label + _("only {0} can be cancelled (not delivered yet).").format(_qty(line["max_cancel"])),
				title=_("Too many cancelled"))
		serials = _check_serials(line, returned, req.serial_nos)
		plan.append(frappe._dict(line, returned_qty=returned, cancelled_qty=cancelled, serial_nos=serials))
	if not plan:
		frappe.throw(_("Choose at least one item to return or cancel."))
	return plan


def _qty(value: float) -> str:
	return f"{flt(value):g}"


def _check_serials(line: dict, returned: float, chosen) -> list[str]:
	if not line["serial_nos"] and not chosen:
		return []
	if isinstance(chosen, str):
		chosen = [s.strip() for s in chosen.replace(",", "\n").split("\n") if s.strip()]
	chosen = list(dict.fromkeys(chosen or []))
	if not chosen and len(line["serial_nos"]) == returned:
		chosen = list(line["serial_nos"])
	if len(chosen) != returned:
		frappe.throw(_("{0}: choose the {1} serial number(s) coming back ({2} chosen).").format(
			line["item_code"], cint(returned), len(chosen)), title=_("Serial numbers"))
	foreign = [s for s in chosen if s not in line["serial_nos"]]
	if foreign:
		frappe.throw(_("{0}: serial {1} was not sold on this sale, or is already back.").format(
			line["item_code"], ", ".join(foreign)), title=_("Serial numbers"))
	return chosen


# ---------------------------------------------------------------------------
# Stock: delivered goods come back, undelivered goods are cancelled
# ---------------------------------------------------------------------------


def _return_delivered(plan, warehouse: str) -> list[str]:
	"""Return Delivery Notes (one per original note) for the goods brought back."""
	per_note: dict[str, dict] = defaultdict(dict)  # {dn: {dn_detail: (qty, serials)}}
	for p in plan:
		qty, wanted = p.returned_qty, set(p.serial_nos)
		rows = _delivered_rows(p.so_detail)
		returned = _returned_per_row([r.name for r in rows])
		# Rows holding the chosen serials first.
		rows.sort(key=lambda r: not wanted.intersection(_row_serials(r)))
		for row in rows:
			if qty <= EPSILON:
				break
			take = min(flt(row.qty) - returned.get(row.name, 0), qty)
			if take <= EPSILON:
				continue
			serials = [s for s in _row_serials(row) if s in wanted][: cint(take)] if wanted else []
			wanted -= set(serials)
			per_note[row.parent][row.name] = (take, serials)
			qty -= take
	names = []
	for note, rows in per_note.items():
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		ret = make_sales_return(note)
		ret.set("items", [r for r in ret.items if r.dn_detail in rows])
		for r in ret.items:
			take, serials = rows[r.dn_detail]
			r.qty, r.stock_qty = -take, -take * flt(r.conversion_factor or 1)
			r.warehouse = warehouse
			if serials:
				r.update({"serial_no": "\n".join(serials), "use_serial_batch_fields": 1, "serial_and_batch_bundle": None})
		ret.set_warehouse = warehouse
		ret.dm_returned_before_delivery = frappe.db.get_value(DN, note, "dm_delivery_status") == "On the way"
		ret.set("packed_items", [])  # rebuilt from the kept rows
		ret.flags.ignore_permissions = True
		ret.insert()
		# Reloaded: ERPNext sets up the order's returned_qty update only for a return loaded as one.
		ret = frappe.get_doc(DN, ret.name)
		ret.flags.ignore_permissions = True
		ret.submit()
		names.append(ret.name)
	return names


def _returned_per_row(dn_details: list[str]) -> dict[str, float]:
	if not dn_details:
		return {}
	rows = frappe.db.sql(
		"""select dni.dn_detail, sum(-dni.qty) as qty
		from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
		where dn.docstatus = 1 and dn.is_return = 1 and dni.dn_detail in %(rows)s group by dni.dn_detail""",
		{"rows": tuple(dn_details)},
		as_dict=True,
	)
	return {r.dn_detail: flt(r.qty) for r in rows}


def _cancel_undelivered(plan) -> None:
	"""Take the cancelled goods off the order's draft Delivery Notes (a note left empty is
	deleted), release their stock reservation and count them as returned on the order."""
	for p in plan:
		qty = p.cancelled_qty
		if qty <= EPSILON:
			continue
		_unreserve(p.so_detail, qty)
		cancelled, returned = frappe.db.get_value(SOI, p.so_detail, ["dm_cancelled_qty", "returned_qty"])
		frappe.db.set_value(SOI, p.so_detail, {"dm_cancelled_qty": flt(cancelled) + qty,
			"returned_qty": flt(returned) + qty}, update_modified=False)
		drafts = frappe.db.sql(
			"""select dni.parent from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
			where dn.docstatus = 0 and dn.is_return = 0 and dni.so_detail = %s order by dn.creation""",
			p.so_detail,
			pluck=True,
		)
		for name in dict.fromkeys(drafts):
			if qty <= EPSILON:
				break
			dn = frappe.get_doc(DN, name)
			for row in [r for r in dn.items if r.so_detail == p.so_detail]:
				take = min(flt(row.qty), qty)
				row.qty = flt(row.qty) - take
				qty -= take
			dn.set("items", [r for r in dn.items if flt(r.qty) > EPSILON])
			dn.flags.ignore_permissions = True
			if dn.items:
				for i, r in enumerate(dn.items, 1):
					r.idx = i
				dn.save()
			else:
				frappe.delete_doc(DN, dn.name, ignore_permissions=True)


def _unreserve(so_detail: str, qty: float) -> None:
	"""Release ``qty`` (order line UOM) of the line's stock reservation, components of a
	bundle in proportion. Reserved stock that is not delivered is what gets released."""
	order_qty = flt(frappe.db.get_value(SOI, so_detail, "qty"))
	left: dict[str, float] = {}  # per item (a bundle reserves each component)
	for sre in frappe.get_all(SRE, filters={"voucher_type": "Sales Order", "voucher_detail_no": so_detail,
			"docstatus": 1}, fields=["name", "item_code", "voucher_qty"], order_by="creation desc"):
		left.setdefault(sre.item_code, qty * flt(sre.voucher_qty) / order_qty)
		doc = frappe.get_doc(SRE, sre.name)
		release = min(flt(doc.reserved_qty) - flt(doc.delivered_qty), left[sre.item_code])
		if release <= EPSILON:
			continue
		left[sre.item_code] -= release
		if flt(doc.reserved_qty) - release <= EPSILON:
			doc.flags.ignore_permissions = True
			doc.cancel()
			continue
		doc.reserved_qty = flt(doc.reserved_qty) - release
		doc.db_update()
		doc.update_reserved_qty_in_voucher()
		doc.update_status()
		doc.update_reserved_stock_in_bin()


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


def _credit_note(si, plan, warehouse: str, shift: str, profile, refund: float | None, owed: float):
	"""The draft Credit Note. The cash given back is a (negative) cash payment on the note itself,
	in the cashier's shift: the shift closing counts it once, and what is not refunded
	stays open on the note as the customer's credit."""
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

	qty = {p.si_detail: p for p in plan}
	cn = make_sales_return(si.name)
	cn.set("items", [r for r in cn.items if r.sales_invoice_item in qty])
	for r in cn.items:
		p = qty[r.sales_invoice_item]
		total = p.returned_qty + p.cancelled_qty
		r.qty, r.stock_qty = -total, -total * flt(r.conversion_factor or 1)
		if cint(cn.update_stock):
			r.warehouse = warehouse
			r.serial_and_batch_bundle = None
			if p.serial_nos:
				r.update({"serial_no": "\n".join(p.serial_nos), "use_serial_batch_fields": 1})
	if cint(cn.update_stock):
		cn.set_warehouse = warehouse
		cn.set("packed_items", [])
	cn.update({"posa_pos_opening_shift": shift, "pos_profile": profile.name, "update_outstanding_for_self": 1,
		"update_billed_amount_in_sales_order": 1, "is_pos": 0})
	cn.set("payments", [])
	cn.flags.ignore_permissions = True
	cn.insert()

	value = -flt(cn.rounded_total or cn.grand_total)
	most = max(value - max(owed, 0), 0)  # the debt on the sale is cleared first
	if refund is None:
		refund = most
	if refund < 0 or refund > most + EPSILON:
		frappe.throw(_("The refund can be at most {0}: the rest of the return value clears what the customer owed.")
			.format(frappe.format_value(most, {"fieldtype": "Currency", "options": si.currency})),
			title=_("Refund too large"))
	if refund > 0:
		mode = profile.get("posa_cash_mode_of_payment") or "Cash"
		cn.is_pos = 1
		cn.append("payments", {"mode_of_payment": mode, "amount": -refund, "default": 1})
		cn.save()
	return cn


def _clear_debt(invoice: str, credit_note: str) -> None:
	"""Use the Credit Note for what the customer still owes on the sale (Payment
	Reconciliation). ERPNext already does it when the note is not bigger than the debt."""
	owed = flt(frappe.db.get_value(SI, invoice, "outstanding_amount"))
	credit = -flt(frappe.db.get_value(SI, credit_note, "outstanding_amount"))
	if min(owed, credit) <= EPSILON:
		return
	si = frappe.db.get_value(SI, invoice, ["company", "customer", "debit_to"], as_dict=True)
	pr = frappe.get_doc({"doctype": "Payment Reconciliation", "company": si.company, "party_type": "Customer",
		"party": si.customer, "receivable_payable_account": si.debit_to})
	pr.get_unreconciled_entries()
	payment = next(p for p in pr.payments if p.reference_name == credit_note)
	debt = next(i for i in pr.invoices if i.invoice_number == invoice)
	pr.allocate_entries(frappe._dict(payments=[payment.as_dict()], invoices=[debt.as_dict()]))
	pr.reconcile()


def _take_off_draft(invoice: str, plan) -> tuple[float, str | None]:
	"""Unpaid Order: the lines given back come off its draft invoice. Returns the value
	taken off and the invoice, or None when nothing is left on it and the draft is deleted
	(earlier Customer Returns keep only the order then)."""
	si = frappe.get_doc(SI, invoice)
	before = flt(si.rounded_total or si.grand_total)
	qty = {p.si_detail: p.returned_qty + p.cancelled_qty for p in plan}
	for row in si.items:
		row.qty = flt(row.qty) - qty.get(row.name, 0)
	si.set("items", [r for r in si.items if flt(r.qty) > EPSILON])
	if not si.items:
		frappe.db.set_value(CR, {"sales_invoice": invoice}, "sales_invoice", None)
		frappe.delete_doc(SI, invoice, ignore_permissions=True)
		return before, None
	for i, r in enumerate(si.items, 1):
		r.idx = i
	si.flags.ignore_permissions = True
	si.save()
	return before - flt(si.rounded_total or si.grand_total), invoice


# ---------------------------------------------------------------------------
# The Sales Order
# ---------------------------------------------------------------------------


def pending_by_type(sales_order: str) -> dict[str, float]:
	"""Quantity still to deliver per delivery type (AC / Non AC)."""
	pending: dict[str, float] = defaultdict(float)
	for row in frappe.get_all(SOI, filters={"parent": sales_order},
			fields=["name", "item_code", "qty", "delivered_qty", "returned_qty"]):
		left = flt(row.qty) - flt(row.delivered_qty) - flt(row.returned_qty)
		if left > EPSILON:
			pending[_line_type(row)] += left
	return pending


def _line_type(row) -> str:
	note_type = frappe.db.sql(
		"""select dn.dm_delivery_type from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
		where dni.so_detail = %s and dn.docstatus < 2 and ifnull(dn.dm_delivery_type, '') != '' limit 1""",
		row.name,
	)
	if note_type:
		return note_type[0][0]
	profile = profile_cart([{"item_code": row.item_code, "qty": row.qty}])
	return asg.TYPE_AC if profile.has_ac or profile.accessory_items else asg.TYPE_NON_AC


def settle_order(sales_order: str) -> None:
	"""After a return (or a delivery of an order that had one): recount the store's
	reservation, free the bookings of a delivery type with nothing left to deliver that was
	never delivered, and close the order when nothing at all is left."""
	from delivery_management.slots import get_reservations, release_reservation
	from delivery_management.slots.engine import SLOT_AC

	returned = frappe.get_all(SOI, filters={"parent": sales_order, "returned_qty": [">", 0]}, pluck="name")
	if not returned:
		return
	frappe.get_doc("Sales Order", sales_order).update_reserved_qty(returned)
	pending = pending_by_type(sales_order)
	delivered_types = set(frappe.db.sql(
		"""select distinct dn.dm_delivery_type from `tabDelivery Note Item` dni
		join `tabDelivery Note` dn on dn.name = dni.parent
		where dni.against_sales_order = %s and dn.docstatus = 1 and dn.is_return = 0""",
		sales_order,
		pluck=True,
	))
	for booking in get_reservations("Sales Order", sales_order):
		kind = asg.TYPE_AC if booking.slot_type == SLOT_AC else asg.TYPE_NON_AC
		if pending.get(kind, 0) <= EPSILON and kind not in delivered_types:
			release_reservation(booking.name)
	if not pending and frappe.db.get_value("Sales Order", sales_order, "status") not in ("Closed", "Completed"):
		so = frappe.get_doc("Sales Order", sales_order)
		so.flags.ignore_permissions = True
		so.update_status("Closed")


# ---------------------------------------------------------------------------
# Delivery Note hooks
# ---------------------------------------------------------------------------


def check_not_returned(dn, method=None):
	"""Delivery Note ``before_submit``: goods given back or cancelled through a Customer
	Return are never delivered again."""
	if cint(dn.get("is_return")):
		return
	asked: dict[str, float] = defaultdict(float)
	for row in dn.items:
		if row.get("so_detail"):
			asked[row.so_detail] += flt(row.qty)
	for so_detail, qty in asked.items():
		soi = frappe.db.get_value(SOI, so_detail, ["item_code", "qty", "delivered_qty", "returned_qty"], as_dict=True)
		if not soi or not flt(soi.returned_qty):
			continue
		left = flt(soi.qty) - flt(soi.delivered_qty) - flt(soi.returned_qty)
		if qty > left + EPSILON:
			frappe.throw(_("{0}: only {1} left to deliver on the order; {2} was returned or cancelled.").format(
				soi.item_code, _qty(max(left, 0)), _qty(soi.returned_qty)), title=_("Returned goods"))


def settle_after_delivery(dn, method=None):
	"""Delivery Note ``on_submit``: an order with returned goods closes after its last delivery."""
	if cint(dn.get("is_return")):
		return
	for sales_order in {r.against_sales_order for r in dn.items if r.get("against_sales_order")}:
		settle_order(sales_order)


def keep_cancelled_as_returned(dn, method=None):
	"""Return Delivery Note ``on_submit`` / ``on_cancel``: ERPNext recounts the order lines'
	``returned_qty`` from their Return Delivery Notes; add back what was cancelled before
	delivery, then recount the store's reservation."""
	if not cint(dn.get("is_return")):
		return
	for so_detail in {r.so_detail for r in dn.items if r.get("so_detail")}:
		cancelled, returned = frappe.db.get_value(SOI, so_detail, ["dm_cancelled_qty", "returned_qty"])
		if flt(cancelled):
			frappe.db.set_value(SOI, so_detail, "returned_qty", flt(returned) + flt(cancelled), update_modified=False)
	for sales_order in {r.against_sales_order for r in dn.items if r.get("against_sales_order")}:
		settle_order(sales_order)
