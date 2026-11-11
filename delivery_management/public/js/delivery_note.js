// delivery_management: Delivery Schedule section of the Delivery Note.
// The Order Type is set from the items; an order with AC and non-AC items has none
// and must get one before it can be saved again or submitted. A draft note's driver is
// assigned on the Driver Assignment page; a note On the way changes driver here ("Change
// Driver"). Its goods leave the store from the Stock Keeper page; while they are out the
// note is locked (transit/service.py). "Move Delivery Slot" moves the order's booking with
// the Delivery Slots page dialog (public/js/slot_move.bundle.js). "Mark Delivered" lets the
// office deliver (submit) an On the way note without the driver's photo and signature.
// Goods the customer gave back after delivery show above the items. "Split Delivery" moves
// part of the goods of a note still in the store to a new note on another day; a split part
// can "Merge Back" while both are in the store (orders/split.py).

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
		const on_the_way = frm.doc.docstatus < 2 && frm.doc.dm_delivery_status === "On the way";
		const in_store = frm.doc.docstatus === 0 && !frm.doc.dm_delivery_status;
		if (!frm.is_new() && frm.doc.docstatus === 0 && frm.doc.dm_delivery_status) {
			frm.disable_save();
			frm.dashboard.add_comment(frm.doc.dm_delivery_status === "On the way"
				? __("The goods are out with the driver. The driver delivers or returns them from the Delivery Schedule page.")
				: __("The whole order is coming back. The stock keeper receives it on the Stock Keeper page."), "blue", true);
		}
		if (in_store && !frm.is_new() && frappe.user.has_role(["System Manager", "Delivery Manager", "Stock Manager", "Stock User"])) {
			frm.add_custom_button(__("Send Out (Stock Keeper)"), () => frappe.set_route("stock-keeper"));
		}
		if (manager && in_store && frm.doc.dm_delivery_type) {
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
		if (manager && in_store && frm.doc.dm_delivery_type && !frm.is_new()) {
			frm.add_custom_button(__("Split Delivery"), () => dm_split_delivery(frm));
			if (frm.doc.dm_split_from) frm.add_custom_button(__("Merge Back"), () => dm_merge_back(frm));
		}
		if (frm.doc.dm_split_from && frm.doc.docstatus < 2) {
			frm.dashboard.add_comment(__("Part of a split delivery: the rest of the order is on {0}.",
				[`<a href="/desk/delivery-note/${encodeURIComponent(frm.doc.dm_split_from)}">${frappe.utils.escape_html(frm.doc.dm_split_from)}</a>`]),
				"blue", true);
		}
		dm_show_returned(frm);
	},
});

const DM_SPLIT_API = "delivery_management.api.split.";

// Split Delivery: the office moves part of the goods to a new Delivery Note on another day
// (orders/split.py). Each line gets a "to the new delivery" quantity; the day is an AC
// installation day or a delivery slot (in the chosen area), by the note's type. The address
// starts as this note's and may change (the customer wants it elsewhere).
async function dm_split_delivery(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the note first."));
		return;
	}
	const info = await frappe.xcall(DM_SPLIT_API + "get_split", { delivery_note: frm.doc.name });
	const esc = frappe.utils.escape_html;
	const ac = info.order_type === "AC";
	const rows = info.lines.map((l) => `<tr class="${l.can_move ? "" : "text-muted"}">
		<td>${esc(l.item_name || l.item_code)}${l.is_bundle ? ` <span class="indicator-pill gray">${__("Bundle")}</span>` : ""}</td>
		<td class="text-right">${l.qty} ${esc(l.uom || "")}</td>
		<td class="text-right" style="width:140px">${l.can_move
			? `<input type="number" class="form-control input-sm text-right" data-move="${esc(l.dn_detail)}" min="0" max="${l.qty}"
				step="${l.whole ? 1 : "any"}" value="0" aria-label="${__("Move {0}", [esc(l.item_name || l.item_code)])}">`
			: `<small>${__("Stays here")}</small>`}</td></tr>`).join("");
	const d = new frappe.ui.Dialog({
		title: __("Split delivery of {0}", [frm.doc.name]),
		size: "large",
		fields: [
			{ fieldtype: "HTML", fieldname: "lines" },
			{ fieldtype: "Section Break", label: __("Address of the new delivery") },
			{ fieldtype: "Link", fieldname: "dm_delivery_area", label: __("Delivery Area"), options: "Delivery Area",
				reqd: !ac, default: info.address.dm_delivery_area, change: () => loadDay() },
			{ fieldtype: "Column Break" },
			{ fieldtype: "Data", fieldname: "dm_block_no", label: __("Block No"), default: info.address.dm_block_no },
			{ fieldtype: "Column Break" },
			{ fieldtype: "Data", fieldname: "dm_road_no", label: __("Road No"), default: info.address.dm_road_no },
			{ fieldtype: "Column Break" },
			{ fieldtype: "Data", fieldname: "dm_building_no", label: __("Building No"), default: info.address.dm_building_no },
			{ fieldtype: "Section Break", label: ac ? __("Installation day of the new delivery") : __("Day and time of the new delivery") },
			{ fieldtype: "Date", fieldname: "date", label: ac ? __("Installation day") : __("Delivery day"), reqd: 1,
				change: () => loadDay() },
			{ fieldtype: "Column Break" },
			ac ? { fieldtype: "HTML", fieldname: "day_status" }
				: { fieldtype: "Select", fieldname: "time", label: __("Time slot"), reqd: 1, options: [] },
		],
		primary_action_label: __("Split delivery"),
		primary_action: async (values) => {
			const moves = {};
			d.$wrapper.find("[data-move]").each((_i, el) => {
				if (flt(el.value) > 0) moves[el.dataset.move] = flt(el.value);
			});
			if (!Object.keys(moves).length) {
				frappe.msgprint(__("Enter how much of each item goes on the new delivery."));
				return;
			}
			d.get_primary_btn().prop("disabled", true);
			try {
				const r = await frappe.xcall(DM_SPLIT_API + "split_delivery", {
					delivery_note: frm.doc.name, moves,
					address: Object.fromEntries(Object.keys(info.address).map((f) => [f, values[f] || ""])),
					...(ac ? { ac_date: values.date } : { delivery_date: values.date, delivery_time: values.time }),
				});
				d.hide();
				frappe.show_alert({ message: __("New delivery {0} made. Assign its driver on the Driver Assignment page.",
					[`<a href="/desk/delivery-note/${encodeURIComponent(r.delivery_note)}">${esc(r.delivery_note)}</a>`]), indicator: "green" }, 8);
				frm.reload_doc();
			} catch (e) {
				d.get_primary_btn().prop("disabled", false);
			}
		},
	});
	d.fields_dict.lines.$wrapper.html(`
		<p class="text-muted">${__("Enter what the customer wants on another day. The rest stays on this note. Services and the delivery charge stay here.")}</p>
		<table class="table table-sm" style="margin-bottom:6px">
			<thead><tr><th>${__("Item")}</th><th class="text-right">${__("On this note")}</th>
				<th class="text-right">${__("To the new delivery")}</th></tr></thead>
			<tbody>${rows}</tbody></table>
		${info.notes.length > 1 ? `<p class="small text-muted">${__("This order already has {0} deliveries: {1}", [info.notes.length,
			info.notes.map((n) => esc(n.name)).join(", ")])}</p>` : ""}`);

	async function loadDay() {
		const date = d.get_value("date");
		if (!date) return;
		if (ac) {
			const [day] = await frappe.xcall(DM_SPLIT_API + "get_ac_days", { start_date: date });
			d.fields_dict.day_status.$wrapper.html(day ? `<div class="small" style="margin-top:28px">
				<span class="indicator-pill ${day.available ? "green" : "red"}">${day.available
					? __("{0} of {1} units free", [day.remaining, day.capacity]) : __("Closed or full")}</span></div>` : "");
			return;
		}
		const area = d.get_value("dm_delivery_area");
		if (!area) {
			d.set_df_property("time", "options", []);
			d.set_df_property("time", "description", __("Choose the Delivery Area first."));
			return;
		}
		const slots = await frappe.xcall(DM_SPLIT_API + "get_slots", { area, date });
		const open = slots.filter((s) => s.available);
		d.set_df_property("time", "options", open.map((s) => ({ value: s.delivery_time,
			label: __("{0} · {1} of {2} free", [s.time, s.remaining, s.capacity]) })));
		d.set_df_property("time", "description", open.length ? "" : __("No free slot on this day in {0}.", [esc(area)]));
		d.set_value("time", open.length ? open[0].delivery_time : "");
	}
	d.show();
}

function dm_merge_back(frm) {
	frappe.confirm(__("Put the goods of {0} back on {1} and free its day? {0} is deleted.",
		[frm.doc.name, frm.doc.dm_split_from]), async () => {
		const r = await frappe.xcall(DM_SPLIT_API + "merge_back", { delivery_note: frm.doc.name });
		frappe.show_alert({ message: __("Merged back into {0}", [r.delivery_note]), indicator: "green" });
		frappe.set_route("Form", "Delivery Note", r.delivery_note);
	});
}

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
