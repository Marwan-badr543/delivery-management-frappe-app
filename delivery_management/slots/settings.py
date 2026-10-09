"""Typed access to the ``Delivery Settings`` single doctype."""

from dataclasses import dataclass

import frappe
from frappe.utils import cint, flt


@dataclass(frozen=True)
class SlotSettings:
	global_ac_daily_capacity: int
	slot_lead_time_hours: float
	booking_window_days: int
	ac_item_groups: tuple[str, ...]
	ac_excluded_item_groups: tuple[str, ...]
	ac_excluded_items: tuple[str, ...]
	main_warehouse: str | None


def get_settings() -> SlotSettings:
	doc = frappe.get_cached_doc("Delivery Settings")
	return SlotSettings(
		global_ac_daily_capacity=max(cint(doc.global_ac_daily_capacity), 0),
		slot_lead_time_hours=max(flt(doc.slot_lead_time_hours), 0.0),
		booking_window_days=cint(doc.booking_window_days) or 30,
		ac_item_groups=tuple(row.item_group for row in (doc.ac_item_groups or []) if row.item_group),
		ac_excluded_item_groups=tuple(row.item_group for row in (doc.ac_excluded_item_groups or [])),
		ac_excluded_items=tuple(row.item_code for row in (doc.ac_excluded_items or [])),
		main_warehouse=doc.main_warehouse or None,
	)
