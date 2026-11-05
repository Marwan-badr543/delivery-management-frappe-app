from frappe.model.document import Document


class OrderLog(Document):
	"""One logistic or money step of an order (``control.log``); never edited."""
