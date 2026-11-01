// delivery_management: a submitted Delivery Note still On the way (Delivery Status), or whose
// goods all came back while On the way (Returned), shows that as its status; every other
// note keeps ERPNext's own status.

(() => {
	const settings = frappe.listview_settings["Delivery Note"];
	const erpnext_indicator = settings.get_indicator;
	const ours = { "On the way": "orange", Returned: "red" };
	settings.add_fields = [...(settings.add_fields || []), "dm_delivery_status"];
	settings.get_indicator = (doc) =>
		doc.docstatus === 1 && ours[doc.dm_delivery_status]
			? [__(doc.dm_delivery_status), ours[doc.dm_delivery_status], `dm_delivery_status,=,${doc.dm_delivery_status}`]
			: erpnext_indicator(doc);
})();
