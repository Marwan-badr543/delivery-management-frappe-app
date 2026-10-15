"""AC Tech Day Limit became a child table of Delivery Settings: attach the rows made
when it was a standalone doctype."""

import frappe


def execute():
	# Frappe creates parent / parenttype / parentfield only with a new child table; this
	# table already existed as a standalone doctype.
	columns = set(frappe.db.get_table_columns("AC Tech Day Limit"))
	for column in ("parent", "parentfield", "parenttype"):
		if column not in columns:
			frappe.db.sql_ddl(f"alter table `tabAC Tech Day Limit` add column `{column}` varchar(140)")
	if "parent" not in columns:
		frappe.db.sql_ddl("alter table `tabAC Tech Day Limit` add index parent(parent)")
	rows = frappe.get_all("AC Tech Day Limit", filters={"parent": ["in", ["", None]]}, pluck="name",
		order_by="work_date asc, driver asc")
	for idx, name in enumerate(rows, start=1 + (frappe.db.sql(
			"select ifnull(max(idx), 0) from `tabAC Tech Day Limit` where parent = 'Delivery Settings'")[0][0])):
		frappe.db.set_value("AC Tech Day Limit", name, {"parent": "Delivery Settings", "parenttype": "Delivery Settings",
			"parentfield": "ac_tech_day_limits", "idx": idx}, update_modified=False)
