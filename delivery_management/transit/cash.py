"""Cash with the drivers: what a driver / AC Tech collects in cash on delivery, until the
office receives it.

* **Collected** (Delivery Schedule, ``api.drivers.record_payment``): a payment in a
  Mode of Payment of type *Cash* goes into the company's **Cash With Drivers** account
  (not the store's cash), and a **Driver Cash** row says who holds how much, for which
  order. Other modes (card, transfer) are recorded as before.
* **Received** (Goods in Transit page, ``receive``): the office counts the money; one
  Journal Entry moves it from Cash With Drivers into the store's cash account (the Cash
  mode's default account), each order logs "Cash handed over" and the rows are removed.
  So the Cash With Drivers balance is always the sum of the open rows.
* A Payment Entry cancelled before the hand-over takes its row with it.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, get_fullname, now_datetime, nowdate

from delivery_management.control import log

CASH = "Driver Cash"
ACCOUNT_NAME = "Cash With Drivers"


def is_cash(mode_of_payment: str | None) -> bool:
	return bool(mode_of_payment) and frappe.get_cached_value("Mode of Payment", mode_of_payment, "type") == "Cash"


def store_cash_account(mode_of_payment: str, company: str) -> str:
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

	return get_bank_cash_account(mode_of_payment, company)["account"]


def drivers_account(company: str) -> str:
	account = frappe.db.get_value("Account", {"account_name": ACCOUNT_NAME, "company": company, "is_group": 0})
	if not account:
		frappe.throw(_("The {0} account of {1} is missing. Run bench migrate to create it.").format(ACCOUNT_NAME, company))
	return account


def ensure_accounts():
	"""``after_migrate``: a Cash With Drivers account next to each company's cash account."""
	for company in frappe.get_all("Company", pluck="name"):
		if frappe.db.exists("Account", {"account_name": ACCOUNT_NAME, "company": company}):
			continue
		parent = frappe.db.get_value("Account", {"account_type": "Cash", "company": company, "is_group": 1}) or \
			frappe.db.get_value("Account", {"account_name": "Cash In Hand", "company": company, "is_group": 1})
		if parent:
			frappe.get_doc({"doctype": "Account", "account_name": ACCOUNT_NAME, "parent_account": parent,
				"company": company, "account_type": "Cash"}).insert(ignore_permissions=True)


def hold(payment_entry, delivery_note: str, driver: str):
	"""A cash payment the driver took: record it on him (the Payment Entry was paid into
	the Cash With Drivers account)."""
	from delivery_management.drivers.delivery_note import sales_order_of

	dn = frappe.get_doc("Delivery Note", delivery_note)
	invoice = next((r.reference_name for r in payment_entry.references if r.reference_doctype == "Sales Invoice"), None)
	frappe.get_doc({
		"doctype": CASH, "driver": driver, "driver_name": get_fullname(driver), "amount": flt(payment_entry.paid_amount),
		"currency": payment_entry.paid_to_account_currency, "collected_at": now_datetime(),
		"delivery_note": delivery_note, "sales_order": sales_order_of(dn), "sales_invoice": invoice,
		"customer": dn.customer, "customer_name": dn.customer_name, "company": payment_entry.company,
		"mode_of_payment": payment_entry.mode_of_payment, "payment_entry": payment_entry.name,
	}).insert(ignore_permissions=True)


def on_payment_cancel(doc, method=None):
	"""Payment Entry ``on_cancel``: money never handed over is no longer with the driver."""
	frappe.db.delete(CASH, {"payment_entry": doc.name})


def receive(names: list[str]) -> list[str]:
	"""The office received these rows' cash: one Journal Entry per company and mode, the
	orders logged, the rows removed. Returns the Journal Entries."""
	names = list(dict.fromkeys(names or []))
	if not names:
		frappe.throw(_("Choose the cash to receive."))
	# Locked, so the same cash cannot be received twice at the same time.
	rows = frappe.db.sql(f"select * from `tab{CASH}` where name in %(names)s for update", {"names": names}, as_dict=True)
	if len(rows) != len(names):
		frappe.throw(_("Some of this cash was already received. Refresh the page."), title=_("Already received"))

	groups = defaultdict(list)
	for r in rows:
		groups[(r.company, r.mode_of_payment)].append(r)
	entries = []
	for (company, mode), group in groups.items():
		total = flt(sum(flt(r.amount) for r in group), 3)
		drivers = ", ".join(sorted({r.driver_name or r.driver for r in group}))
		je = frappe.get_doc({
			"doctype": "Journal Entry", "voucher_type": "Cash Entry", "company": company, "posting_date": nowdate(),
			"user_remark": _("Cash handed over by {0}: {1}").format(drivers, ", ".join(r.delivery_note for r in group)),
			"accounts": [
				{"account": store_cash_account(mode, company), "debit_in_account_currency": total},
				{"account": drivers_account(company), "credit_in_account_currency": total},
			],
		})
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
		entries.append(je.name)
		for r in group:
			log.add("Cash handed over", sales_order=r.sales_order, sales_invoice=r.sales_invoice,
				delivery_note=r.delivery_note, driver=r.driver, amount=flt(r.amount), mode_of_payment=r.mode_of_payment,
				reference=("Journal Entry", je.name))
	frappe.db.delete(CASH, {"name": ["in", names]})
	return entries
