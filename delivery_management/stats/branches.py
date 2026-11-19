"""Branches for Sales Statistics: a Branch has a Sales Target and its POS Profiles.

A salesman's branch is found from the POS Profiles he may use (POS Profile ▸
Applicable for Users) and the Branch that lists that profile.
"""

from __future__ import annotations

import frappe
from frappe import _

CHILD = "Branch POS Profile"


def validate_branch(doc, method=None):
	"""A POS Profile counts for one branch only, so no sale is counted twice."""
	seen = set()
	for row in doc.get("dm_pos_profiles") or []:
		if row.pos_profile in seen:
			frappe.throw(_("POS Profile {0} is listed twice.").format(frappe.bold(row.pos_profile)))
		seen.add(row.pos_profile)
	if not seen:
		return
	taken = frappe.get_all(CHILD, filters={"parenttype": "Branch", "parent": ["!=", doc.name],
		"pos_profile": ["in", list(seen)]}, fields=["pos_profile", "parent"], limit=1)
	if taken:
		frappe.throw(_("POS Profile {0} already belongs to branch {1}.").format(
			frappe.bold(taken[0].pos_profile), frappe.bold(taken[0].parent)))


def profiles_of(branches: list[str]) -> list[str]:
	if not branches:
		return []
	return frappe.get_all(CHILD, filters={"parenttype": "Branch", "parent": ["in", branches]},
		pluck="pos_profile", distinct=True)


def branches_of_user(user: str) -> list[str]:
	"""Branches of the POS Profiles this user may use."""
	profiles = frappe.get_all("POS Profile User", filters={"parenttype": "POS Profile", "user": user},
		pluck="parent", distinct=True)
	if not profiles:
		return []
	return sorted(set(frappe.get_all(CHILD, filters={"parenttype": "Branch", "pos_profile": ["in", profiles]},
		pluck="parent")))


def all_branches() -> list[str]:
	"""Branches with at least one POS Profile."""
	return sorted(set(frappe.get_all(CHILD, filters={"parenttype": "Branch"}, pluck="parent")))
