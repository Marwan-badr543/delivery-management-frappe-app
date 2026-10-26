"""Delivery Note hooks: schedule copied from the order, Order Type, driver, permissions."""

import frappe
from frappe import _

from delivery_management.drivers import assignment as asg

# Copied from the Sales Order (the order keeps them too; the DN is what drivers work from).
SCHEDULE_FIELDS = (
	"dm_delivery_area",
	"dm_delivery_date",
	"dm_delivery_time",
	"dm_ac_installation_date",
	"dm_ac_units",
	"dm_block_no",
	"dm_road_no",
	"dm_building_no",
)


# Delivery Status (dm_delivery_status): submitting a note sends it On the way, the
# driver confirms it Delivered. Shown as the note's status while On the way
# (public/js/delivery_note_list.js); ERPNext's own statuses otherwise.
ON_THE_WAY = "On the way"
DELIVERED = "Delivered"


# Fields that belong to the other team. A mixed order is split into an AC and a Non AC
# Delivery Note (``orders.delivery_note``); each keeps only its own schedule.
OTHER_TYPE_FIELDS = {
	asg.TYPE_AC: ("dm_delivery_date", "dm_delivery_time"),
	asg.TYPE_NON_AC: ("dm_ac_installation_date", "dm_ac_units"),
}


def schedule_fields_for(order_type: str | None) -> tuple[str, ...]:
	skip = OTHER_TYPE_FIELDS.get(order_type, ())
	return tuple(f for f in SCHEDULE_FIELDS if f not in skip)


def sales_order_of(dn) -> str | None:
	orders = {i.get("against_sales_order") for i in dn.items if i.get("against_sales_order")}
	return orders.pop() if len(orders) == 1 else None


def copy_schedule_from_sales_order(dn) -> None:
	so = sales_order_of(dn)
	if not so:
		return
	fields = schedule_fields_for(dn.get("dm_delivery_type"))
	values = frappe.db.get_value("Sales Order", so, list(fields), as_dict=True) or {}
	for field in fields:
		if not dn.get(field) and values.get(field):
			dn.set(field, values[field])


def before_insert(doc, method=None):
	copy_schedule_from_sales_order(doc)
	if not doc.get("dm_delivery_type"):
		# None for an order mixing AC and other goods (a hand-made Delivery Note, or
		# automatic notes switched off): someone has to choose.
		doc.dm_delivery_type = asg.order_type_of(doc.items)


def validate(doc, method=None):
	if doc.is_new():
		return
	if not doc.get("dm_delivery_type"):
		doc.dm_delivery_type = asg.order_type_of(doc.items)
	if not doc.get("dm_delivery_type"):
		frappe.throw(
			_("This order has AC and non-AC items. Choose its <b>Order Type</b> (AC or Non AC) in the Delivery Schedule section."),
			title=_("Order Type required"),
		)
	if doc.get("dm_driver") and doc.has_value_changed("dm_delivery_type"):
		if asg.ROLE_FOR_TYPE[doc.dm_delivery_type] not in frappe.get_roles(doc.dm_driver):
			frappe.msgprint(
				_("Driver {0} was removed: he does not serve {1} orders. Assign a new one on the Driver Assignment page.").format(
					doc.dm_driver, doc.dm_delivery_type),
				indicator="orange", alert=True,
			)
			doc.dm_driver = None
	if doc.get("dm_driver") and doc.has_value_changed("dm_driver"):
		asg.check_assignable(doc, doc.dm_driver)


def before_submit(doc, method=None):
	if not doc.get("dm_delivery_type"):
		doc.dm_delivery_type = asg.order_type_of(doc.items)
	if not doc.get("dm_delivery_type"):
		frappe.throw(_("Choose the Order Type (AC or Non AC) before submitting."), title=_("Order Type required"))
	doc.dm_delivery_status = None if doc.get("is_return") else ON_THE_WAY


def on_update(doc, method=None):
	asg.sync_assignment(doc)


def on_cancel(doc, method=None):
	asg.remove_assignment(doc.name)


def on_trash(doc, method=None):
	asg.remove_assignment(doc.name)


# ---------------------------------------------------------------------------
# Permissions: a driver sees only his own Delivery Notes.
# ---------------------------------------------------------------------------

# Roles that already see Delivery Notes for their normal work; they are never limited.
_FULL_ACCESS_ROLES = {"System Manager", "Delivery Manager", "Stock User", "Stock Manager", "Sales User",
	"Sales Manager", "Accounts User", "Accounts Manager"}


def only_own_orders(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return False
	roles = set(frappe.get_roles(user))
	return bool(roles & set(asg.DRIVER_ROLES)) and not roles & _FULL_ACCESS_ROLES


def get_permission_query_conditions(user=None):
	user = user or frappe.session.user
	if only_own_orders(user):
		return f"`tabDelivery Note`.`dm_driver` = {frappe.db.escape(user)}"
	return ""


def has_permission(doc, ptype=None, user=None):
	user = user or frappe.session.user
	if only_own_orders(user):
		return doc.get("dm_driver") == user
	# Frappe treats any falsy answer as "denied", so say yes and let the DocPerms decide.
	return True
