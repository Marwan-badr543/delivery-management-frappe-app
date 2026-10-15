"""Technicians see only the support tickets (Issues) assigned to them, in the desk too.

A user with a full-access role (System Manager, Support Team, ...) is not limited.
"""

import frappe

from delivery_management.customer_support.tickets import ROLE_MAINT, ROLE_TECH

LIMITED_ROLES = {ROLE_TECH, ROLE_MAINT}
# Roles that see every Issue anyway (ERPNext's own Issue permissions).
FULL_ROLES = {"System Manager", "Support Team", "Administrator", "Projects User", "HR User", "HR Manager"}


def only_own_tickets(user: str | None = None) -> bool:
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	return bool(roles & LIMITED_ROLES) and not roles & FULL_ROLES


def get_permission_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if not only_own_tickets(user):
		return ""
	u = frappe.db.escape(user)
	return f"(`tabIssue`.sm_technician = {u} or `tabIssue`.sm_maint_technician = {u})"


def has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if not only_own_tickets(user):
		return True
	return user in (doc.get("sm_technician"), doc.get("sm_maint_technician"))


def on_issue_trash(doc, method=None):
	from delivery_management.slots import engine

	engine.release(doc.doctype, doc.name)
