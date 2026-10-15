// Maintenance Issues: see public/js/support_pages.js (port of the old support pages).
frappe.pages["maintenance-issues"].on_page_load = function (wrapper) {
	wrapper.dm_page = new window.dm_support.SupportPage(wrapper, "maintenance");
};

frappe.pages["maintenance-issues"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.show();
};
