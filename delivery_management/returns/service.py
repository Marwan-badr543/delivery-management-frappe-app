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
  Customer Credit on the next sale (exchange). An unpaid Order's invoice is owed in full,
  so its Credit Note only clears the debt.
* **The Sales Order is never cancelled; its lines are not to deliver any more.** The
  order line's ``returned_qty`` is what the customer gave back for good: the goods of
  the Return Delivery Notes made here ("Customer Return") and the goods cancelled
  before delivery (``dm_cancelled_qty``). With Selling Settings "Don't Reserve Sales
  Order Qty on Sales Return" (set by the app) the store stops reserving both.
* **Goods the driver brings back** are never returned on a Delivery Note: the note is a
  draft until it is installed (``transit.service``). Goods taken off it are cancelled on
  the order and credited (``cancel_not_installed``); a note that comes back whole is sent
  again later.
* **The order closes when it is done** (``settle_order``): nothing left to deliver,
  every Delivery Note of it delivered (or returned whole), and its invoices paid. Its
  delivery / AC bookings not used are freed as soon as nothing is left to deliver.
* **TV accounts** of the TVs given back are deleted (``orders.tv_accounts``).

Everything is recorded on a **Customer Return** that links the documents made.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from delivery_management.control import log
from delivery_management.drivers import assignment as asg
from delivery_management.drivers.delivery_note import ON_THE_WAY, sales_order_of
from delivery_management.orders import reservation, tv_accounts
from delivery_management.slots.cart import profile_cart

CR = "Customer Return"
SI = "Sales Invoice"
SOI = "Sales Order Item"
DN = "Delivery Note"
SRE = "Stock Reservation Entry"
EPSILON = 1e-6
EPSILON_MONEY = 0.0005


# ---------------------------------------------------------------------------
# What can be returned
# ---------------------------------------------------------------------------


def get_sale(invoice: str) -> dict:
	"""The sale as the return screen shows it: each line with what can still be given
	back (``max_return``: with the customer) or cancelled (``max_cancel``: never left
	the store), and the serials still with the customer."""
	si = frappe.get_doc(SI, invoice)
	_check_returnable(si)
	credited = _credited_qty(si.name)
	so_items = _sales_order_items(si)
	# What one unit is worth with tax and invoice discount, to show the money on screen.
	ratio = flt(si.rounded_total or si.grand_total) / flt(si.net_total) if flt(si.net_total) else 1
	lines = []
	for row in si.items:
		open_qty = flt(row.qty) - credited.get(row.name, 0)
		soi = so_items.get(row.so_detail)
		if soi:
			with_customer = max(flt(soi.delivered_qty), 0)
			# Goods out with a driver are not in the store to cancel: they come back first.
			not_delivered = max(flt(soi.qty) - flt(soi.delivered_qty) - flt(soi.returned_qty) - out_qty(row.so_detail), 0)
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
		"outstanding_amount": flt(si.outstanding_amount),
		"is_order": bool(so_items), "update_stock": cint(si.update_stock),
		"sales_order": next((s.parent for s in so_items.values()), None), "lines": lines,
	}


def _check_returnable(si) -> None:
	if si.docstatus != 1:
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


def out_qty(so_detail: str) -> float:
	"""Quantity of an order line out of the store with a driver (draft notes sent out)."""
	return flt(frappe.db.sql(
		"""select ifnull(sum(dni.qty), 0) from `tabDelivery Note Item` dni
		join `tabDelivery Note` dn on dn.name = dni.parent
		where dn.docstatus = 0 and dn.is_return = 0 and ifnull(dn.dm_delivery_status, '') != ''
		and dni.so_detail = %s""",
		so_detail,
	)[0][0])


# ---------------------------------------------------------------------------
# The return
# ---------------------------------------------------------------------------


def make_return(data: dict) -> dict:
	"""``data``: ``sales_invoice``, ``pos_profile``, ``lines`` (``si_detail``, ``returned_qty``,
	``cancelled_qty``, ``serial_nos``), ``refund_amount`` (cash now; the rest stays as credit) or ``refund_all``
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
	# Goods come back into the cashier's own branch store, never another branch's.
	warehouse = _receiving_warehouse(profile.warehouse, si.company)
	shift = open_shift(profile.name)
	plan = _plan(sale, data.lines or [])

	# The Credit Note is drafted first: it checks the refund before any stock moves.
	owed = sale["outstanding_amount"]
	refund = None if cint(data.refund_all) else flt(data.refund_amount, 3)
	cn = _credit_note(si, plan, warehouse, shift, profile, refund, owed)
	accounts = tv_accounts.remove_from_sale(si.name, sale["sales_order"], tv_accounts.tv_units(
		[{"item_code": p.item_code, "qty": p.returned_qty + p.cancelled_qty, "conversion_factor": p.conversion_factor}
			for p in plan]))
	return_notes = []
	if not cint(si.update_stock):
		return_notes = _return_delivered(plan, warehouse)
		_cancel_undelivered(plan)
	cn.submit()
	_clear_debt(si.name, cn.name)
	value = -flt(cn.rounded_total or cn.grand_total)
	cleared = owed - max(flt(frappe.db.get_value(SI, si.name, "outstanding_amount")), 0)
	refund = -flt(cn.paid_amount)
	credit = -flt(frappe.db.get_value(SI, cn.name, "outstanding_amount"))

	if sale["sales_order"]:
		settle_order(sale["sales_order"])

	doc = frappe.get_doc({
		"doctype": CR, "sales_invoice": si.name, "sales_order": sale["sales_order"], "customer": si.customer,
		"posting_date": nowdate(), "company": si.company, "currency": si.currency, "pos_profile": profile.name,
		"pos_opening_shift": shift, "warehouse": warehouse, "reason": data.reason, "notes": data.notes,
		"return_amount": value, "cleared_amount": cleared,
		"refunded_amount": refund, "mode_of_payment": cn.payments[0].mode_of_payment if refund else None,
		"credit_amount": max(credit, 0), "credit_note": cn.name,
		"return_delivery_notes": "\n".join(return_notes) or None, "tv_accounts": "\n".join(accounts) or None,
		"request_id": data.request_id or None,
		"items": [{"item_code": p.item_code, "item_name": p.item_name, "returned_qty": p.returned_qty,
			"cancelled_qty": p.cancelled_qty, "rate": p.rate, "amount": p.rate * (p.returned_qty + p.cancelled_qty),
			"serial_no": "\n".join(p.serial_nos) or None, "si_detail": p.si_detail, "so_detail": p.so_detail}
			for p in plan],
	})
	doc.insert(ignore_permissions=True)
	log.add("POS return", sales_order=sale["sales_order"], sales_invoice=si.name, amount=value,
		items=[{"item_name": p.item_name, "qty": p.returned_qty + p.cancelled_qty, "serials": p.serial_nos} for p in plan],
		reference=(CR, doc.name), note=" · ".join(filter(None, [data.reason, data.notes, profile.warehouse])))
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
		frappe.throw(_("The POS Profile's store must be a store of {0}.").format(company))
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
		ret.dm_customer_return = 1
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
	"""Take the cancelled goods off the order's draft Delivery Notes still in the store (a
	note left empty is deleted), release their stock reservation and count them as returned
	on the order."""
	for p in plan:
		qty = p.cancelled_qty
		if qty <= EPSILON:
			continue
		unreserve(p.so_detail, qty)
		cancelled = frappe.db.get_value(SOI, p.so_detail, "dm_cancelled_qty")
		frappe.db.set_value(SOI, p.so_detail, "dm_cancelled_qty", flt(cancelled) + qty, update_modified=False)
		frappe.db.set_value(SOI, p.so_detail, "returned_qty", returned_for_good(p.so_detail), update_modified=False)
		drafts = frappe.db.sql(
			"""select dni.parent from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
			where dn.docstatus = 0 and dn.is_return = 0 and ifnull(dn.dm_delivery_status, '') = ''
			and dni.so_detail = %s order by dn.creation""",
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


def unreserve(so_detail: str, qty: float) -> None:
	"""Release ``qty`` (order line UOM) of the line's stock reservation, components of a
	bundle in proportion. Reserved stock that is not delivered is what gets released."""
	order_qty = flt(frappe.db.get_value(SOI, so_detail, "qty"))
	left: dict[str, float] = {}  # per item (a bundle reserves each component)
	for sre in frappe.get_all(SRE, filters={"voucher_type": "Sales Order", "voucher_detail_no": so_detail,
			"docstatus": 1}, fields=["name", "item_code", "voucher_qty"], order_by="creation desc"):
		left.setdefault(sre.item_code, qty * flt(sre.voucher_qty) / order_qty)
		doc = frappe.get_doc(SRE, sre.name)
		qty_off = min(flt(doc.reserved_qty) - flt(doc.delivered_qty), left[sre.item_code])
		if qty_off <= EPSILON:
			continue
		left[sre.item_code] -= qty_off
		reservation.release(doc, qty_off)


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


def cancel_not_installed(dn, taken) -> list[str]:
	"""Goods the driver took off a note On the way (``transit.service.take_off_note``): the
	order no longer delivers them (``dm_cancelled_qty``) and they are credited on the sale.
	The Credit Note moves no stock (the stock keeper receives the goods) and first clears
	what the customer owes; the rest is the customer's credit, paid out at the POS. Their
	TV accounts are deleted. ``taken``: ``[{so_detail, item_code, qty, conversion_factor}]``.
	Returns the Credit Notes."""
	per_line: dict[str, float] = defaultdict(float)
	for t in taken:
		per_line[t.so_detail] += flt(t.qty)
	for so_detail, qty in per_line.items():
		cancelled = flt(frappe.db.get_value(SOI, so_detail, "dm_cancelled_qty")) + qty
		frappe.db.set_value(SOI, so_detail, "dm_cancelled_qty", cancelled, update_modified=False)
		frappe.db.set_value(SOI, so_detail, "returned_qty", returned_for_good(so_detail), update_modified=False)

	invoice_rows = frappe.db.sql(
		"""select sii.parent, sii.name, sii.so_detail, sii.qty from `tabSales Invoice Item` sii
		join `tabSales Invoice` si on si.name = sii.parent
		where si.docstatus = 1 and si.is_return = 0 and sii.so_detail in %(lines)s
		order by si.posting_date, si.creation, sii.idx""",
		{"lines": tuple(per_line)},
		as_dict=True,
	)
	to_credit: dict[str, dict[str, float]] = defaultdict(dict)  # {invoice: {invoice row: qty}}
	credited: dict[str, dict[str, float]] = {}
	left = dict(per_line)
	for r in invoice_rows:
		done = credited.setdefault(r.parent, _credited_qty(r.parent))
		take = min(flt(r.qty) - done.get(r.name, 0), left[r.so_detail])
		if take > EPSILON:
			to_credit[r.parent][r.name] = take
			left[r.so_detail] -= take
	sales_order = sales_order_of(dn)
	notes = []
	for invoice, rows in to_credit.items():
		notes.append(_credit_not_installed(invoice, rows, dn.name))
		tv_accounts.remove_from_sale(invoice, sales_order, tv_accounts.tv_units(
			[{"item_code": t.item_code, "qty": t.qty, "conversion_factor": t.conversion_factor} for t in taken]))
	if sales_order:
		settle_order(sales_order)
	return notes


def _credit_not_installed(invoice: str, rows: dict[str, float], delivery_note: str) -> str:
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

	cn = make_sales_return(invoice)
	cn.set("items", [r for r in cn.items if r.sales_invoice_item in rows])
	for r in cn.items:
		qty = rows[r.sales_invoice_item]
		r.qty, r.stock_qty = -qty, -qty * flt(r.conversion_factor or 1)
	cn.update({"update_stock": 0, "is_pos": 0, "update_outstanding_for_self": 1,
		"update_billed_amount_in_sales_order": 1,
		"remarks": _("Not delivered: taken off {0} by the driver.").format(delivery_note)})
	cn.set("payments", [])
	cn.flags.ignore_permissions = True
	cn.insert()
	cn.submit()
	_clear_debt(invoice, cn.name)
	return cn.name


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
	"""After a return, a delivery or a payment of an order that had a return: recount the
	store's reservation, free the bookings of a delivery type with nothing left to deliver
	that was never delivered, and close the order once it is done (``order_done``).
	ERPNext completes by itself an order delivered and billed in full."""
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
	if not pending and order_done(sales_order) \
			and frappe.db.get_value("Sales Order", sales_order, "status") not in ("Closed", "Completed"):
		so = frappe.get_doc("Sales Order", sales_order)
		so.flags.ignore_permissions = True
		so.update_status("Closed")
		reservation.release_undelivered(sales_order)


def order_done(sales_order: str) -> bool:
	"""No Delivery Note of the order is waiting or On the way, and its invoices are paid
	(the Credit Notes of its returns cleared their part)."""
	open_notes = frappe.db.sql(
		"""select 1 from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
		where dni.against_sales_order = %s and dn.is_return = 0
		and (dn.docstatus = 0 or (dn.docstatus = 1 and dn.dm_delivery_status = %s)) limit 1""",
		(sales_order, ON_THE_WAY),
	)
	owed = frappe.db.sql(
		"""select 1 from `tabSales Invoice` si where si.docstatus = 1 and si.is_return = 0
		and si.outstanding_amount > %s and exists (select 1 from `tabSales Invoice Item` sii
			where sii.parent = si.name and sii.sales_order = %s) limit 1""",
		(EPSILON_MONEY, sales_order),
	)
	return not open_notes and not owed


def settle_orders_of(delivery_note: str) -> None:
	for sales_order in frappe.get_all("Delivery Note Item", filters={"parent": delivery_note,
			"against_sales_order": ["is", "set"]}, pluck="against_sales_order", distinct=True):
		settle_order(sales_order)


# ---------------------------------------------------------------------------
# Delivery Note and Payment Entry hooks
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


def flag_before_delivery(dn, method=None):
	"""Return Delivery Note ``before_submit``: goods back from a note still On the way were
	never delivered or installed (Installation Statistics leave them out)."""
	if cint(dn.get("is_return")) and dn.get("return_against"):
		dn.dm_returned_before_delivery = frappe.db.get_value(DN, dn.return_against, "dm_delivery_status") == ON_THE_WAY


def settle_after_delivery(dn, method=None):
	"""Delivery Note ``on_submit``: an order with returned goods closes after its last delivery."""
	if not cint(dn.get("is_return")):
		settle_orders_of(dn.name)


def after_return(dn, method=None):
	"""Return Delivery Note ``on_submit`` / ``on_cancel``.

	* The order lines' ``returned_qty`` is set again to what the customer gave back for good
	  (ERPNext counts every Return Delivery Note in it): the Customer Return notes' goods
	  and the goods cancelled before delivery. The store's reservation is recounted with it.
	* The note it returns shows its AC Units net of the returned units.
	"""
	if not cint(dn.get("is_return")):
		return
	lines: dict[str, set] = defaultdict(set)
	for r in dn.items:
		if r.get("so_detail") and r.get("against_sales_order"):
			lines[r.against_sales_order].add(r.so_detail)
	for sales_order, so_details in lines.items():
		for so_detail in so_details:
			frappe.db.set_value(SOI, so_detail, "returned_qty", returned_for_good(so_detail), update_modified=False)
		# ERPNext counted the store's reservation with its own returned_qty.
		frappe.get_doc("Sales Order", sales_order).update_reserved_qty(list(so_details))
	if dn.get("return_against"):
		update_returned_note(dn.return_against)
	for sales_order in lines:
		settle_order(sales_order)


def returned_for_good(so_detail: str) -> float:
	"""Order line quantity the customer gave back or cancelled through Customer Returns."""
	given_back = frappe.db.sql(
		"""select ifnull(sum(-dni.qty), 0) from `tabDelivery Note Item` dni
		join `tabDelivery Note` dn on dn.name = dni.parent
		where dn.docstatus = 1 and dn.is_return = 1 and dn.dm_customer_return = 1 and dni.so_detail = %s""",
		so_detail,
	)[0][0]
	return flt(given_back) + flt(frappe.db.get_value(SOI, so_detail, "dm_cancelled_qty"))


def update_returned_note(delivery_note: str) -> None:
	dn = frappe.get_doc(DN, delivery_note)
	if dn.dm_delivery_type == asg.TYPE_AC:
		left = [{"item_code": r.item_code, "qty": net_qty(r)} for r in dn.items]
		dn.db_set("dm_ac_units", profile_cart([r for r in left if r["qty"] > EPSILON]).ac_units, update_modified=False)


def net_qty(row) -> float:
	"""Quantity of a Delivery Note row that was not brought back (row UOM)."""
	return flt(row.qty) - flt(row.returned_qty) / (flt(row.conversion_factor) or 1)


def settle_after_payment(pe, method=None):
	"""Payment Entry ``on_submit``: an order paid in full after a return can close."""
	invoices = [r.reference_name for r in pe.get("references") or [] if r.reference_doctype == SI]
	if not invoices:
		return
	for sales_order in frappe.get_all("Sales Invoice Item", filters={"parent": ["in", invoices],
			"sales_order": ["is", "set"]}, pluck="sales_order", distinct=True):
		settle_order(sales_order)
