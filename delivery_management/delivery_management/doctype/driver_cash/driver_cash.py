# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class DriverCash(Document):
	"""Cash a driver / AC Tech took from a customer, until the office receives it
	(``transit.cash``). Made and removed by the app only."""
