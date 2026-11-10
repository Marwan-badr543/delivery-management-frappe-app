// Copyright (c) 2026, Marwan Badr and contributors
// For license information, please see license.txt

// The score is saved by the server; this only shows it live while the manager rates.
const SEV_CRITERIA = ["teamwork", "customer_service", "product_knowledge", "section_tidiness", "work_pressure",
	"response_speed", "skill_development", "customer_follow_up", "supervisor_instructions"];
const SEV_MAX = 10;

frappe.ui.form.on("Sales Evaluation", {
	refresh(frm) {
		SEV_CRITERIA.forEach((f) => frm.fields_dict[f].$input && frm.fields_dict[f].$input.attr({ min: 0, max: SEV_MAX }));
	},
	...Object.fromEntries(SEV_CRITERIA.map((f) => [f, (frm) => {
		const v = cint(frm.doc[f]);
		if (v < 0 || v > SEV_MAX) {
			frappe.show_alert({ message: __("Rate from 0 to {0}.", [SEV_MAX]), indicator: "orange" });
			frm.set_value(f, Math.min(Math.max(v, 0), SEV_MAX));
			return;
		}
		const total = SEV_CRITERIA.reduce((sum, c) => sum + cint(frm.doc[c]), 0);
		frm.set_value("total_points", total);
		frm.set_value("score", flt((total * 100) / (SEV_MAX * SEV_CRITERIA.length), 2));
	}])),
});
