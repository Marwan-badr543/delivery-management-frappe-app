"""AC Tech Payment: pays an AC Tech for the AC units of delivered installations.

* A row is one delivered AC Delivery Note of the AC Tech. Its units come from the server
  (``drivers.stats``, the goods returned before delivery taken off) and its cost per unit
  is the AC Tech's "AC Unit Installation Cost" (User) when the payment is submitted;
  the row keeps it after.
* An installation is paid once: a note on a submitted payment cannot be paid again.
* Submit posts a Journal Entry: the expense account (AC Installation Cost) to the cash /
  bank account it is paid from. Cancel cancels it, and the installations are unpaid again.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from delivery_management.drivers import assignment as asg
from delivery_management.drivers import stats

EXPENSE_ACCOUNT_NAME = "AC Installation Cost"


def expense_account_of(company: str) -> str | None:
	return frappe.db.get_value("Account", {"account_name": EXPENSE_ACCOUNT_NAME, "company": company, "is_group": 0})


class ACTechPayment(Document):
	def validate(self):
		if asg.ROLE_FOR_TYPE[asg.TYPE_AC] not in frappe.get_roles(self.technician):
			frappe.throw(_("{0} is not an AC Tech.").format(self.technician))
		self.expense_account = self.expense_account or expense_account_of(self.company)
		self.cost_center = self.cost_center or frappe.get_cached_value("Company", self.company, "cost_center")
		self._check_accounts()
		self._set_rows()

	def _check_accounts(self):
		paid_from = frappe.db.get_value("Account", self.paid_from, ["company", "account_type", "is_group"], as_dict=True)
		if not paid_from or paid_from.company != self.company or paid_from.is_group \
				or paid_from.account_type not in ("Cash", "Bank"):
			frappe.throw(_("Paid From must be a cash or bank account of {0}.").format(self.company))
		if not self.expense_account:
			frappe.throw(_("Choose the Expense Account."))
		expense = frappe.db.get_value("Account", self.expense_account, ["company", "root_type", "is_group"], as_dict=True)
		if not expense or expense.company != self.company or expense.is_group or expense.root_type != "Expense":
			frappe.throw(_("The Expense Account must be an expense account of {0}.").format(self.company))

	def _set_rows(self):
		"""Each row from the server: the note is a delivered AC installation of this AC Tech."""
		names = [r.delivery_note for r in self.items]
		twice = {n for n in names if names.count(n) > 1}
		if twice:
			frappe.throw(_("{0} is in the list more than once.").format(", ".join(sorted(twice))))
		notes = {n.name: n for n in stats.delivered_notes(technician=self.technician, order_type=asg.TYPE_AC, names=names)}
		wrong = [n for n in names if n not in notes]
		if wrong:
			frappe.throw(_("{0}: not a delivered AC installation of {1}.").format(", ".join(wrong), self.technician))
		units = stats.units_by_note(names)
		cost = flt(frappe.db.get_value("User", self.technician, "dm_ac_unit_cost"))
		if cost <= 0:
			frappe.throw(_("Set the AC Unit Installation Cost of {0} on the User first.").format(self.technician))
		for row in self.items:
			row.installation_date = notes[row.delivery_note].work_date
			row.ac_units = (units.get(row.delivery_note) or (0, 0))[0]
			if row.ac_units <= 0:
				frappe.throw(_("{0} has no AC unit installed.").format(row.delivery_note))
			row.unit_cost = cost
			row.amount = flt(row.ac_units * flt(row.unit_cost), 3)
		self.total_units = sum(cint(r.ac_units) for r in self.items)
		self.total_amount = flt(sum(flt(r.amount) for r in self.items), 3)

	def before_submit(self):
		# Under a lock on the AC Tech, so two payments cannot both pay one installation.
		frappe.db.sql("select name from `tabUser` where name = %s for update", self.technician)
		paid = stats.paid_notes([r.delivery_note for r in self.items])
		if paid:
			frappe.throw(_("Already paid: {0}.").format(", ".join(sorted(paid))), title=_("Paid before"))

	def on_submit(self):
		bank = frappe.get_cached_value("Account", self.paid_from, "account_type") == "Bank"
		je = frappe.get_doc({
			"doctype": "Journal Entry",
			"voucher_type": "Bank Entry" if bank else "Cash Entry",
			"company": self.company,
			"posting_date": self.posting_date,
			"cheque_no": self.name if bank else None,
			"cheque_date": self.posting_date if bank else None,
			"user_remark": _("AC installation payment {0} to {1}").format(self.name, self.technician_name or self.technician)
				+ (f": {self.remarks}" if self.remarks else ""),
			"accounts": [
				{"account": self.expense_account, "debit_in_account_currency": self.total_amount,
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
		if self.journal_entry:
			je = frappe.get_doc("Journal Entry", self.journal_entry)
			if je.docstatus == 1:
				je.flags.ignore_permissions = True
				je.cancel()


@frappe.whitelist()
def get_unpaid_installations(technician: str, up_to_date=None) -> list[dict]:
	"""Delivered AC installations of the AC Tech not paid yet, oldest first."""
	frappe.has_permission("AC Tech Payment", "create", throw=True)
	notes = stats.delivered_notes(end_date=up_to_date, technician=technician, order_type=asg.TYPE_AC)
	units = stats.units_by_note([n.name for n in notes])
	paid = stats.paid_notes(list(units))
	cost = flt(frappe.db.get_value("User", technician, "dm_ac_unit_cost"))
	names = {n.name: n for n in frappe.get_all("Delivery Note", filters={"name": ["in", list(units) or [""]]},
		fields=["name", "customer_name"])}
	rows = []
	for note in notes:
		ac_units = (units.get(note.name) or (0, 0))[0]
		if ac_units <= 0 or note.name in paid:
			continue
		rows.append({"delivery_note": note.name, "customer_name": names[note.name].customer_name,
			"installation_date": note.work_date, "ac_units": ac_units, "unit_cost": cost,
			"amount": flt(ac_units * cost, 3)})
	return rows


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def ac_tech_query(doctype, txt, searchfield, start, page_len, filters):
	"""AC Tech link search: enabled users with the AC Tech role."""
	txt = (txt or "").lower()
	techs = [(d.user, d.full_name) for d in asg.drivers_for(asg.TYPE_AC)
		if txt in d.user.lower() or txt in (d.full_name or "").lower()]
	return techs[cint(start): cint(start) + cint(page_len)]
