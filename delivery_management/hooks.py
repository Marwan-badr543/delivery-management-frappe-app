app_name = "delivery_management"
app_title = "Delivery Management"
app_publisher = "Marwan Badr"
app_description = "Delivery slots engine, AC capacity and POS order fulfilment"
app_email = "marwanbadr514@gmail.com"
app_license = "mit"

add_to_apps_screen = [
	{
		"name": "delivery_management",
		"logo": "/assets/delivery_management/images/delivery-logo.svg",
		"title": "Delivery Management",
		"route": "/desk/delivery-management",
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# Bundles get a content hash in their URL, so browsers load each new version at once.
app_include_css = [
	"header_search.bundle.css",
	"drivers_pages.bundle.css",
]
app_include_js = [
	"header_search.bundle.js",
	"drivers_common.bundle.js",
	"support_pages.bundle.js",
	"pos_reload_qty.bundle.js",
	"slot_move.bundle.js",
]

# include js, css files in header of web template
# web_include_css = "/assets/delivery_management/css/delivery_management.css"
# web_include_js = "/assets/delivery_management/js/delivery_management.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "delivery_management/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "delivery_management.utils.jinja_methods",
# 	"filters": "delivery_management.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "delivery_management.install.before_install"
# after_install = "delivery_management.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "delivery_management.uninstall.before_uninstall"
# after_uninstall = "delivery_management.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "delivery_management.utils.before_app_install"
# after_app_install = "delivery_management.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "delivery_management.utils.before_app_uninstall"
# after_app_uninstall = "delivery_management.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "delivery_management.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Sales Order": {
		"validate": "delivery_management.orders.sales_order.validate",
		"before_submit": "delivery_management.orders.sales_order.before_submit",
		# Replaces the server script "Delivery Note From Sales Order".
		"on_submit": [
			"delivery_management.orders.reservation.reserve",
			"delivery_management.orders.delivery_note.create_from_sales_order",
		],
		"on_cancel": "delivery_management.orders.events.on_sales_order_cancel",
		"on_trash": "delivery_management.orders.events.on_sales_order_trash",
	},
	"Delivery Note": {
		"before_validate": "delivery_management.orders.serial_choice.keep_scanned",
		"before_insert": [
			"delivery_management.orders.events.inherit_header_from_sales_order",
			"delivery_management.drivers.delivery_note.before_insert",
			"delivery_management.orders.serial_choice.clear_order_serials",
		],
		"validate": [
			"delivery_management.drivers.delivery_note.validate",
			"delivery_management.orders.serial_choice.restore_scanned",
		],
		"before_submit": [
			"delivery_management.drivers.delivery_note.before_submit",
			"delivery_management.orders.reservation.deliver",
		],
		"before_cancel": "delivery_management.orders.reservation.undeliver",
		"on_update": "delivery_management.drivers.delivery_note.on_update",
		"on_cancel": "delivery_management.drivers.delivery_note.on_cancel",
		"on_trash": "delivery_management.drivers.delivery_note.on_trash",
	},
	"Sales Invoice": {
		"validate": "delivery_management.orders.serial_choice.apply_to_packed_items",
	},
	"Issue": {
		# A pickup slot booked for a support ticket is freed with it.
		"on_trash": "delivery_management.customer_support.permissions.on_issue_trash",
	},
}

required_apps = ["erpnext"]

after_install = "delivery_management.install.after_install"
after_migrate = "delivery_management.install.after_migrate"

doctype_js = {
	"Sales Order": "public/js/sales_order.js",
	"Delivery Note": "public/js/delivery_note.js",
	"Issue": "public/js/issue.js",
}

fixtures = [
	{"dt": "Role", "filters": [["role_name", "in", ["AC Tech", "TV Driver", "Technician", "Maintenance Technician"]]]},
	# Delivery Note workflow: Draft -> On the way (stock keeper submits) -> Delivered (driver);
	# a return goes Draft -> Return.
	{"dt": "Workflow State", "filters": [["name", "in", ["Draft", "On the way", "Delivered", "Return", "Cancelled"]]]},
	{"dt": "Workflow Action Master", "filters": [["name", "in", ["On the way", "Deliver", "Retrun", "Cancel"]]]},
	{"dt": "Workflow", "filters": [["name", "=", "delivery note on the way"]]},
]

permission_query_conditions = {
	"Delivery Note": "delivery_management.drivers.delivery_note.get_permission_query_conditions",
	"Issue": "delivery_management.customer_support.permissions.get_permission_query_conditions",
	"AC Site Check": "delivery_management.delivery_management.doctype.ac_site_check.ac_site_check.get_permission_query_conditions",
}
has_permission = {
	"Delivery Note": "delivery_management.drivers.delivery_note.has_permission",
	"Issue": "delivery_management.customer_support.permissions.has_permission",
	"AC Site Check": "delivery_management.delivery_management.doctype.ac_site_check.ac_site_check.has_permission",
}

extend_bootinfo = "delivery_management.api.pos.extend_bootinfo"


