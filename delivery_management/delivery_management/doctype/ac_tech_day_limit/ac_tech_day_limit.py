# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ACTechDayLimit(Document):
	"""A row of Delivery Settings; validated by ``DeliverySettings.validate_day_limits``."""
