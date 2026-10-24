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
	ac_tech_max_orders_per_day: int = 0
	delivery_charge_item: str | None = None
	tv_driver_gap_hours: float = 1.0
	tv_driver_gap_hours_manager: float = 0.5
	auto_create_delivery_note: bool = True
	auto_delivery_note_company: str | None = None
	support_visit_gap_minutes: int = 60
	support_maint_service_person: str | None = None
	tv_items: tuple[str, ...] = ()
	tv_item_groups: tuple[str, ...] = ()
	tv_excluded_items: tuple[str, ...] = ()


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
		ac_tech_max_orders_per_day=max(cint(doc.get("ac_tech_max_orders_per_day")), 0),
		delivery_charge_item=doc.get("delivery_charge_item") or None,
		tv_driver_gap_hours=_non_negative(doc.get("tv_driver_gap_hours"), 1.0),
		tv_driver_gap_hours_manager=_non_negative(doc.get("tv_driver_gap_hours_manager"), 0.5),
		auto_create_delivery_note=bool(cint(doc.get("auto_create_delivery_note"))),
		auto_delivery_note_company=doc.get("auto_delivery_note_company") or None,
		support_visit_gap_minutes=cint(_non_negative(doc.get("support_visit_gap_minutes"), 60)),
		support_maint_service_person=doc.get("support_maint_service_person") or None,
		tv_items=tuple(row.item_code for row in (doc.get("tv_items") or [])),
		tv_item_groups=tuple(row.item_group for row in (doc.get("tv_item_groups") or [])),
		tv_excluded_items=tuple(row.item_code for row in (doc.get("tv_excluded_items") or [])),
	)


def _non_negative(value, default: float) -> float:
	"""A Float setting; empty (never saved since the field was added) means the default."""
	return default if value is None or value == "" else max(flt(value), 0.0)
