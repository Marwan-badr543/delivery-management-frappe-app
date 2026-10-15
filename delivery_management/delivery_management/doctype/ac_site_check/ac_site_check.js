frappe.ui.form.on("AC Site Check", {
	setup(frm) {
		frm.set_query("ac_tech", () => ({
			query: "delivery_management.delivery_management.doctype.ac_site_check.ac_site_check.ac_tech_query",
		}));
	},
});
