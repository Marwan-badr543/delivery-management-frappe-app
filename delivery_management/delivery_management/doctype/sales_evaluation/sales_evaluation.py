# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate

# Each point is rated 0 to 10; the score is their sum out of the maximum.
CRITERIA = (
	"teamwork",
	"customer_service",
	"product_knowledge",
	"section_tidiness",
	"work_pressure",
	"response_speed",
	"skill_development",
	"customer_follow_up",
	"supervisor_instructions",
)
MAX_POINTS = 10


class SalesEvaluation(Document):
	def validate(self):
		if getdate(self.from_date) > getdate(self.to_date):
			frappe.throw(_("From Date must be on or before To Date."))
		for field in CRITERIA:
			if not 0 <= cint(self.get(field)) <= MAX_POINTS:
				frappe.throw(_("{0} must be from 0 to {1}.").format(_(self.meta.get_label(field)), MAX_POINTS))
		self.total_points = sum(cint(self.get(field)) for field in CRITERIA)
		self.score = round(self.total_points * 100 / (MAX_POINTS * len(CRITERIA)), 2)
