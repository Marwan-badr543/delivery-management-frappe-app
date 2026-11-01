// delivery_management: Delivery Schedule section of the Delivery Note.
// The Order Type is set from the items; an order with AC and non-AC items has none
// and must get one before it can be saved again or submitted. A draft note's driver is
// assigned on the Driver Assignment page; a note On the way changes driver here ("Change
// Driver"). "Move Delivery Slot" moves the order's booking with the Delivery Slots page
// dialog (public/js/slot_move.bundle.js). "Mark Delivered" lets the office confirm an On the
// way note without the driver's photo and signature.
// Goods that came back show above the items. When some came back without a Customer Return,
// "Send Again" (also the menu's Duplicate) makes a draft note with them to scan and send again.

const DM_MANAGERS = ["System Manager", "Delivery Manager"];

frappe.ui.form.on("Delivery Note", {
	refresh(frm) {
		const untyped = !frm.is_new() && !frm.doc.dm_delivery_type && frm.doc.docstatus === 0;
		frm.toggle_reqd("dm_delivery_type", untyped);
		if (untyped) {
			frm.dashboard.add_comment(
				__("This order has AC and non-AC items. Choose its <b>Order Type</b> (AC or Non AC) in the Delivery Schedule section, then save."),
				"orange",
				true
			);
		}
		const manager = frappe.user.has_role(DM_MANAGERS);
		const on_the_way = frm.doc.docstatus === 1 && frm.doc.dm_delivery_status === "On the way";
		if (manager && frm.doc.docstatus === 0 && frm.doc.dm_delivery_type) {
			frm.add_custom_button(__("Assign Driver"), () => {
				frappe.route_options = { tab: frm.doc.dm_delivery_type === "AC" ? "ac" : "tv", search: frm.doc.name };
				frappe.set_route("driver-assignment");
			});
		}
		if (manager && on_the_way) {
			frm.add_custom_button(frm.doc.dm_driver ? __("Change Driver") : __("Assign Driver"), () => dm_change_driver(frm));
		}
		if (!frm.is_new() && frm.doc.docstatus < 2 && !["Delivered", "Returned"].includes(frm.doc.dm_delivery_status)
				&& frm.doc.dm_delivery_type && manager) {
			const label = frm.doc.dm_delivery_type === "AC" ? __("Move Installation Day") : __("Move Delivery Slot");
			frm.add_custom_button(label, async () => {
				const booking = await frappe.xcall("delivery_management.api.admin.get_delivery_note_booking",
					{ delivery_note: frm.doc.name });
				window.dm_slots.moveBooking(booking, () => frm.reload_doc());
			});
		}
		if (manager && on_the_way) {
			frm.add_custom_button(__("Mark Delivered"), async () => {
				await frappe.xcall("delivery_management.api.drivers.mark_delivered_by_office",
					{ delivery_note: frm.doc.name });
				frm.reload_doc();
			});
		}
		dm_show_returned(frm);
		if (manager && frm.doc.__onload && frm.doc.__onload.dm_resend) {
			const resend = () => dm_resend(frm);
			frm.add_custom_button(__("Send Again"), resend);
			frm.copy_doc = resend; // the menu's Duplicate
		} else {
			delete frm.copy_doc; // back to Frappe's own Duplicate
		}
	},
});

function dm_show_returned(frm) {
	if (frm.doc.docstatus !== 1 || frm.doc.is_return || !frm.doc.per_returned) return;
	const rows = frm.doc.items.filter((r) => r.returned_qty > 0).map((r) => {
		const back = r.returned_qty / (r.conversion_factor || 1);
		return `<li>${frappe.utils.escape_html(r.item_name || r.item_code)}: <b>${__("returned")} ${back}</b>
			${__("of")} ${r.qty} · ${__("received")} ${r.qty - back}</li>`;
	});
	frm.dashboard.add_comment(`<b>${__("Returned items")}</b><ul style="margin:4px 0 0">${rows.join("")}</ul>`,
		"red", true);
}

async function dm_resend(frm) {
	const name = await frappe.xcall("delivery_management.api.drivers.make_resend", { delivery_note: frm.doc.name });
	frappe.show_alert({ message: __("{0} is ready: scan the serials and submit to send again.", [name]), indicator: "green" });
	frappe.set_route("Form", "Delivery Note", name);
}

async function dm_change_driver(frm) {
	const options = await frappe.xcall("delivery_management.api.drivers.get_driver_options",
		{ delivery_note: frm.doc.name });
	if (options.message) {
		frappe.msgprint(options.message);
		return;
	}
	const choices = options.drivers
		.filter((d) => d.available || d.user === options.current_driver)
		.map((d) => ({ value: d.user, label: `${d.full_name}${d.label ? ` (${d.label})` : ""}` }));
	const d = new frappe.ui.Dialog({
		title: __("{0} of {1}", [options.order_type === "AC" ? __("AC Tech") : __("Driver"), frm.doc.name]),
		fields: [{ fieldname: "driver", fieldtype: "Select", label: options.order_type === "AC" ? __("AC Tech") : __("Driver"),
			options: [{ value: "", label: "" }, ...choices], default: options.current_driver || "",
			description: __("Busy drivers are not listed. Leave empty to remove the driver.") }],
		primary_action_label: __("Save"),
		primary_action: async ({ driver }) => {
			await frappe.xcall("delivery_management.api.drivers.assign_driver",
				{ delivery_note: frm.doc.name, driver: driver || null });
			d.hide();
			frm.reload_doc();
		},
	});
	d.show();
}
