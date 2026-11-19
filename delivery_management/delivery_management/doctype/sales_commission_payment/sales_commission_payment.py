"""Sales Commission Payment: pays a salesman his commission, month by month.

* The numbers come from ``stats.sales.unpaid_commission``, the same code as the Sales
  Statistics page: each month's % from that whole month's small items against its rule
  (User ▸ Commission Rules, or the closest one), 75% on the sales + 25% × the evaluation.
* An invoice's commission is paid once: on submit each invoice gets
  ``dm_commission_payment`` = this payment (under a row lock), and Get Unpaid reads only
  invoices without it, through an index. Cancel clears it, so they are unpaid again.
* Unpaid sales of months before From Month (paid late by the customer) are brought too,
  marked Late, at their own month's %.
* Submit posts a Journal Entry: the Sales Commission expense to the cash / bank account
  it is paid from. Cancel cancels it.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_months, flt, getdate, today

from delivery_management.stats import sales

ACCOUNT_NAME = "Commission on Sales"  # ERPNext's own account (standard chart of accounts)
BASIS = {"Total (incl. VAT)": "total", "Net Total (excl. VAT)": "net_total"}
EVENT = "dm_commission_unpaid"
HANDOFF_SECONDS = 60 * 60


def commission_account_of(company: str) -> str | None:
	return frappe.db.get_value("Account", {"account_name": ACCOUNT_NAME, "company": company, "is_group": 0})


@frappe.whitelist()
def default_commission_account(company: str) -> str | None:
	"""The form's default Commission Account (it can be changed)."""
	frappe.has_permission("Sales Commission Payment", "create", throw=True)
	return commission_account_of(company)


class SalesCommissionPayment(Document):
	def validate(self):
		self.to_month = sales.month_start(self.to_month)
		self.from_month = sales.month_start(self.from_month) if self.from_month else None
		if self.from_month and self.from_month > self.to_month:
			frappe.throw(_("From Month must be on or before To Month."))
		if sales.month_end(self.to_month) >= getdate(today()):
			frappe.throw(_("{0} has not ended yet. Only a month that has ended can be paid.").format(
				self.to_month.strftime("%B %Y")))
		self.commission_account = self.commission_account or commission_account_of(self.company)
		self.cost_center = self.cost_center or frappe.get_cached_value("Company", self.company, "cost_center")
		self._check_accounts()
		self._set_rows()

	def _check_accounts(self):
		paid_from = frappe.db.get_value("Account", self.paid_from, ["company", "account_type", "is_group"], as_dict=True)
		if not paid_from or paid_from.company != self.company or paid_from.is_group \
				or paid_from.account_type not in ("Cash", "Bank"):
			frappe.throw(_("Paid From must be a cash or bank account of {0}.").format(self.company))
		if not self.commission_account:
			frappe.throw(_("Choose the Commission Account (the company has no {0} account).").format(ACCOUNT_NAME))
		account = frappe.db.get_value("Account", self.commission_account, ["company", "root_type", "is_group"],
			as_dict=True)
		if not account or account.company != self.company or account.is_group or account.root_type != "Expense":
			frappe.throw(_("The Commission Account must be an expense account of {0}.").format(self.company))

	def _set_rows(self):
		"""Every row from the server, for the invoices in the list: each one must still be an
		unpaid sale of this salesman up to To Month."""
		names = [r.sales_invoice for r in self.invoices]
		twice = {n for n in names if names.count(n) > 1}
		if twice:
			frappe.throw(_("{0} is in the list more than once.").format(", ".join(sorted(twice))))
		result = self.calculate(names)
		found = {r["sales_invoice"] for r in result["invoices"]}
		wrong = [n for n in names if n not in found]
		if wrong:
			frappe.throw(_("{0}: not a sale of {1} with unpaid commission up to {2}.").format(
				", ".join(wrong), self.salesman, self.to_month.strftime("%B %Y")))
		self.set("invoices", result["invoices"])
		self.set("months", result["months"])
		self.total_amount = result["total"]

	def calculate(self, names=None) -> dict:
		return sales.unpaid_commission(self.salesman, self.company, self.to_month, bool(self.exclude_services),
			BASIS[self.basis], from_month=self.from_month, names=names)

	def before_submit(self):
		if not self.invoices:
			frappe.throw(_("There is nothing to pay."))
		names = [r.sales_invoice for r in self.invoices]
		# Locked, so two payments cannot both pay one invoice.
		taken = frappe.db.sql("""select name, dm_commission_payment from `tabSales Invoice`
			where name in %(names)s for update""", {"names": names}, as_dict=True)
		paid = sorted(r.name for r in taken if r.dm_commission_payment)
		if paid:
			frappe.throw(_("Commission already paid: {0}.").format(", ".join(paid)), title=_("Paid before"))

	def on_submit(self):
		frappe.db.sql("""update `tabSales Invoice` set dm_commission_payment = %(me)s
			where name in %(names)s and dm_commission_payment is null""",
			{"me": self.name, "names": [r.sales_invoice for r in self.invoices]})
		# A month whose returns cancel its sales pays 0: its invoices are closed, with no entry.
		if flt(self.total_amount) <= 0:
			return
		bank = frappe.get_cached_value("Account", self.paid_from, "account_type") == "Bank"
		je = frappe.get_doc({
			"doctype": "Journal Entry",
			"voucher_type": "Bank Entry" if bank else "Cash Entry",
			"company": self.company,
			"posting_date": self.posting_date,
			"cheque_no": self.name if bank else None,
			"cheque_date": self.posting_date if bank else None,
			"user_remark": _("Sales commission {0} to {1}").format(self.name, self.salesman_name or self.salesman)
				+ (f": {self.remarks}" if self.remarks else ""),
			"accounts": [
				{"account": self.commission_account, "debit_in_account_currency": self.total_amount,
					"cost_center": self.cost_center},
				{"account": self.paid_from, "credit_in_account_currency": self.total_amount,
					"cost_center": self.cost_center},
			],
		})
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
		self.db_set("journal_entry", je.name)

	def on_cancel(self):
		frappe.db.sql("update `tabSales Invoice` set dm_commission_payment = null where dm_commission_payment = %s",
			self.name)
		if self.journal_entry:
			je = frappe.get_doc("Journal Entry", self.journal_entry)
			if je.docstatus == 1:
				je.flags.ignore_permissions = True
				je.cancel()


# ---------------------------------------------------------------- Get Unpaid (background)


def _key(run_id: str) -> str:
	return f"dm_commission_unpaid:{run_id}"


def _store(run_id: str, value: dict) -> None:
	frappe.cache().set_value(_key(run_id), value, expires_in_sec=HANDOFF_SECONDS)


@frappe.whitelist()
def start_unpaid(salesman, company, to_month, exclude_services=1, basis="Total (incl. VAT)", from_month=None):
	"""Queue the calculation of the salesman's unpaid commission. Returns ``{key, status[, result]}``."""
	frappe.has_permission("Sales Commission Payment", "create", throw=True)
	if basis not in BASIS:
		frappe.throw(_("Choose what the commission is on."))
	run_id = frappe.generate_hash(length=20)
	args = {"salesman": salesman, "company": company, "to_month": str(sales.month_start(to_month)),
		"exclude_services": bool(frappe.utils.cint(exclude_services)), "basis": BASIS[basis],
		"from_month": str(sales.month_start(from_month)) if from_month else None}
	_store(run_id, {"status": "queued", "by": frappe.session.user})
	frappe.enqueue("delivery_management.delivery_management.doctype.sales_commission_payment."
		"sales_commission_payment.run_unpaid", queue="long", timeout=1800, now=bool(frappe.flags.in_test),
		run_id=run_id, args=args, by=frappe.session.user)
	return unpaid_status(run_id)


def run_unpaid(run_id: str, args: dict, by: str):
	_store(run_id, {"status": "running", "by": by})
	try:
		result = sales.unpaid_commission(args["salesman"], args["company"], args["to_month"], args["exclude_services"],
			args["basis"], from_month=args["from_month"])
		_store(run_id, {"status": "done", "by": by, "result": result})
	except Exception:
		frappe.log_error(title="Sales Commission: Get Unpaid failed")
		_store(run_id, {"status": "failed", "by": by, "error": _("The commission could not be calculated. See the Error Log.")})
	frappe.publish_realtime(EVENT, {"key": run_id}, user=by)


@frappe.whitelist()
def unpaid_status(key):
	frappe.has_permission("Sales Commission Payment", "create", throw=True)
	state = frappe.cache().get_value(_key(key), expires=True, use_local_cache=False) or {"status": "expired"}
	if state.get("by") and state["by"] != frappe.session.user:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return {"key": key, **{k: v for k, v in state.items() if k != "by"}}


@frappe.whitelist()
def next_month(salesman: str) -> str | None:
	"""The month after the one the salesman's last submitted payment covers up to."""
	frappe.has_permission("Sales Commission Payment", "create", throw=True)
	last = frappe.get_all("Sales Commission Payment", filters={"salesman": salesman, "docstatus": 1},
		fields=["to_month"], order_by="to_month desc, creation desc", limit=1)
	return str(getdate(add_months(last[0].to_month, 1))) if last else None
