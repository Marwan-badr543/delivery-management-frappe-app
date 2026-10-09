// Delivery Slots: one page, two tabs (Delivery Times / AC Installation).
// Every action goes through delivery_management.api.admin -> the slot engine.

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

frappe.pages["delivery-slots"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Delivery Slots"),
		single_column: true,
	});
	wrapper.delivery_slots = new DeliverySlotsPage(page);
};

frappe.pages["delivery-slots"].on_page_show = function (wrapper) {
	wrapper.delivery_slots && wrapper.delivery_slots.refresh();
};

class DeliverySlotsPage {
	constructor(page) {
		this.page = page;
		this.$body = $(`
			<div class="dm-page">
				<div class="dm-tabs" role="tablist">
					<button class="dm-tab-btn active" role="tab" data-tab="delivery">
						${frappe.utils.icon("calendar", "sm")}<span>${__("Delivery times")}</span>
					</button>
					<button class="dm-tab-btn" role="tab" data-tab="ac">
						${frappe.utils.icon("snowflake", "sm")}<span>${__("AC installation")}</span>
					</button>
				</div>
				<div class="dm-tab" data-tab="delivery"></div>
				<div class="dm-tab" data-tab="ac" style="display:none"></div>
			</div>`).appendTo(page.main);

		this.delivery = new DeliveryTab(this.$body.find('.dm-tab[data-tab="delivery"]'));
		this.ac = new AcTab(this.$body.find('.dm-tab[data-tab="ac"]'));
		this.active = "delivery";

		this.$body.on("click", ".dm-tab-btn", (e) => {
			this.active = $(e.currentTarget).data("tab");
			this.$body.find(".dm-tab-btn").removeClass("active");
			$(e.currentTarget).addClass("active");
			this.$body.find(".dm-tab").hide();
			this.$body.find(`.dm-tab[data-tab="${this.active}"]`).show();
			this.refresh();
		});

		page.set_secondary_action(__("Refresh"), () => this.refresh(), "refresh");
		page.add_menu_item(__("Delivery Settings"), () => frappe.set_route("Form", "Delivery Settings"));
		page.add_menu_item(__("Areas"), () => frappe.set_route("List", "Delivery Area"));
		page.add_menu_item(__("Work Hours"), () => frappe.set_route("List", "Delivery Work Hour"));
		page.add_menu_item(__("Reservation Ledger"), () => frappe.set_route("List", "Delivery Slot Reservation"));
	}

	refresh() {
		this[this.active].refresh();
	}
}

// ---------------------------------------------------------------- helpers

function call(method, args) {
	return frappe.call({ method: API + method, args, freeze: true }).then((r) => r.message);
}

function shiftDate(date, days) {
	return frappe.datetime.add_days(date, days);
}

const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));

function longDate(date) {
	const today = frappe.datetime.get_today();
	const label = moment(date).format("dddd, D MMM YYYY");
	if (date === today) return `${label} <span class="dm-today-tag">${__("Today")}</span>`;
	return label;
}

function timeLabel(time) {
	return moment(time, "HH:mm").format("h:mm A");
}

/** free | partial | full | closed | past */
function slotState(s) {
	if ((s.reasons || []).some((r) => ["too_soon", "past_date"].includes(r))) return "past";
	if (s.manually_disabled || (s.reasons || []).some((r) => ["manually_disabled", "area_disabled"].includes(r))) return "closed";
	if ((s.reasons || []).includes("full") || s.remaining <= 0) return "full";
	return s.reserved > 0 ? "partial" : "free";
}

const STATE_LABELS = {
	free: __("Free"),
	partial: __("Partly booked"),
	full: __("Full"),
	closed: __("Closed"),
	past: __("Too soon / passed"),
};

function legend() {
	return `<div class="dm-legend">${Object.entries(STATE_LABELS)
		.map(([k, v]) => `<span><i class="dm-dot dm-dot-${k}"></i>${v}</span>`).join("")}</div>`;
}

function meter(used, total) {
	const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 100;
	const level = pct >= 100 ? "full" : pct >= 50 ? "partial" : "free";
	return `<div class="dm-meter dm-meter-${level}"><i style="width:${pct}%"></i></div>`;
}

function reasonBadges(reasons) {
	if (!reasons.length) return `<span class="indicator-pill green">${__("Available")}</span>`;
	return reasons
		.map((r) => `<span class="indicator-pill ${r === "full" ? "red" : "orange"}">${REASON_LABELS[r] || r}</span>`)
		.join(" ");
}

function referenceLink(row) {
	if (!row.reference_doctype) {
		return `<span class="indicator-pill gray">${__("Manual")}</span>${row.remarks ? ` <b>${esc(row.remarks)}</b>` : ""}`;
	}
	return `<a class="dm-ref" href="${frappe.utils.get_form_link(row.reference_doctype, row.reference_name)}">${esc(row.reference_name)}</a>`;
}

function dateControl($parent, label, value, onchange) {
	const control = frappe.ui.form.make_control({
		parent: $parent,
		df: { fieldtype: "Date", label, fieldname: "date", onchange: () => onchange(control.get_value()) },
		render_input: true,
	});
	control.set_value(value);
	return control;
}

function dayNav() {
	return `<div class="dm-nav btn-group">
		<button class="btn btn-default btn-sm" data-shift="-1" title="${__("Previous day")}">${frappe.utils.icon("left", "sm")}</button>
		<button class="btn btn-default btn-sm" data-shift="0">${__("Today")}</button>
		<button class="btn btn-default btn-sm" data-shift="1" title="${__("Next day")}">${frappe.utils.icon("right", "sm")}</button>
	</div>`;
}

function emptyState(icon, title, text) {
	return `<div class="dm-empty">${frappe.utils.icon(icon, "lg")}<b>${title}</b>${text ? `<span>${text}</span>` : ""}</div>`;
}

// ------------------------------------------------------------ Delivery tab

class DeliveryTab {
	constructor($wrapper) {
		this.$w = $wrapper;
		this.date = frappe.datetime.get_today();
		this.area = null;
		this.selected = null;
		this.$w.html(`
			<section class="dm-panel dm-toolbar">
				<div class="dm-area"></div>
				<div class="dm-date"></div>
				${dayNav()}
				<div class="dm-day-title"></div>
			</section>
			<div class="dm-summary"></div>
			<div class="dm-split">
				<section class="dm-panel">
					<header class="dm-panel-head">
						<h4>${__("Time slots")}</h4>
						${legend()}
					</header>
					<div class="dm-slots"></div>
				</section>
				<section class="dm-panel dm-detail"></section>
			</div>`);

		this.areaControl = frappe.ui.form.make_control({
			parent: this.$w.find(".dm-area"),
			df: {
				fieldtype: "Link", options: "Delivery Area", label: __("Area"), fieldname: "area",
				onchange: () => { this.area = this.areaControl.get_value(); this.selected = null; this.refresh(); },
			},
			render_input: true,
		});
		this.dateControl = dateControl(this.$w.find(".dm-date"), __("Date"), this.date, (v) => {
			if (v && v !== this.date) { this.date = v; this.selected = null; this.refresh(); }
		});
		this.$w.on("click", "[data-shift]", (e) => {
			const shift = cint($(e.currentTarget).data("shift"));
			this.date = shift ? shiftDate(this.date, shift) : frappe.datetime.get_today();
			this.selected = null;
			this.dateControl.set_value(this.date);
		});
		this.$w.on("click", ".dm-slot", (e) => {
			if ($(e.target).closest("button").length) return;
			this.selected = $(e.currentTarget).data("time");
			this.render_slots();
			this.load_detail();
		});
		this.$w.on("click", "[data-toggle-block]", (e) => {
			const $b = $(e.currentTarget);
			this.toggle_block($b.data("time"), cint($b.data("toggle-block")));
		});
	}

	async refresh() {
		if (!this.area) {
			const areas = await frappe.db.get_list("Delivery Area", { fields: ["name"], order_by: "area_name asc", limit: 1 });
			if (!areas.length) {
				this.$w.find(".dm-slots").html(emptyState("map", __("No delivery areas yet"),
					__("Add an area and its work hours to start taking delivery bookings.")));
				return;
			}
			this.area = areas[0].name;
			this.areaControl.set_value(this.area);
			return; // set_value triggers refresh
		}
		this.$w.find(".dm-day-title").html(longDate(this.date));
		this.board = await call("get_slot_board", { area: this.area, date: this.date });
		const slots = this.board.slots;
		const booked = slots.reduce((a, s) => a + s.reserved, 0);
		const free = slots.filter((s) => s.available).reduce((a, s) => a + s.remaining, 0);
		const stat = (label, value, cls = "") => `<div class="dm-stat ${cls}"><span>${label}</span><b>${value}</b></div>`;
		this.$w.find(".dm-summary").html(
			stat(__("Cars per slot"), this.board.cars_number) +
			stat(__("Slots this day"), slots.length) +
			stat(__("Booked"), booked, booked ? "dm-stat-blue" : "") +
			stat(__("Free cars left"), free, free ? "dm-stat-green" : "dm-stat-red") +
			(this.board.enabled ? "" : `<div class="dm-stat dm-warn">${frappe.utils.icon("es-line-alert-circle", "sm")} ${__("This area is turned off: no new bookings.")}</div>`)
		);
		this.render_slots();
		if (this.selected) this.load_detail();
		else this.$w.find(".dm-detail").html(emptyState("select", __("Pick a time slot"),
			__("Click a slot on the left to see who is booked and to add or move bookings.")));
	}

	render_slots() {
		const slots = this.board ? this.board.slots : [];
		if (!slots.length) {
			this.$w.find(".dm-slots").html(emptyState("calendar", __("No delivery on this day"),
				__("{0} has no work hours on {1}. Change the work hours to add slots.", [esc(this.area), moment(this.date).format("dddd")])));
			return;
		}
		this.$w.find(".dm-slots").html(slots.map((s) => {
			const state = slotState(s);
			const status = s.available
				? (s.remaining === 1 ? __("1 car left") : __("{0} cars left", [s.remaining]))
				: (s.reasons || []).map((r) => REASON_LABELS[r] || r).join(", ");
			return `
			<div class="dm-slot dm-slot-${state} ${s.time === this.selected ? "selected" : ""}" data-time="${s.time}"
				tabindex="0" role="button" aria-label="${timeLabel(s.time)} ${STATE_LABELS[state]}">
				<div class="dm-slot-head">
					<span class="dm-time">${timeLabel(s.time)}</span>
					<span class="dm-count" title="${__("Booked / cars")}">${s.reserved}<small>/${s.capacity}</small></span>
				</div>
				${meter(s.reserved, s.capacity)}
				<div class="dm-slot-status">${esc(status)}</div>
				<div class="dm-slot-actions">
					${s.manually_disabled
						? `<button class="btn btn-xs btn-default" data-toggle-block="0" data-time="${s.time}">${frappe.utils.icon("unlock", "xs")} ${__("Reopen")}</button>`
						: `<button class="btn btn-xs btn-default dm-btn-quiet" data-toggle-block="1" data-time="${s.time}">${frappe.utils.icon("lock", "xs")} ${__("Close")}</button>`}
				</div>
			</div>`;
		}).join(""));
	}

	async toggle_block(time, blocked) {
		let remarks = null;
		if (blocked) {
			const values = await new Promise((resolve) =>
				frappe.prompt({ fieldname: "remarks", fieldtype: "Small Text", label: __("Reason (optional)"),
					description: __("No new bookings will be taken in this slot. Existing bookings stay.") },
				resolve, __("Close {0} on {1}?", [timeLabel(time), frappe.datetime.str_to_user(this.date)]), __("Close slot")));
			remarks = values.remarks;
		}
		await call("set_slot_blocked", { area: this.area, date: this.date, time, blocked, remarks });
		frappe.show_alert({ message: blocked ? __("Slot closed") : __("Slot reopened"), indicator: "green" });
		this.refresh();
	}

	async load_detail() {
		const time = this.selected;
		const rows = await call("get_slot_reservations", { area: this.area, date: this.date, time });
		const slot = (this.board.slots || []).find((s) => s.time === time) || {};
		const $d = this.$w.find(".dm-detail");
		$d.html(`
			<header class="dm-panel-head">
				<div>
					<h4>${timeLabel(time)} · ${esc(this.area)}</h4>
					<p>${frappe.datetime.str_to_user(this.date)} · ${__("{0} of {1} cars booked", [slot.reserved || 0, slot.capacity || 0])}</p>
				</div>
				<button class="btn btn-sm btn-primary" data-add>${frappe.utils.icon("add", "xs")} ${__("Add booking")}</button>
			</header>
			${meter(slot.reserved || 0, slot.capacity || 0)}
			<div class="dm-bookings">
			${rows.length ? "" : emptyState("users", __("No bookings yet"), __("Orders placed in the POS appear here automatically."))}
			${rows.map((r) => `
				<div class="dm-booking">
					<div class="dm-booking-main">
						<div class="dm-booking-title">${referenceLink(r)} ${r.forced ? `<span class="indicator-pill orange">${__("Over capacity")}</span>` : ""}</div>
						<div class="dm-booking-meta">
							${esc((r.order && r.order.customer_name) || r.customer || "")}
							${r.contact_phone ? ` · ${frappe.utils.icon("call", "xs")} ${esc(r.contact_phone)}` : ""}
						</div>
						${r.order ? `<div class="dm-booking-meta">${frappe.utils.icon("map-pin", "xs")}
							${__("Block")} ${esc(r.order.dm_block_no || "-")} · ${__("Road")} ${esc(r.order.dm_road_no || "-")} · ${__("Building")} ${esc(r.order.dm_building_no || "-")}</div>` : ""}
					</div>
					<div class="dm-booking-actions">
						<button class="btn btn-xs btn-default" data-move="${r.name}">${__("Move")}</button>
						${r.reference_doctype ? "" : `<button class="btn btn-xs btn-default text-danger" data-release="${r.name}">${__("Release")}</button>`}
					</div>
				</div>`).join("")}
			</div>`);
		$d.find("[data-add]").on("click", () => this.add_booking(time));
		$d.find("[data-move]").on("click", (e) => this.move_booking($(e.currentTarget).data("move")));
		$d.find("[data-release]").on("click", (e) => this.release_booking($(e.currentTarget).data("release")));
	}

	add_booking(time) {
		const d = new frappe.ui.Dialog({
			title: __("Add a manual booking · {0} {1}", [frappe.datetime.str_to_user(this.date), timeLabel(time)]),
			fields: [
				{ fieldname: "remarks", fieldtype: "Data", label: __("Reference / Invoice No"), reqd: 1,
					description: __("For orders taken outside the POS (phone, website...).") },
				{ fieldname: "contact_phone", fieldtype: "Data", label: __("Customer Phone"), options: "Phone" },
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer") },
				{ fieldname: "force", fieldtype: "Check", label: __("Allow over capacity / closed slot") },
			],
			primary_action_label: __("Book"),
			primary_action: async (v) => {
				await call("add_slot_reservation", { area: this.area, date: this.date, time, ...v });
				d.hide();
				frappe.show_alert({ message: __("Booking added"), indicator: "green" });
				this.refresh();
			},
		});
		d.show();
	}

	move_booking(reservation) {
		const d = new frappe.ui.Dialog({
			title: __("Move booking"),
			fields: [
				{ fieldname: "area", fieldtype: "Link", options: "Delivery Area", label: __("Area"), reqd: 1, default: this.area },
				{ fieldname: "date", fieldtype: "Date", label: __("Date"), reqd: 1, default: this.date },
				{ fieldname: "time", fieldtype: "Select", label: __("Time slot"), reqd: 1 },
				{ fieldname: "force", fieldtype: "Check", label: __("Allow over capacity / closed slot") },
			],
			primary_action_label: __("Move"),
			primary_action: async (v) => {
				await call("move_reservation", { reservation, ...v });
				d.hide();
				frappe.show_alert({ message: __("Booking moved"), indicator: "green" });
				this.refresh();
			},
		});
		const loadSlots = async () => {
			const { area, date } = d.get_values(true);
			if (!area || !date) return;
			const board = await call("get_slot_board", { area, date });
			const options = board.slots.map((s) => ({
				value: s.time,
				label: `${timeLabel(s.time)} · ${s.reserved}/${s.capacity}${s.available ? "" : " · " + s.reasons.map((r) => REASON_LABELS[r] || r).join(", ")}`,
			}));
			d.set_df_property("time", "options", options);
		};
		d.fields_dict.area.df.onchange = loadSlots;
		d.fields_dict.date.df.onchange = loadSlots;
		d.show();
		loadSlots();
	}

	release_booking(reservation) {
		frappe.confirm(__("Release this manual booking? The car becomes free for other orders."), async () => {
			await call("release_reservation", { reservation });
			frappe.show_alert({ message: __("Booking released"), indicator: "green" });
			this.refresh();
		});
	}
}

// ------------------------------------------------------------------ AC tab

class AcTab {
	constructor($wrapper) {
		this.$w = $wrapper;
		this.date = frappe.datetime.get_today();
		this.$w.html(`
			<section class="dm-panel dm-toolbar">
				<div class="dm-date"></div>
				${dayNav()}
				<div class="dm-day-title"></div>
			</section>
			<section class="dm-panel">
				<header class="dm-panel-head">
					<div><h4>${__("Next 14 days")}</h4><p>${__("Units booked / daily capacity. Click a day to manage it.")}</p></div>
					${legend().replace(/<span><i class="dm-dot dm-dot-past"><\/i>[^<]*<\/span>/, "")}
				</header>
				<div class="dm-strip"></div>
			</section>
			<div class="dm-split">
				<section class="dm-panel dm-ac-day"></section>
				<section class="dm-panel dm-ac-orders"></section>
			</div>`);
		this.dateControl = dateControl(this.$w.find(".dm-date"), __("Installation date"), this.date, (v) => {
			if (v && v !== this.date) { this.date = v; this.refresh(); }
		});
		this.$w.on("click", "[data-shift]", (e) => {
			const shift = cint($(e.currentTarget).data("shift"));
			this.date = shift ? shiftDate(this.date, shift) : frappe.datetime.get_today();
			this.dateControl.set_value(this.date);
		});
		this.$w.on("click", ".dm-chip", (e) => this.dateControl.set_value($(e.currentTarget).data("date")));
	}

	async refresh() {
		this.$w.find(".dm-day-title").html(longDate(this.date));
		const [overview, day] = await Promise.all([
			call("get_ac_overview", { start_date: frappe.datetime.get_today(), days: 14 }),
			call("get_ac_day", { date: this.date }),
		]);
		this.render_strip(overview);
		this.render_day(day);
		this.render_orders(day.reservations);
	}

	render_strip(days) {
		this.$w.find(".dm-strip").html(days.map((d) => {
			const state = d.manually_disabled ? "closed" : d.remaining <= 0 ? "full" : d.reserved > 0 ? "partial" : "free";
			return `
			<button class="dm-chip dm-chip-${state} ${d.date === this.date ? "selected" : ""}" data-date="${d.date}">
				<span class="dm-chip-day">${moment(d.date).format("ddd")}</span>
				<span class="dm-chip-date">${moment(d.date).format("D MMM")}</span>
				<b>${d.reserved}<small>/${d.capacity}</small></b>
				${meter(d.reserved, d.capacity)}
				${d.manually_disabled ? `<span class="dm-chip-note">${__("Closed")}</span>` : ""}
			</button>`;
		}).join(""));
	}

	render_day(day) {
		const $d = this.$w.find(".dm-ac-day");
		$d.html(`
			<header class="dm-panel-head">
				<div><h4>${frappe.datetime.str_to_user(this.date)}</h4><p>${reasonBadges(day.reasons)}</p></div>
				<button class="btn btn-sm btn-primary" data-reserve>${frappe.utils.icon("add", "xs")} ${__("Reserve units")}</button>
			</header>
			<div class="dm-summary dm-summary-tight">
				<div class="dm-stat"><span>${__("Booked units")}</span><b>${day.reserved}</b></div>
				<div class="dm-stat"><span>${__("Capacity")}</span><b>${day.capacity}</b>
					<small>${day.has_capacity_override ? __("set for this date") : __("default (Delivery Settings)")}</small></div>
				<div class="dm-stat ${day.remaining > 0 ? "dm-stat-green" : "dm-stat-red"}"><span>${__("Free")}</span><b>${day.remaining}</b></div>
			</div>
			${meter(day.reserved, day.capacity)}
			<div class="dm-ac-settings">
				<label>${__("Capacity for this date only")}</label>
				<div class="dm-inline">
					<input type="number" min="0" class="form-control input-sm" data-capacity placeholder="${day.capacity}"
						value="${day.has_capacity_override ? day.capacity : ""}">
					<button class="btn btn-sm btn-default" data-set-capacity>${__("Save")}</button>
					${day.has_capacity_override ? `<button class="btn btn-sm btn-link" data-clear-capacity>${__("Back to default")}</button>` : ""}
				</div>
				<div class="dm-ac-close">
					${day.manually_disabled
						? `<button class="btn btn-sm btn-default" data-block="0">${frappe.utils.icon("unlock", "xs")} ${__("Reopen this day")}</button>`
						: `<button class="btn btn-sm btn-default text-danger" data-block="1">${frappe.utils.icon("lock", "xs")} ${__("Close this day")}</button>`}
				</div>
			</div>`);
		$d.find("[data-block]").on("click", (e) => {
			const blocked = cint($(e.currentTarget).data("block"));
			const apply = async () => {
				await call("set_ac_day_blocked", { date: this.date, blocked });
				frappe.show_alert({ message: blocked ? __("AC day closed") : __("AC day reopened"), indicator: "green" });
				this.refresh();
			};
			if (blocked) {
				frappe.confirm(__("Close {0} for AC installations? Existing bookings stay; no new ones are taken.",
					[frappe.datetime.str_to_user(this.date)]), apply);
			} else apply();
		});
		$d.find("[data-set-capacity]").on("click", async () => {
			const value = $d.find("[data-capacity]").val();
			if (value === "" || cint(value) < 0) {
				frappe.msgprint(__("Enter a capacity of 0 or more."));
				return;
			}
			await call("set_ac_day_capacity", { date: this.date, capacity: cint(value) });
			frappe.show_alert({ message: __("Capacity updated"), indicator: "green" });
			this.refresh();
		});
		$d.find("[data-clear-capacity]").on("click", async () => {
			await call("set_ac_day_capacity", { date: this.date, capacity: "" });
			this.refresh();
		});
		$d.find("[data-reserve]").on("click", () => this.reserve_units());
	}

	render_orders(rows) {
		const $o = this.$w.find(".dm-ac-orders");
		const head = `<header class="dm-panel-head"><div><h4>${__("AC bookings")}</h4>
			<p>${__("{0} booking(s) on this day", [rows.length])}</p></div></header>`;
		if (!rows.length) {
			$o.html(head + emptyState("snowflake", __("No AC installations booked"), __("Orders with AC units book this day automatically.")));
			return;
		}
		$o.html(`${head}
			<div class="dm-scroll"><table class="dm-table">
				<thead><tr>
					<th>${__("Order")}</th><th>${__("Customer")}</th><th>${__("AC items")}</th>
					<th class="text-right">${__("Units")}</th><th></th>
				</tr></thead>
				<tbody>${rows.map((r) => `
					<tr>
						<td>${referenceLink(r)} ${r.forced ? `<span class="indicator-pill orange">${__("Over capacity")}</span>` : ""}</td>
						<td>${esc((r.order && r.order.customer_name) || r.customer || "")}
							${r.contact_phone ? `<div class="small text-muted">${esc(r.contact_phone)}</div>` : ""}</td>
						<td>${(r.ac_items || []).map((i) => `<div class="small">${esc(i.item_code)} × ${i.stock_qty}</div>`).join("") || "-"}</td>
						<td class="text-right"><b>${r.qty}</b></td>
						<td class="text-right text-nowrap">
							<button class="btn btn-xs btn-default" data-move="${r.name}">${__("Move")}</button>
							${r.reference_doctype ? "" : `<button class="btn btn-xs btn-default text-danger" data-release="${r.name}">${__("Release")}</button>`}
						</td>
					</tr>`).join("")}
				</tbody>
			</table></div>`);
		$o.find("[data-move]").on("click", (e) => this.move_booking($(e.currentTarget).data("move")));
		$o.find("[data-release]").on("click", (e) => {
			const reservation = $(e.currentTarget).data("release");
			frappe.confirm(__("Release this manual booking? The units become free for other orders."), async () => {
				await call("release_reservation", { reservation });
				frappe.show_alert({ message: __("Booking released"), indicator: "green" });
				this.refresh();
			});
		});
	}

	reserve_units() {
		const d = new frappe.ui.Dialog({
			title: __("Reserve AC units · {0}", [frappe.datetime.str_to_user(this.date)]),
			fields: [
				{ fieldname: "units", fieldtype: "Int", label: __("Units"), reqd: 1, default: 1 },
				{ fieldname: "remarks", fieldtype: "Data", label: __("Reference / Reason"), reqd: 1 },
				{ fieldname: "contact_phone", fieldtype: "Data", label: __("Customer Phone"), options: "Phone" },
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer") },
				{ fieldname: "force", fieldtype: "Check", label: __("Allow over capacity / closed day") },
			],
			primary_action_label: __("Reserve"),
			primary_action: async (v) => {
				await call("add_ac_reservation", { date: this.date, ...v });
				d.hide();
				frappe.show_alert({ message: __("Units reserved"), indicator: "green" });
				this.refresh();
			},
		});
		d.show();
	}

	move_booking(reservation) {
		frappe.prompt(
			[
				{ fieldname: "ac_date", fieldtype: "Date", label: __("New installation date"), reqd: 1, default: this.date },
				{ fieldname: "force", fieldtype: "Check", label: __("Allow over capacity / closed day") },
			],
			async (v) => {
				await call("move_reservation", { reservation, ...v });
				frappe.show_alert({ message: __("Booking moved"), indicator: "green" });
				this.refresh();
			},
			__("Move AC booking"),
			__("Move")
		);
	}
}
