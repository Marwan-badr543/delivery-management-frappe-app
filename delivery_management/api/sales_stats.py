"""Endpoints for the Sales Statistics page.

Every click on *Show* is a **fresh calculation** (nothing is reused between runs):
``start`` queues a background job (queue ``long``) with its own run id and
returns at once. The job stores its result in Redis under that run id only so
the page can pick it up (realtime event ``dm_sales_stats``, with polling as a
fallback); the stored copy is deleted after an hour and is never served for
another run. The run holds the per-salesman summary only; the invoices behind it
are read a page at a time with ``get_invoices``.

Only System Manager sees every salesman; anyone else with page access sees only their own sales.
"""

import frappe
from frappe import _
from frappe.utils import cint, getdate

from delivery_management.stats import sales

MANAGER_ROLES = ("System Manager", "Sales Manager", "Accounts Manager")
PAGE_ROLES = MANAGER_ROLES + ("Sales User",)
HANDOFF_SECONDS = 60 * 60  # how long a finished run waits for the page to fetch it
EVENT = "dm_sales_stats"


def _is_manager() -> bool:
	"""Only a System Manager sees every salesman's sales."""
	return frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()


def _filters(from_date, to_date, company=None, user=None, exclude_services=1, pos_profile=None) -> dict:
	frappe.only_for(PAGE_ROLES)
	if not from_date or not to_date:
		frappe.throw(_("Choose the From and To dates."))
	if getdate(from_date) > getdate(to_date):
		frappe.throw(_("From Date must be on or before To Date."))
	company = company or frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company")
	if not company:
		frappe.throw(_("Choose a company."))
	if not frappe.has_permission("Company", "read", company):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not _is_manager():
		user = frappe.session.user  # salesmen see only their own numbers
	return {"from_date": str(getdate(from_date)), "to_date": str(getdate(to_date)), "company": company,
		"user": user or None, "exclude_services": bool(cint(exclude_services)), "pos_profile": pos_profile or None}


def _redis_key(run_id: str) -> str:
	return f"dm_sales_stats_run:{run_id}"


def _get(run_id: str):
	# Always read Redis, never a request-local copy (the job writes from another process).
	return frappe.cache().get_value(_redis_key(run_id), expires=True, use_local_cache=False)


def _set(run_id: str, value: dict) -> None:
	frappe.cache().set_value(_redis_key(run_id), value, expires_in_sec=HANDOFF_SECONDS)


@frappe.whitelist()
def get_setup():
	frappe.only_for(PAGE_ROLES)
	return {
		"is_manager": _is_manager(),
		"can_evaluate": bool(frappe.has_permission("Sales Evaluation", "create")),
		"user": frappe.session.user,
		"company": frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company"),
	}


@frappe.whitelist()
def start(from_date, to_date, company=None, user=None, exclude_services=1, pos_profile=None):
	"""Queue a fresh calculation. Returns ``{key, status[, result]}``."""
	filters = _filters(from_date, to_date, company, user, exclude_services, pos_profile)
	run_id = frappe.generate_hash(length=20)
	_set(run_id, {"status": "queued", "requested_by": frappe.session.user})
	frappe.enqueue(
		"delivery_management.api.sales_stats.run_job",
		queue="long",
		timeout=1800,
		now=bool(frappe.flags.in_test),
		run_id=run_id,
		filters=filters,
		requested_by=frappe.session.user,
	)
	return status(run_id)


@frappe.whitelist()
def get_invoices(from_date, to_date, company=None, user=None, search=None, page=0, all_pages=0, exclude_services=1,
		pos_profile=None):
	"""One page of the invoices counted for these filters (``all_pages=1``: every one, for the CSV)."""
	filters = _filters(from_date, to_date, company, user, exclude_services, pos_profile)
	return sales.invoices(filters["from_date"], filters["to_date"], filters["company"], filters["user"],
		search=search, page=page, page_len=None if cint(all_pages) else sales.INVOICES_PER_PAGE,
		exclude_services=filters["exclude_services"], pos_profile=filters["pos_profile"])


@frappe.whitelist()
def status(key):
	frappe.only_for(PAGE_ROLES)
	state = _get(key) or {"status": "expired"}
	if state.get("requested_by") and state["requested_by"] != frappe.session.user:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return {"key": key, **{k: v for k, v in state.items() if k != "requested_by"}}


def run_job(run_id: str, filters: dict, requested_by: str):
	_set(run_id, {"status": "running", "requested_by": requested_by})
	try:
		result = sales.compute(filters["from_date"], filters["to_date"], filters["company"], filters.get("user"),
			filters.get("exclude_services", True), filters.get("pos_profile"))
		_set(run_id, {"status": "done", "result": result, "requested_by": requested_by})
	except Exception:
		frappe.log_error(title="Sales Statistics failed")
		_set(run_id, {"status": "failed", "requested_by": requested_by,
			"error": _("The statistics could not be computed. See the Error Log.")})
	frappe.publish_realtime(EVENT, {"key": run_id}, user=requested_by)
