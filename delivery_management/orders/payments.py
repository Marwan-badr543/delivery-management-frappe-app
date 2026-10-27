"""Customer money outside a sale: a payment on a sale that is still owed, or cash out of
the customer's credit.

* ``collect``: the customer pays (part of) a sale. An unpaid Order's invoice is still a
  draft (``orders.service``); it is submitted first, then paid.
* ``pay_out``: the customer takes in cash the credit that returns left (the Credit
  Notes still open), oldest first.

Both are Payment Entries. From the POS their Reference No is the cashier's shift, so the
shift closing counts them (a payment in, a pay-out out of the drawer); on delivery
(Delivery Schedule page) it is the Delivery Note.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate

SI = "Sales Invoice"
EPSILON = 0.0005


def submit_draft_order(invoice: str) -> None:
	"""The customer pays an unpaid Order: its draft invoice is submitted (still unpaid).

	An order closed by a return (nothing left to deliver) is opened for the submit, as
	ERPNext bills no closed order, and closed again."""
	from delivery_management.returns.service import settle_order

	si = frappe.get_doc(SI, invoice)
	if si.docstatus != 0:
		return
	closed = [frappe.get_doc("Sales Order", name) for name in {r.sales_order for r in si.items if r.sales_order}
		if frappe.db.get_value("Sales Order", name, "status") == "Closed"]
	for so in closed:
		so.flags.ignore_permissions = True
		so.update_status("Draft")  # ERPNext's "Re-open"
	si.flags.ignore_permissions = True
	si.submit()
	for so in closed:
		settle_order(so.name)


def collect(invoice: str, amount, mode_of_payment: str, reference_no: str, remarks: str):
	"""Payment Entry of ``amount`` for the invoice, at most what it still owes."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

	amount = flt(amount, 3)
	if amount <= 0:
		frappe.throw(_("Enter an amount greater than zero."))
	submit_draft_order(invoice)
	# Under a lock, so two payments cannot both pass the check.
	outstanding = flt(frappe.db.sql("select outstanding_amount from `tabSales Invoice` where name = %s for update",
		invoice)[0][0], 3)
	if outstanding <= 0:
		frappe.throw(_("{0} is already fully paid.").format(invoice))
	if amount > outstanding + EPSILON:
		frappe.throw(_("Amount paid ({0}) cannot be greater than the outstanding amount ({1}).").format(
			amount, outstanding))
	pe = get_payment_entry(SI, invoice, party_amount=amount)
	pe.mode_of_payment = mode_of_payment
	pe.paid_to = get_bank_cash_account(mode_of_payment, pe.company)["account"]
	pe.paid_to_account_currency = frappe.get_cached_value("Account", pe.paid_to, "account_currency")
	pe.paid_amount = pe.received_amount = amount
	for ref in pe.references:
		ref.allocated_amount = amount if ref.reference_name == invoice else 0
	pe.update({"reference_no": reference_no, "reference_date": nowdate(), "remarks": remarks})
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe


def open_credit_notes(customer: str, company: str, lock: bool = False) -> list:
	"""Returns of the customer whose value was not refunded or used yet, oldest first."""
	return frappe.db.sql(
		f"""select name, -outstanding_amount as credit from `tabSales Invoice`
		where customer = %s and company = %s and docstatus = 1 and is_return = 1 and outstanding_amount < 0
		order by posting_date, creation {"for update" if lock else ""}""",
		(customer, company),
		as_dict=True,
	)


def pay_out(customer: str, company: str, amount, mode_of_payment: str, reference_no: str, remarks: str):
	"""Payment Entry (Pay) giving the customer ``amount`` of their credit in cash."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

	amount = flt(amount, 3)
	if amount <= 0:
		frappe.throw(_("Enter an amount greater than zero."))
	notes = open_credit_notes(customer, company, lock=True)
	credit = flt(sum(n.credit for n in notes), 3)
	if amount > credit + EPSILON:
		frappe.throw(_("The customer has {0} of credit; that is the most that can be paid out.").format(credit),
			title=_("Not enough credit"))
	pe = get_payment_entry(SI, notes[0].name)
	pe.set("references", [])
	left = amount
	for note in notes:
		take = min(note.credit, left)
		if take <= EPSILON:
			break
		# A Credit Note is paid out with a negative allocation (ERPNext's own sign).
		pe.append("references", {"reference_doctype": SI, "reference_name": note.name, "allocated_amount": -take})
		left = flt(left - take, 3)
	pe.mode_of_payment = mode_of_payment
	pe.paid_from = get_bank_cash_account(mode_of_payment, company)["account"]
	pe.paid_from_account_currency = frappe.get_cached_value("Account", pe.paid_from, "account_currency")
	pe.paid_amount = pe.received_amount = amount
	pe.update({"reference_no": reference_no, "reference_date": nowdate(), "remarks": remarks})
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe


def available_credit(customer: str, company: str) -> list[dict]:
	"""POS Awesome's Customer Credit, each return's credit read from its outstanding amount.

	POS Awesome also subtracts the cash paid out against a return, which ERPNext has
	already taken off the outstanding amount, so a credit paid out from the drawer would
	be counted twice. Advances (unallocated payments) are POS Awesome's own.
	"""
	from posawesome.posawesome.api.payments import get_available_credit

	invoices = frappe.get_all(SI, filters={"outstanding_amount": ["<", 0], "docstatus": 1, "customer": customer,
		"company": company}, fields=["name", "outstanding_amount", "is_return"], order_by="posting_date, creation")
	return [{"type": "Invoice", "credit_origin": r.name, "total_credit": -flt(r.outstanding_amount),
		"credit_to_redeem": 0, "source_type": "Sales Return" if r.is_return else "Sales Invoice"} for r in invoices] + \
		[r for r in get_available_credit(customer, company) if r["type"] != "Invoice"]
