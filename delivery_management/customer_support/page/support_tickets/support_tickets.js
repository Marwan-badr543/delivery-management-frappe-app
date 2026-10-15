// Support Tickets: see public/js/support_pages.js (port of the old support pages).
frappe.pages["support-tickets"].on_page_load = function (wrapper) {
	wrapper.dm_page = new window.dm_support.SupportPage(wrapper, "cs");
};

frappe.pages["support-tickets"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.show();
};
