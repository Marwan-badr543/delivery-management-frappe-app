// delivery_management: pick the delivery slot / AC installation day on a Sales Order.
// The time and AC day fields are read-only and are set from a picker that lists only
// free slots, so a taken slot cannot be typed in. The server re-checks on save and
// books the capacity on submit.

const DM_METHOD = "delivery_management.orders.sales_order.get_schedule_options";

frappe.ui.form.on("Sales Order", {
	setup(frm) {
		frm.set_query("dm_delivery_area", () => ({ filters: { enabled: 1 } }));
	},
	refresh(frm) {
		frm.set_df_property("dm_delivery_time", "read_only", 1);
		frm.set_df_property("dm_ac_installation_date", "read_only", 1);
		frm.set_df_property("dm_ac_units", "read_only", 1);
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Choose Delivery Slot"), () => dm_open_slot_picker(frm)).addClass("btn-primary");
		}
	},
	dm_delivery_area(frm) {
		frm.set_value("dm_delivery_time", null);
	},
	dm_delivery_date(frm) {
		frm.set_value("dm_delivery_time", null);
	},
});

function dm_items(frm) {
	return (frm.doc.items || [])
		.filter((i) => i.item_code)
		.map((i) => ({ item_code: i.item_code, qty: i.qty, conversion_factor: i.conversion_factor || 1 }));
}

function dm_time_label(time) {
	return moment(time, "HH:mm:ss").format("h:mm A");
}

function dm_inject_styles() {
	if (document.getElementById("dm-slot-picker-css")) return;
	$(`<style id="dm-slot-picker-css">
		.dmp-days { display: flex; gap: 6px; overflow-x: auto; padding: 2px 2px 8px; }
		.dmp-day, .dmp-slot, .dmp-ac { border: 1px solid var(--border-color); background: var(--card-bg, var(--fg-color));
			color: var(--text-color); border-radius: var(--border-radius-md, 8px); cursor: pointer; transition: border-color .15s, box-shadow .15s; }
		.dmp-day:hover, .dmp-slot:hover, .dmp-ac:hover { border-color: var(--primary); }
		.dmp-day { flex: 0 0 auto; min-width: 64px; padding: 6px 8px; display: flex; flex-direction: column; align-items: center; line-height: 1.2; }
		.dmp-day small { font-size: 11px; color: var(--text-muted); text-transform: uppercase; }
		.dmp-day b { font-size: 14px; }
		.dmp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 8px; }
		.dmp-slot { padding: 10px 12px; text-align: left; display: flex; flex-direction: column; gap: 2px; }
		.dmp-slot b { font-size: 15px; }
		.dmp-slot small, .dmp-ac small { color: var(--green-600, var(--green)); font-size: 12px; }
		.dmp-slot.low small { color: var(--orange-600, var(--orange)); font-weight: 600; }
		.dmp-ac { flex: 0 0 auto; min-width: 84px; padding: 8px 10px; display: flex; flex-direction: column; align-items: center; line-height: 1.25; }
		.dmp-ac span { font-size: 11px; color: var(--text-muted); text-transform: uppercase; }
		.dmp-selected { background: var(--primary) !important; border-color: var(--primary) !important; color: #fff !important; }
		.dmp-selected small, .dmp-selected span { color: rgba(255,255,255,.9) !important; }
		.dmp-note { display: flex; gap: 8px; align-items: center; padding: 10px 12px; border-radius: var(--border-radius-md, 8px);
			background: var(--subtle-fg, var(--control-bg)); color: var(--text-muted); font-size: var(--text-sm, 13px); }
		.dmp-note.warn { color: var(--red-600, var(--red)); }
		.dmp-summary { display: flex; flex-wrap: wrap; gap: 8px 18px; padding: 10px 12px; border-radius: var(--border-radius-md, 8px);
			border: 1px dashed var(--border-color); font-size: var(--text-sm, 13px); }
		.dmp-summary b { color: var(--text-color); }
		.dmp-summary span { color: var(--text-muted); }
	</style>`).appendTo(document.head);
}

function dm_open_slot_picker(frm) {
	dm_inject_styles();
	// The choice lives in the dialog until Apply.
	const pick = {
		area: frm.doc.dm_delivery_area || null,
		date: frm.doc.dm_delivery_date || frappe.datetime.get_today(),
		time: frm.doc.dm_delivery_time || null,
		ac_date: frm.doc.dm_ac_installation_date || null,
	};
	let data = { slots: [], ac_days: [], ac_units: 0 };

	const d = new frappe.ui.Dialog({
		title: __("Choose Delivery Slot"),
		size: "large",
		fields: [
			{ fieldname: "summary_html", fieldtype: "HTML" },
			{
				fieldname: "area",
				fieldtype: "Link",
				options: "Delivery Area",
				label: __("Delivery Area"),
				default: pick.area,
				get_query: () => ({ filters: { enabled: 1 } }),
				onchange: () => {
					const area = d.get_value("area");
					if (area !== pick.area) {
						pick.area = area;
						pick.time = null;
						load();
					}
				},
			},
			{ fieldtype: "Section Break", label: __("Delivery day and time") },
			{ fieldname: "days_html", fieldtype: "HTML" },
			{ fieldname: "slots_html", fieldtype: "HTML" },
			{ fieldtype: "Section Break", label: __("AC installation day"), fieldname: "ac_section" },
			{ fieldname: "ac_html", fieldtype: "HTML" },
		],
		primary_action_label: __("Apply"),
		primary_action() {
			const values = { dm_delivery_area: pick.area || null };
			values.dm_delivery_date = pick.time ? pick.date : frm.doc.dm_delivery_date;
			frm.set_value(values).then(() => {
				// The area/date handlers clear the time, so set it last.
				const after = { dm_delivery_time: pick.time || null };
				if (data.ac_units) after.dm_ac_installation_date = pick.ac_date || null;
				return frm.set_value(after);
			});
			d.hide();
		},
		secondary_action_label: __("Clear schedule"),
		secondary_action() {
			frappe.confirm(__("Remove the delivery slot and AC day from this order?"), () => {
				frm.set_value({ dm_delivery_time: null, dm_delivery_date: null, dm_ac_installation_date: null });
				d.hide();
			});
		},
	});

	function render_summary() {
		const parts = [
			[__("Area"), pick.area || "—"],
			[__("Delivery"), pick.time ? `${frappe.datetime.str_to_user(pick.date)} · ${dm_time_label(pick.time)}` : __("not chosen")],
		];
		if (data.ac_units) parts.push([__("AC installation"), pick.ac_date ? frappe.datetime.str_to_user(pick.ac_date) : __("not chosen")]);
		d.fields_dict.summary_html.$wrapper.html(`<div class="dmp-summary">${parts
			.map(([k, v]) => `<span>${k}: <b>${frappe.utils.escape_html(v)}</b></span>`).join("")}</div>`);
	}

	function render_days() {
		const today = frappe.datetime.get_today();
		const $w = d.fields_dict.days_html.$wrapper.empty();
		const $days = $(`<div class="dmp-days"></div>`).appendTo($w);
		for (let i = 0; i < 21; i++) {
			const date = frappe.datetime.add_days(today, i);
			const label = i === 0 ? __("Today") : i === 1 ? __("Tomorrow") : moment(date).format("ddd");
			$(`<button type="button" class="dmp-day ${date === pick.date ? "dmp-selected" : ""}">
				<small>${label}</small><b>${moment(date).format("D MMM")}</b></button>`)
				.appendTo($days)
				.on("click", () => {
					if (pick.date !== date) {
						pick.date = date;
						pick.time = null;
						load();
					}
				});
		}
	}

	function render() {
		render_summary();
		render_days();
		const $slots = d.fields_dict.slots_html.$wrapper.empty();
		if (!pick.area) {
			$slots.html(`<div class="dmp-note">${__("Choose the delivery area to see the free times.")}</div>`);
		} else if (!data.slots.length) {
			$slots.html(`<div class="dmp-note warn">${__("No free delivery time in {0} on this day. Pick another day.", [frappe.utils.escape_html(pick.area)])}</div>`);
		} else {
			const $grid = $(`<div class="dmp-grid"></div>`).appendTo($slots);
			data.slots.forEach((slot) => {
				const selected = pick.time && pick.time.startsWith(slot.time);
				const left = slot.remaining === 1 ? __("Last car") : __("{0} cars free", [slot.remaining]);
				$(`<button type="button" class="dmp-slot ${selected ? "dmp-selected" : ""} ${slot.remaining === 1 ? "low" : ""}">
					<b>${dm_time_label(slot.delivery_time)}</b><small>${left}</small></button>`)
					.appendTo($grid)
					.on("click", () => {
						pick.time = slot.delivery_time;
						render();
					});
			});
		}

		const $ac = d.fields_dict.ac_html.$wrapper.empty();
		d.set_df_property("ac_section", "hidden", !data.ac_units);
		if (!data.ac_units) return;
		$ac.append(`<div class="dmp-note">${__("This order has {0} AC unit(s). Only days with enough installation capacity are shown.", [data.ac_units])}</div>`);
		if (!data.ac_days.length) {
			$ac.append(`<div class="dmp-note warn">${__("No day has enough AC capacity.")}</div>`);
			return;
		}
		const $days = $(`<div class="dmp-days" style="margin-top:8px"></div>`).appendTo($ac);
		data.ac_days.forEach((day) => {
			$(`<button type="button" class="dmp-ac ${pick.ac_date === day.date ? "dmp-selected" : ""}">
				<span>${moment(day.date).format("ddd")}</span><b>${moment(day.date).format("D MMM")}</b>
				<small>${__("{0} free", [day.remaining])}</small></button>`)
				.appendTo($days)
				.on("click", () => {
					pick.ac_date = day.date;
					render();
				});
		});
	}

	function load() {
		frappe
			.call({ method: DM_METHOD, args: { area: pick.area, date: pick.date, items: dm_items(frm) } })
			.then((r) => {
				data = Object.assign({ slots: [], ac_days: [], ac_units: 0 }, r.message || {});
				if (pick.time && !data.slots.some((s) => pick.time.startsWith(s.time))) {
					// Keep the order's own booked time visible even if it no longer shows as free.
					if (!(frm.doc.dm_delivery_area === pick.area && frm.doc.dm_delivery_date === pick.date)) pick.time = null;
				}
				render();
			});
	}

	d.show();
	render();
	load();
}
