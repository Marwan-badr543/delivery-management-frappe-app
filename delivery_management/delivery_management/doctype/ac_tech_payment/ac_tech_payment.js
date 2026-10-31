// AC Tech Payment: get the AC Tech's unpaid installations, then submit to pay them.

frappe.ui.form.on("AC Tech Payment", {
	setup(frm) {
		frm.set_query("technician", () => ({
			query: "delivery_management.delivery_management.doctype.ac_tech_payment.ac_tech_payment.ac_tech_query",
		}));
		frm.set_query("paid_from", () => ({
			filters: { company: frm.doc.company, is_group: 0, account_type: ["in", ["Cash", "Bank"]] },
		}));
		frm.set_query("expense_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
		frm.set_query("cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Get Unpaid Installations"), () => frm.trigger("get_unpaid"));
		}
	},

	async get_unpaid(frm) {
		if (!frm.doc.technician) {
			frappe.msgprint(__("Choose the AC Tech first."));
			return;
		}
		const { message: rows } = await frappe.call({
			method: "delivery_management.delivery_management.doctype.ac_tech_payment.ac_tech_payment.get_unpaid_installations",
			args: { technician: frm.doc.technician, up_to_date: frm.doc.up_to_date },
			freeze: true,
		});
		frm.clear_table("items");
		rows.forEach((row) => frm.add_child("items", row));
		frm.set_value("total_units", rows.reduce((s, r) => s + r.ac_units, 0));
		frm.set_value("total_amount", rows.reduce((s, r) => s + r.amount, 0));
		frm.refresh_field("items");
		if (!rows.length) frappe.show_alert({ message: __("Nothing unpaid for this AC Tech."), indicator: "orange" });
	},
});
