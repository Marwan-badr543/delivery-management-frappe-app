// delivery_management: a submitted Delivery Note still On the way (Delivery Status) shows
// that as its status; every other note keeps ERPNext's own status.

(() => {
	const settings = frappe.listview_settings["Delivery Note"];
	const erpnext_indicator = settings.get_indicator;
	settings.add_fields = [...(settings.add_fields || []), "dm_delivery_status"];
	settings.get_indicator = (doc) =>
		doc.docstatus === 1 && doc.dm_delivery_status === "On the way"
			? [__("On the way"), "orange", "dm_delivery_status,=,On the way"]
			: erpnext_indicator(doc);
})();
