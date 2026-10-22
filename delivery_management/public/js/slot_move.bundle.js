// delivery_management: the "Move booking" dialog, shared by the Delivery Slots page and
// the Delivery Note form. The area is a dropdown of every area, so a filled area does
// not hide the others.

(function () {
	const API = "delivery_management.api.admin.";
	const REASON_LABELS = {
		area_disabled: __("Area turned off"),
		not_a_working_hour: __("Not a working hour"),
		too_soon: __("Too soon / passed"),
		past_date: __("Past date"),
		manually_disabled: __("Closed by admin"),
		full: __("Full"),
		not_enough_capacity: __("Not enough capacity"),
	};

	const call = (method, args) => frappe.call({ method: API + method, args, freeze: true }).then((r) => r.message);
	const timeLabel = (time) => moment(time, "HH:mm").format("h:mm A");

	/** booking: {reservation, slot_type ("Delivery" | "AC"), area, date, time}; onMoved runs after the move. */
	async function moveBooking(booking, onMoved) {
		const done = async (args) => {
			await call("move_reservation", { reservation: booking.reservation, ...args });
			frappe.show_alert({ message: __("Booking moved"), indicator: "green" });
			onMoved && onMoved();
		};
		if (booking.slot_type === "AC") {
			frappe.prompt(
				[
					{ fieldname: "ac_date", fieldtype: "Date", label: __("New installation date"), reqd: 1, default: booking.date },
					{ fieldname: "force", fieldtype: "Check", label: __("Allow over capacity / closed day") },
				],
				done,
				__("Move AC booking"),
				__("Move")
			);
			return;
		}
		const areas = await call("get_areas");
		const d = new frappe.ui.Dialog({
			title: __("Move booking"),
			fields: [
				{ fieldname: "area", fieldtype: "Select", label: __("Area"), reqd: 1, options: areas, default: booking.area },
				{ fieldname: "date", fieldtype: "Date", label: __("Date"), reqd: 1, default: booking.date },
				{ fieldname: "time", fieldtype: "Select", label: __("Time slot"), reqd: 1 },
				{ fieldname: "force", fieldtype: "Check", label: __("Allow over capacity / closed slot") },
			],
			primary_action_label: __("Move"),
			primary_action: async (v) => {
				await done(v);
				d.hide();
			},
		});
		const loadSlots = async () => {
			const { area, date } = d.get_values(true);
			if (!area || !date) return;
			const board = await call("get_slot_board", { area, date });
			d.set_df_property("time", "options", board.slots.map((s) => ({
				value: s.time,
				label: `${timeLabel(s.time)} · ${s.reserved}/${s.capacity}${s.available ? "" : " · " + s.reasons.map((r) => REASON_LABELS[r] || r).join(", ")}`,
			})));
			if (booking.time && area === booking.area && date === booking.date) d.set_value("time", booking.time);
		};
		d.fields_dict.area.df.onchange = loadSlots;
		d.fields_dict.date.df.onchange = loadSlots;
		d.show();
		loadSlots();
	}

	window.dm_slots = { moveBooking, REASON_LABELS, timeLabel };
})();
