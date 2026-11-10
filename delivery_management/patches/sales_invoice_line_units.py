"""Freeze the units of the invoice lines saved before ``stats/units.py`` existed.

One UPDATE per distinct item, with today's items and bundles (a credit note line
gets the same per-unit units as its sale).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from delivery_management.install import get_custom_fields
from delivery_management.stats.units import _per_unit_units


def execute():
	# Patches run before after_migrate: the line fields and Item's Small Item must exist first.
	create_custom_fields(get_custom_fields(), ignore_validate=True, update=True)
	codes = set(frappe.db.sql_list("select distinct item_code from `tabSales Invoice Item` where ifnull(item_code, '') != ''"))
	for code, (ac, non_ac, small) in _per_unit_units(codes).items():
		frappe.db.sql(
			"""update `tabSales Invoice Item` set dm_ac_units = stock_qty * %s, dm_non_ac_units = stock_qty * %s,
				dm_small_units = stock_qty * %s where item_code = %s""",
			(ac, non_ac, small, code),
		)
