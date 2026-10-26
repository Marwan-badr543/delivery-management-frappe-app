"""Endpoints of the POS Return / Exchange screen (``public/js/pos_returns.bundle.js``).

The cashier must be a user of the POS Profile; the documents a return makes (Credit
Note, Return Delivery Note, refund Payment Entry) are then made for him by
``returns.service``.
"""

import json

import frappe
from frappe import _

from delivery_management.orders.service import quiet_messages
from delivery_management.returns import service


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
		"""select si.name, si.customer, si.customer_name, si.contact_mobile, si.posting_date,
			si.rounded_total, si.grand_total, si.outstanding_amount, si.currency, si.update_stock,
			(select soi.sales_order from `tabSales Invoice Item` soi where soi.parent = si.name
				and ifnull(soi.sales_order, '') != '' limit 1) as sales_order
		from `tabSales Invoice` si
		where si.docstatus = 1 and si.is_return = 0 and si.company = %(company)s
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
