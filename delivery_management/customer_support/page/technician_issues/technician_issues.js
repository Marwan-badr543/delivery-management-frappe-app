// Technician Issues: see public/js/support_pages.bundle.js (port of the old support pages).
frappe.pages["technician-issues"].on_page_load = function (wrapper) {
	wrapper.dm_page = new window.dm_support.SupportPage(wrapper, "technician");
};

frappe.pages["technician-issues"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.show();
};
