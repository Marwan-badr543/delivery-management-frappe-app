// delivery_management: Delivery Schedule section of the Delivery Note.
// The Order Type is set from the items; an order with AC and non-AC items has none
// and must get one before it can be saved again or submitted. The driver is assigned
// on the Driver Assignment page, not here. "Move Delivery Slot" moves the order's booking
// with the Delivery Slots page dialog (public/js/slot_move.bundle.js). "Mark Delivered" lets
// the office confirm an On the way note without the driver's photo and signature.

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
		if (frm.doc.docstatus < 2 && frm.doc.dm_delivery_type && frappe.user.has_role(["System Manager", "Delivery Manager"])) {
			frm.add_custom_button(__("Assign Driver"), () => {
				frappe.route_options = { tab: frm.doc.dm_delivery_type === "AC" ? "ac" : "tv", search: frm.doc.name };
				frappe.set_route("driver-assignment");
			});
		}
		if (!frm.is_new() && frm.doc.docstatus < 2 && frm.doc.dm_delivery_status !== "Delivered" && frm.doc.dm_delivery_type
				&& frappe.user.has_role(["System Manager", "Delivery Manager"])) {
			const label = frm.doc.dm_delivery_type === "AC" ? __("Move Installation Day") : __("Move Delivery Slot");
			frm.add_custom_button(label, async () => {
				const booking = await frappe.xcall("delivery_management.api.admin.get_delivery_note_booking",
					{ delivery_note: frm.doc.name });
				window.dm_slots.moveBooking(booking, () => frm.reload_doc());
			});
		}
		if (frm.doc.docstatus === 1 && frm.doc.dm_delivery_status === "On the way"
				&& frappe.user.has_role(["System Manager", "Delivery Manager"])) {
			frm.add_custom_button(__("Mark Delivered"), async () => {
				await frappe.xcall("delivery_management.api.drivers.mark_delivered_by_office",
					{ delivery_note: frm.doc.name });
				frm.reload_doc();
			});
		}
	},
});
