"""Technicians see only their trips' tickets (Issues), the workshop only the tickets that reached
it, in the desk too (the same rule as ``tickets.can_view``).

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
	roles = set(frappe.get_roles(user))
	conditions = []
	if ROLE_TECH in roles:
		conditions.append(f"`tabIssue`.sm_driver = {u} or `tabIssue`.sm_technician = {u}")
	if ROLE_MAINT in roles:
		conditions.append("`tabIssue`.sm_step not in ('', 'Customer Service', 'Pickup')")
	return "(" + " or ".join(conditions) + ")"


def has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if not only_own_tickets(user):
		return True
	from delivery_management.customer_support.tickets import can_view

	return can_view(doc, user)


def on_issue_trash(doc, method=None):
	from delivery_management.slots import engine

	engine.release(doc.doctype, doc.name)
