"""Endpoints of the Sales Dashboard page (managers only).

As on the Sales Statistics page, every load is a fresh calculation run as a background
job (queue ``long``): ``start`` queues it and returns at once; the job stores the result in
Redis under its own run id for the page to pick up (realtime event ``dm_sales_dashboard``,
polling as a fallback). The stored copy expires after an hour and serves no other run.
"""

import frappe
from frappe import _
from frappe.utils import getdate

from delivery_management.stats import dashboard

PAGE_ROLES = ("System Manager", "Sales Manager", "Accounts Manager")
HANDOFF_SECONDS = 60 * 60
EVENT = "dm_sales_dashboard"


def _redis_key(run_id: str) -> str:
	return f"dm_sales_dashboard_run:{run_id}"


def _get(run_id: str):
	# Always Redis, never a request-local copy (the job writes from another process).
	return frappe.cache().get_value(_redis_key(run_id), expires=True, use_local_cache=False)


def _set(run_id: str, value: dict) -> None:
	frappe.cache().set_value(_redis_key(run_id), value, expires_in_sec=HANDOFF_SECONDS)


@frappe.whitelist()
def start(start_date, end_date):
	"""Queue a fresh calculation. Returns ``{key, status[, result]}``."""
	frappe.only_for(PAGE_ROLES)
	start_date, end_date = getdate(start_date), getdate(end_date)
	if start_date > end_date:
		frappe.throw(_("Start date must be on or before the end date."))
	run_id = frappe.generate_hash(length=20)
	_set(run_id, {"status": "queued", "requested_by": frappe.session.user})
	frappe.enqueue(
		"delivery_management.api.dashboard.run_job",
		queue="long",
		timeout=1800,
		now=bool(frappe.flags.in_test),
		run_id=run_id,
		start_date=str(start_date),
		end_date=str(end_date),
		requested_by=frappe.session.user,
	)
	return status(run_id)


@frappe.whitelist()
def status(key):
	frappe.only_for(PAGE_ROLES)
	state = _get(key) or {"status": "expired"}
	if state.get("requested_by") and state["requested_by"] != frappe.session.user:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return {"key": key, **{k: v for k, v in state.items() if k != "requested_by"}}


def run_job(run_id: str, start_date: str, end_date: str, requested_by: str):
	_set(run_id, {"status": "running", "requested_by": requested_by})
	try:
		_set(run_id, {"status": "done", "result": dashboard.compute(start_date, end_date), "requested_by": requested_by})
	except Exception:
		frappe.log_error(title="Sales Dashboard failed")
		_set(run_id, {"status": "failed", "requested_by": requested_by,
			"error": _("The dashboard could not be computed. See the Error Log.")})
	frappe.publish_realtime(EVENT, {"key": run_id}, user=requested_by)
