// Sales Commission Payment: get the salesman's unpaid commission (in the background), then submit to pay it.

const SCP_API = "delivery_management.delivery_management.doctype.sales_commission_payment.sales_commission_payment.";

frappe.ui.form.on("Sales Commission Payment", {
	setup(frm) {
		frm.set_query("salesman", () => ({ filters: { enabled: 1, user_type: "System User" } }));
		frm.set_query("paid_from", () => ({
			filters: { company: frm.doc.company, is_group: 0, account_type: ["in", ["Cash", "Bank"]] },
		}));
		frm.set_query("commission_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
		frm.set_query("cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frappe.realtime.on("dm_commission_unpaid", (data) => {
			if (data && data.key === frm.__unpaid_key) frm.events.check_unpaid(frm);
		});
	},

	async company(frm) {
		if (!frm.doc.company || frm.doc.docstatus !== 0) return;
		const { message } = await frappe.call({ method: SCP_API + "default_commission_account", args: { company: frm.doc.company } });
		if (message) frm.set_value("commission_account", message);
	},

	onload(frm) {
		if (frm.is_new() && frm.doc.company && !frm.doc.commission_account) frm.trigger("company");
		if (frm.is_new() && !frm.doc.to_month) {
			// The last month that has ended.
			frm.set_value("to_month", frappe.datetime.add_months(frappe.datetime.month_start(), -1));
		}
	},

	async salesman(frm) {
		if (!frm.doc.salesman || frm.doc.docstatus !== 0) return;
		const { message: month } = await frappe.call({ method: SCP_API + "next_month", args: { salesman: frm.doc.salesman } });
		frm.set_value("from_month", month || null);
		if (month) {
			frappe.show_alert({
				message: __("From Month is set to the month after the last payment ({0}). Unpaid sales of earlier months are still brought, marked Late.",
					[moment(month).format("MMMM YYYY")]),
				indicator: "orange",
			}, 10);
		}
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Get Unpaid Commissions"), () => frm.trigger("get_unpaid"));
		}
	},

	async get_unpaid(frm) {
		if (!frm.doc.salesman || !frm.doc.company || !frm.doc.to_month) {
			frappe.msgprint(__("Choose the Salesman, Company and To Month first."));
			return;
		}
		frm.dashboard.set_headline(__("Calculating the commission in the background..."));
		const { message: state } = await frappe.call({
			method: SCP_API + "start_unpaid",
			args: {
				salesman: frm.doc.salesman, company: frm.doc.company, to_month: frm.doc.to_month,
				from_month: frm.doc.from_month, exclude_services: frm.doc.exclude_services, basis: frm.doc.basis,
			},
		});
		frm.events.handle_unpaid(frm, state);
	},

	async check_unpaid(frm) {
		if (!frm.__unpaid_key) return;
		const { message: state } = await frappe.call({ method: SCP_API + "unpaid_status", args: { key: frm.__unpaid_key } });
		frm.events.handle_unpaid(frm, state);
	},

	handle_unpaid(frm, state) {
		if (!state) return;
		frm.__unpaid_key = state.key;
		if (state.status === "done") {
			clearInterval(frm.__unpaid_poll);
			frm.__unpaid_poll = null;
			frm.dashboard.clear_headline();
			const r = state.result;
			frm.clear_table("months");
			frm.clear_table("invoices");
			r.months.forEach((row) => frm.add_child("months", row));
			r.invoices.forEach((row) => frm.add_child("invoices", row));
			frm.set_value("total_amount", r.total);
			frm.refresh_fields();
			if (!r.invoices.length) frappe.show_alert({ message: __("No unpaid commission for this salesman."), indicator: "orange" });
		} else if (state.status === "failed" || state.status === "expired") {
			clearInterval(frm.__unpaid_poll);
			frm.__unpaid_poll = null;
			frm.dashboard.clear_headline();
			frappe.msgprint(state.error || __("The result expired. Click Get Unpaid Commissions again."));
		} else if (!frm.__unpaid_poll) {
			// Realtime says when it is ready; poll too in case the socket is down.
			frm.__unpaid_poll = setInterval(() => frm.events.check_unpaid(frm), 3000);
		}
	},
});
