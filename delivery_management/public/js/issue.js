// Support ticket (Issue with a Support Stage): the stage is moved on the Support pages.
const DM_SUPPORT_PAGES = {
	"Customer Service": "support-tickets",
	"Technician": "technician-issues",
	"Maintenance Technician": "maintenance-issues",
};

frappe.ui.form.on("Issue", {
	refresh(frm) {
		const page = DM_SUPPORT_PAGES[frm.doc.sm_stage];
		if (!page || frm.is_new()) return;
		frm.add_custom_button(__("Open in Support page"), () => {
			frappe.route_options = { ticket: frm.doc.name };
			frappe.set_route(page);
		});
		frm.set_intro(__("Support ticket in stage <b>{0}</b>. Reports, technicians and stages are changed on the Support pages.",
			[__(frm.doc.sm_stage)]), "blue");
	},
});
