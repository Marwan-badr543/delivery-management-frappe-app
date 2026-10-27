"""Endpoints of the POS Return / Exchange screen (``public/js/pos_returns.bundle.js``).

The cashier must be a user of the POS Profile; the documents are then made for them:
a return by ``returns.service`` (Credit Note, Return Delivery Note), a customer's
payment or credit paid out by ``orders.payments`` (Payment Entry in the cashier's shift).
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

from delivery_management.orders import payments
from delivery_management.orders.service import quiet_messages
from delivery_management.returns import service

# A sale the return screen can open: submitted, or the draft invoice of an unpaid Order.
_RETURNABLE = """si.is_return = 0 and (si.docstatus = 1 or (si.docstatus = 0 and exists (select 1
	from `tabSales Invoice Item` o where o.parent = si.name and ifnull(o.sales_order, '') != '')))"""


def _profile(pos_profile):
	if not frappe.has_permission("Sales Invoice", "create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	doc = frappe.get_cached_doc("POS Profile", pos_profile)
	if frappe.session.user != "Administrator" and not any(
			row.user == frappe.session.user for row in doc.applicable_for_users):
		frappe.throw(_("You are not a user of POS Profile {0}.").format(pos_profile), frappe.PermissionError)
	return doc


@frappe.whitelist()
def search_sales(pos_profile, query=""):
	"""The latest sales matching an invoice / order number, customer name or mobile."""
	profile = _profile(pos_profile)
	like = f"%{(query or '').strip()}%"
	return frappe.db.sql(
		f"""select si.name, si.docstatus, si.customer, si.customer_name, si.contact_mobile, si.posting_date,
			si.rounded_total, si.grand_total, si.currency, si.update_stock,
			if(si.docstatus = 1, si.outstanding_amount, if(si.rounded_total = 0, si.grand_total, si.rounded_total))
				as outstanding_amount,
			(select soi.sales_order from `tabSales Invoice Item` soi where soi.parent = si.name
				and ifnull(soi.sales_order, '') != '' limit 1) as sales_order
		from `tabSales Invoice` si
		where {_RETURNABLE} and si.company = %(company)s
			and (si.name like %(q)s or si.customer like %(q)s or si.customer_name like %(q)s
				or ifnull(si.contact_mobile, '') like %(q)s
				or exists (select 1 from `tabSales Invoice Item` i where i.parent = si.name and i.sales_order like %(q)s))
		order by si.posting_date desc, si.creation desc
		limit 20""",
		{"company": profile.company, "q": like},
		as_dict=True,
	)


@frappe.whitelist()
def get_sale(pos_profile, invoice):
	profile = _profile(pos_profile)
	sale = service.get_sale(invoice)
	if sale["company"] != profile.company:
		frappe.throw(_("Invoice {0} belongs to another company.").format(invoice))
	sale["stores"] = frappe.get_all("Warehouse", filters={"company": profile.company, "is_group": 0, "disabled": 0},
		pluck="name", order_by="name")
	sale["default_store"] = profile.warehouse
	return sale


@frappe.whitelist(methods=["POST"])
def submit_return(data):
	data = json.loads(data) if isinstance(data, str) else data
	_profile(data.get("pos_profile"))
	# ERPNext's informational pop-ups (e.g. "Credit Note is made against ...") would open a
	# desk dialog over the POS; errors still raise.
	with quiet_messages():
		return service.make_return(data)


# ---------------------------------------------------------------- customer money


@frappe.whitelist()
def search_customers(pos_profile, query=""):
	"""Customers matching a name or mobile, with their credit and what they still owe."""
	profile = _profile(pos_profile)
	like = f"%{(query or '').strip()}%"
	return frappe.db.sql(
		f"""select c.name, c.customer_name, c.mobile_no,
			(select -ifnull(sum(si.outstanding_amount), 0) from `tabSales Invoice` si where si.customer = c.name
				and si.company = %(company)s and si.docstatus = 1 and si.is_return = 1 and si.outstanding_amount < 0)
				as credit,
			(select ifnull(sum(if(si.docstatus = 1, si.outstanding_amount,
					if(si.rounded_total = 0, si.grand_total, si.rounded_total))), 0)
				from `tabSales Invoice` si where si.customer = c.name and si.company = %(company)s and {_RETURNABLE}
				and (si.docstatus = 0 or si.outstanding_amount > 0)) as due
		from `tabCustomer` c
		where c.disabled = 0 and (c.name like %(q)s or c.customer_name like %(q)s or ifnull(c.mobile_no, '') like %(q)s)
		order by c.modified desc
		limit 20""",
		{"company": profile.company, "q": like},
		as_dict=True,
	)


@frappe.whitelist()
def get_customer_money(pos_profile, customer):
	"""The customer's credit (Credit Notes still open) and the sales they still owe on."""
	profile = _profile(pos_profile)
	due = frappe.db.sql(
		f"""select si.name, si.docstatus, si.posting_date,
			if(si.docstatus = 1, si.outstanding_amount, if(si.rounded_total = 0, si.grand_total, si.rounded_total))
				as outstanding_amount,
			if(si.rounded_total = 0, si.grand_total, si.rounded_total) as total,
			(select soi.sales_order from `tabSales Invoice Item` soi where soi.parent = si.name
				and ifnull(soi.sales_order, '') != '' limit 1) as sales_order
		from `tabSales Invoice` si
		where si.customer = %(customer)s and si.company = %(company)s and {_RETURNABLE}
			and (si.docstatus = 0 or si.outstanding_amount > 0)
		order by si.posting_date, si.creation""",
		{"customer": customer, "company": profile.company},
		as_dict=True,
	)
	return {
		"customer": customer,
		"customer_name": frappe.db.get_value("Customer", customer, "customer_name"),
		"currency": frappe.get_cached_value("Company", profile.company, "default_currency"),
		"credit": flt(sum(n.credit for n in payments.open_credit_notes(customer, profile.company)), 3),
		"due": due,
		"modes": _modes(profile),
	}


@frappe.whitelist(methods=["POST"])
def collect_payment(data):
	"""The customer pays (part of) a sale at the till: in the cashier's shift."""
	data = frappe._dict(json.loads(data) if isinstance(data, str) else data)
	profile = _profile(data.pos_profile)
	si = frappe.db.get_value("Sales Invoice", data.invoice, ["customer", "company"], as_dict=True)
	if not si or si.company != profile.company:
		frappe.throw(_("Invoice {0} is not a sale of this company.").format(data.invoice))
	return _once(data.request_id, lambda: payments.collect(data.invoice, data.amount, _mode(profile, data),
		service.open_shift(profile.name), _("Paid at the POS ({0})").format(data.notes or data.invoice)))


@frappe.whitelist(methods=["POST"])
def pay_out_credit(data):
	"""The customer takes (part of) their credit in cash, from the cashier's drawer."""
	data = frappe._dict(json.loads(data) if isinstance(data, str) else data)
	profile = _profile(data.pos_profile)
	if not (data.reason or "").strip():
		frappe.throw(_("Write why the credit is paid out."))
	return _once(data.request_id, lambda: payments.pay_out(data.customer, profile.company, data.amount,
		_mode(profile, data), service.open_shift(profile.name), _("Credit paid out at the POS: {0}").format(data.reason)))


@frappe.whitelist()
def get_available_credit(customer, company):
	"""Replaces POS Awesome's Customer Credit (``override_whitelisted_methods``): see
	``orders.payments.available_credit``."""
	return payments.available_credit(customer, company)


def _modes(profile) -> list[str]:
	cash = profile.get("posa_cash_mode_of_payment") or "Cash"
	return list(dict.fromkeys([cash, *(row.mode_of_payment for row in profile.payments)]))


def _mode(profile, data) -> str:
	mode = data.mode_of_payment or _modes(profile)[0]
	if mode not in _modes(profile):
		frappe.throw(_("{0} is not a payment method of this POS.").format(mode))
	return mode


def _once(request_id, make) -> dict:
	"""Runs ``make`` (a Payment Entry) once per request id: a retried request (double
	click, network drop) gets the first entry back."""
	key = f"dm_pos_money:{request_id}"
	done = request_id and frappe.cache.get_value(key)
	pe = frappe.get_doc("Payment Entry", done) if done else make()
	if request_id and not done:
		frappe.db.after_commit.add(lambda: frappe.cache.set_value(key, pe.name, expires_in_sec=86400))
	return {"payment_entry": pe.name, "amount": flt(pe.paid_amount), "mode_of_payment": pe.mode_of_payment,
		"party": pe.party, "payment_type": pe.payment_type}
