// Driver Assignment: give each Delivery Note waiting for delivery a TV Driver (Non AC
// orders) or an AC Tech (AC orders). Port of the old admin pages "Driver Assignment"
// and "AC Technician Assignment". Clicking an order opens its Delivery Note.

frappe.pages["driver-assignment"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Driver Assignment"), single_column: true });
	wrapper.dm_page = new DriverAssignmentPage(page);
};

frappe.pages["driver-assignment"].on_page_show = function (wrapper) {
	const p = wrapper.dm_page;
	if (!p) return;
	// Coming from a Delivery Note's "Assign Driver" button.
	if (frappe.route_options && (frappe.route_options.tab || frappe.route_options.search)) {
		const { tab, search } = frappe.route_options;
		frappe.route_options = null;
		p.applyRoute(tab, search);
	} else {
		p.load();
	}
};

const DA_TYPES = { tv: "Non AC", ac: "AC" };

class DriverAssignmentPage {
	constructor(page) {
		const D = window.dm_drivers;
		this.D = D;
		this.page = page;
		const saved = D.prefs("assignment");
		this.tab = saved.tab || "tv";
		this.pageSize = saved.pageSize || 20;
		this.filters = { tv: this.blankFilters(), ac: this.blankFilters() };
		this.offset = 0;
		this.drivers = {};

		this.$root = $(`<div class="dd-page">
			${D.tabs([
				{ key: "tv", label: __("TV / Non AC orders"), icon: "truck" },
				{ key: "ac", label: __("AC orders"), icon: "snowflake" },
			], this.tab)}
			<div class="dd-untyped"></div>
			<div class="dd-panel dd-filter-panel"></div>
			<div class="dd-panel dd-loads-panel" style="display:none"></div>
			<div class="dd-panel dd-table-panel">
				<div class="dd-scroll"><table class="dd-table dd-cards">
					<thead></thead><tbody></tbody></table></div>
			</div>
		</div>`).appendTo(page.main);

		this.$root.on("click", ".dd-tab-btn", (e) => {
			this.tab = $(e.currentTarget).data("tab");
			this.$root.find(".dd-tab-btn").removeClass("active");
			$(e.currentTarget).addClass("active");
			this.offset = 0;
			this.savePrefs();
			this.renderFilters();
			this.load();
		});

		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		page.add_menu_item(__("Delivery Schedule"), () => frappe.set_route("delivery-schedule"));
		page.add_menu_item(__("Installation Statistics"), () => frappe.set_route("installation-statistics"));
		page.add_menu_item(__("Driver Schedule (list)"), () => frappe.set_route("List", "Driver Schedule"));
		page.add_menu_item(__("AC Tech Day Limits"), () => frappe.set_route("Form", "Delivery Settings"));
		page.add_menu_item(__("Delivery Settings"), () => frappe.set_route("Form", "Delivery Settings"));

		this.renderFilters();
	}

	blankFilters() {
		return { search: "", assigned_status: "", driver: "", area: "", delivery_date: "" };
	}

	get type() {
		return DA_TYPES[this.tab];
	}

	get f() {
		return this.filters[this.tab];
	}

	savePrefs() {
		this.D.prefs("assignment", { tab: this.tab, pageSize: this.pageSize });
	}

	applyRoute(tab, search) {
		if (tab && DA_TYPES[tab]) {
			this.tab = tab;
			this.$root.find(".dd-tab-btn").removeClass("active");
			this.$root.find(`.dd-tab-btn[data-tab="${tab}"]`).addClass("active");
		}
		this.filters[this.tab] = { ...this.blankFilters(), search: search || "" };
		this.offset = 0;
		this.renderFilters();
		this.load();
	}

	async driversFor(type) {
		if (!this.drivers[type]) this.drivers[type] = await this.D.call("get_drivers", { order_type: type });
		return this.drivers[type];
	}

	// ------------------------------------------------------------ filters

	async renderFilters() {
		const D = this.D;
		const ac = this.tab === "ac";
		const [drivers, areas] = await Promise.all([this.driversFor(this.type), D.areas()]);
		const f = this.f;
		const who = ac ? __("AC Tech") : __("Driver");
		this.$root.find(".dd-filter-panel").html(`
			<div class="dd-filters">
				<div class="dd-field dd-search">
					<label>${__("Search")}</label>
					<span class="dd-search-icon">${frappe.utils.icon("search", "sm")}</span>
					<input class="form-control" data-f="search" placeholder="${__("DN number, customer, phone")}" value="${D.esc(f.search)}">
				</div>
				<div class="dd-field"><label>${__("Assignment")}</label>
					<select class="form-control" data-f="assigned_status">${D.select([
						{ value: "", label: __("All orders") },
						{ value: "unassigned", label: __("Not assigned yet") },
						{ value: "assigned", label: __("Assigned") },
					], f.assigned_status)}</select></div>
				<div class="dd-field"><label>${who}</label>
					<select class="form-control" data-f="driver">${D.select(
						[{ value: "", label: __("All") }, ...drivers.map((d) => ({ value: d.user, label: d.full_name }))],
						f.driver)}</select></div>
				<div class="dd-field"><label>${__("Area")}</label>
					<select class="form-control" data-f="area">${D.select(
						[{ value: "", label: __("All areas") }, ...areas.map((a) => ({ value: a, label: a }))], f.area)}</select></div>
				<div class="dd-field"><label>${ac ? __("Installation date") : __("Delivery date")}</label>
					<input type="date" class="form-control" data-f="delivery_date" value="${D.esc(f.delivery_date)}"></div>
			</div>
			<div class="dd-filter-foot">
				<span class="dd-hint">${ac
					? __("Pick a date to see each AC Tech's load and set a limit for that day.")
					: __("Orders waiting for delivery, earliest first. Click an order to open its Delivery Note.")}</span>
				<div class="dd-actions">
					<select class="form-control input-xs" data-page-size style="width:auto;height:30px">${D.select(
						[10, 20, 50, 100].map((n) => ({ value: n, label: __("{0} per page", [n]) })), this.pageSize)}</select>
					<button class="btn btn-default btn-sm" data-clear>${frappe.utils.icon("es-line-reload", "xs")} ${__("Clear filters")}</button>
					<div class="dd-pager">
						<button class="btn btn-xs btn-default" data-prev>${frappe.utils.icon("left", "xs")}</button>
						<span data-page-label>–</span>
						<button class="btn btn-xs btn-default" data-next>${frappe.utils.icon("right", "xs")}</button>
					</div>
				</div>
			</div>`);

		const $p = this.$root.find(".dd-filter-panel");
		const apply = frappe.utils.debounce(() => { this.offset = 0; this.load(); }, 300);
		$p.find("[data-f]").on("input change", (e) => {
			this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
			apply();
		});
		$p.find("[data-page-size]").on("change", (e) => {
			this.pageSize = cint($(e.currentTarget).val());
			this.offset = 0;
			this.savePrefs();
			this.load();
		});
		$p.find("[data-clear]").on("click", () => {
			this.filters[this.tab] = this.blankFilters();
			this.offset = 0;
			this.renderFilters();
			this.load();
		});
		$p.find("[data-prev]").on("click", () => {
			this.offset = Math.max(0, this.offset - this.pageSize);
			this.load();
		});
		$p.find("[data-next]").on("click", () => {
			if (this.offset + this.pageSize < this.total) {
				this.offset += this.pageSize;
				this.load();
			}
		});
	}

	// ------------------------------------------------------------ data

	async load() {
		const D = this.D;
		const ac = this.tab === "ac";
		const $thead = this.$root.find(".dd-table thead");
		const $tbody = this.$root.find(".dd-table tbody");
		$thead.html(`<tr>
			<th>${__("Delivery Note")}</th><th>${__("Customer")}</th><th>${__("Address")}</th>
			<th>${ac ? __("AC installation") : __("Delivery")}</th><th>${__("Type")}</th><th>${__("Payment")}</th>
			<th>${ac ? __("AC Tech") : __("Driver")}</th></tr>`);
		if (!this.rows) $tbody.html(D.skeleton(7));
		this.$root.find(".dd-table-panel").addClass("dd-loading");
		const seq = (this.seq = (this.seq || 0) + 1);
		let res;
		try {
			res = await D.call("get_assignment_orders", {
				order_type: this.type, ...this.f, limit: this.pageSize, offset: this.offset,
			});
		} finally {
			this.$root.find(".dd-table-panel").removeClass("dd-loading");
		}
		if (seq !== this.seq) return;
		this.rows = res.rows;
		this.total = res.total;
		this.renderRows();
		this.renderUntyped(res.untyped);
		this.renderPager();
		this.$root.find(`[data-count="${this.tab}"]`).text(res.total);
		this.loadOtherCount();
		this.renderLoads();
	}

	async loadOtherCount() {
		const other = this.tab === "tv" ? "ac" : "tv";
		const r = await this.D.call("get_assignment_orders", { order_type: DA_TYPES[other], limit: 1 });
		this.$root.find(`[data-count="${other}"]`).text(r.total);
	}

	renderPager() {
		const pages = Math.max(1, Math.ceil(this.total / this.pageSize));
		const current = Math.floor(this.offset / this.pageSize) + 1;
		this.$root.find("[data-page-label]").text(this.total
			? __("Page {0} of {1} · {2} orders", [current, pages, this.total])
			: __("No orders"));
		this.$root.find("[data-prev]").prop("disabled", this.offset === 0);
		this.$root.find("[data-next]").prop("disabled", this.offset + this.pageSize >= this.total);
	}

	renderUntyped(list) {
		const D = this.D;
		const $u = this.$root.find(".dd-untyped");
		if (!list || !list.length) return $u.empty();
		$u.html(`<div class="dd-banner dd-banner-warn">${frappe.utils.icon("es-line-alert-circle", "md")}<div>
			<b>${__("{0} order(s) have AC and non-AC items and no Order Type.", [list.length])}</b>
			${__("They are not on either tab until you open them and choose AC or Non AC:")}
			<ul>${list.slice(0, 8).map((d) => `<li><a href="/desk/delivery-note/${encodeURIComponent(d.name)}">${D.esc(d.name)}</a>
				· ${D.esc(d.customer_name || "")}</li>`).join("")}</ul></div></div>`);
	}

	renderRows() {
		const D = this.D;
		const ac = this.tab === "ac";
		const $tbody = this.$root.find(".dd-table tbody");
		if (!this.rows.length) {
			$tbody.html(D.emptyRow(7, __("No orders found"),
				__("Nothing waiting for delivery matches these filters.")));
			return;
		}
		$tbody.html(this.rows.map((r) => `
			<tr data-name="${D.esc(r.name)}" tabindex="0">
				<td data-label="${__("Delivery Note")}"><span class="dd-dn">${D.esc(r.name)}</span>
					<span class="dd-sub">${D.date(r.posting_date)}</span></td>
				<td data-label="${__("Customer")}"><b>${D.esc(r.customer_name || r.customer)}</b>
					<span class="dd-sub">${D.phoneLink(r.phone)}</span></td>
				<td data-label="${__("Address")}" class="dd-addr">${D.esc(r.address) || `<span class="dd-muted">—</span>`}</td>
				<td data-label="${ac ? __("AC installation") : __("Delivery")}">${D.whenCell(r)}
					${ac && r.ac_units ? `<span class="dd-sub">${__("{0} AC unit(s)", [r.ac_units])}</span>` : ""}</td>
				<td data-label="${__("Type")}">${D.typePill(r.order_type)}</td>
				<td data-label="${__("Payment")}">${D.paymentPill(r.payment)}</td>
				<td data-label="${ac ? __("AC Tech") : __("Driver")}">
					<button class="dd-driver-btn ${r.driver ? "assigned" : "unassigned"}" data-assign="${D.esc(r.name)}">
						<span>${r.driver ? D.esc(r.driver_name || r.driver) : (ac ? __("Assign AC Tech") : __("Assign driver"))}</span>
						${frappe.utils.icon(r.driver ? "edit" : "add", "xs")}
					</button></td>
			</tr>`).join(""));

		$tbody.find("tr[data-name]").on("click keydown", (e) => {
			if (e.type === "keydown" && e.key !== "Enter") return;
			if ($(e.target).closest("[data-assign], a").length) return;
			frappe.set_route("Form", "Delivery Note", $(e.currentTarget).data("name"));
		});
		$tbody.find("[data-assign]").on("click", (e) => {
			e.stopPropagation();
			const row = this.rows.find((r) => r.name === $(e.currentTarget).data("assign"));
			this.openPicker(row);
		});
	}

	// ------------------------------------------------------------ assign

	async openPicker(row) {
		const D = this.D;
		const ac = row.order_type === "AC";
		const d = new frappe.ui.Dialog({
			title: ac ? __("AC Tech for {0}", [row.name]) : __("Driver for {0}", [row.name]),
			size: "large",
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
		});
		const $b = d.fields_dict.body.$wrapper.html(`<div class="dd-empty">${__("Checking availability…")}</div>`);
		d.show();
		d.$wrapper.on("hidden.bs.modal", () => d.$wrapper.remove());
		const info = await D.call("get_driver_options", { delivery_note: row.name });
		const when = ac
			? `${D.relativeDay(info.delivery_date)} · ${D.date(info.delivery_date)}`
			: `${D.date(info.delivery_date)} · ${D.time(info.delivery_time) || __("no time")}`;
		const rule = ac
			? (info.max_orders_per_day
				? __("Each AC Tech can take {0} AC orders a day (per-day limits on this page).", [info.max_orders_per_day])
				: __("No daily limit set (Delivery Settings)."))
			: __("A driver is busy if he has another order within {0} hour(s) of this time.", [info.window_hours]);
		if (info.message) {
			$b.html(`<div class="dd-banner dd-banner-warn">${D.esc(info.message)}</div>`);
			return;
		}
		const available = info.drivers.filter((x) => x.available).length;
		$b.html(`
			<div class="dd-pick-head">
				<span>${__("Customer")}: <b>${D.esc(row.customer_name)}</b></span>
				<span>${ac ? __("Installation") : __("Delivery")}: <b>${D.esc(when)}</b></span>
				<span>${__("Area")}: <b>${D.esc(row.area || "—")}</b></span>
				<span>${__("Available")}: <b>${available} / ${info.drivers.length}</b></span>
			</div>
			<p class="text-muted small">${D.esc(rule)}</p>
			${info.drivers.length ? `<div class="dd-pick-list">${info.drivers.map((x) => `
				<button class="dd-pick ${x.user === info.current_driver ? "current" : ""}" data-user="${D.esc(x.user)}"
					${x.available || x.user === info.current_driver ? "" : "disabled"}
					title="${x.available ? "" : D.esc(__("Not available for this order"))}">
					${D.avatar(x.full_name)}
					<span class="dd-pick-body"><b>${D.esc(x.full_name)}</b>
						<small class="${x.available ? "dd-ok" : "dd-no"}">${x.user === info.current_driver ? __("Assigned now")
							: x.available ? (x.label ? __("Available · {0}", [D.esc(x.label)]) : __("Available"))
							: D.esc(x.label || __("Unavailable"))}</small></span>
				</button>`).join("")}</div>`
				: `<div class="dd-banner dd-banner-warn">${ac
					? __("No user has the AC Tech role. Give it to your technicians in User settings.")
					: __("No user has the TV Driver role. Give it to your drivers in User settings.")}</div>`}`);

		$b.find(".dd-pick:not([disabled])").on("click", async (e) => {
			const user = $(e.currentTarget).data("user");
			if (user === info.current_driver) return d.hide();
			await D.call("assign_driver", { delivery_note: row.name, driver: user }, { post: true, freeze: true });
			frappe.show_alert({ message: __("{0} assigned to {1}", [row.name, $(e.currentTarget).find("b").text()]), indicator: "green" });
			d.hide();
			this.load();
		});
		if (info.current_driver) {
			d.set_secondary_action_label(ac ? __("Unassign AC Tech") : __("Unassign driver"));
			d.set_secondary_action(() => {
				frappe.confirm(__("Remove {0} from {1}?", [info.current_driver, row.name]), async () => {
					await D.call("assign_driver", { delivery_note: row.name, driver: "" }, { post: true, freeze: true });
					frappe.show_alert({ message: __("Unassigned"), indicator: "orange" });
					d.hide();
					this.load();
				});
			});
		}
	}

	// ------------------------------------------------------------ AC tech load

	async renderLoads() {
		const D = this.D;
		const $p = this.$root.find(".dd-loads-panel");
		const day = this.f.delivery_date;
		if (this.tab !== "ac" || !day) return $p.hide();
		const res = await D.call("get_ac_loads", { work_date: day });
		$p.show().html(`
			<div class="dd-panel-head">
				<h4>${frappe.utils.icon("users", "sm")} ${__("AC Tech load on {0}", [D.date(day)])}</h4>
				<span class="dd-muted small">${res.max_orders_per_day
					? __("Limit {0} orders a day", [res.max_orders_per_day])
					: __("No daily limit set")} · ${__("click a technician to change his limit for this day")}</span>
			</div>
			${res.loads.length ? `<div class="dd-loads">${res.loads.map((l) => `
				<button class="dd-load ${l.is_day_off ? "off" : l.is_full ? "full" : ""}" data-user="${D.esc(l.user)}">
					${D.esc(l.full_name)}
					<small>${l.is_day_off ? __("Day off") : `${l.assigned_orders}${l.max_orders != null ? "/" + l.max_orders : ""}`}
						${l.is_full && !l.is_day_off ? " · " + __("Full") : ""}${l.has_override && !l.is_day_off ? " · " + __("Custom") : ""}</small>
					${frappe.utils.icon("edit", "xs")}
				</button>`).join("")}</div>`
				: `<span class="dd-muted">${__("No user has the AC Tech role yet.")}</span>`}`);
		$p.find(".dd-load").on("click", (e) => {
			const load = res.loads.find((l) => l.user === $(e.currentTarget).data("user"));
			this.editLimit(load, day, res.max_orders_per_day);
		});
	}

	editLimit(load, day, globalMax) {
		const D = this.D;
		const d = new frappe.ui.Dialog({
			title: __("Daily limit · {0}", [load.full_name]),
			fields: [
				{ fieldtype: "HTML", options: `<div class="dd-pick-head"><span>${__("Day")}: <b>${D.date(day)}</b></span>
					<span>${__("Already booked")}: <b>${load.assigned_orders}</b></span></div>` },
				{
					// Data, not Int: an empty Int reads as 0, which would mean "day off".
					fieldname: "max_orders", fieldtype: "Data", label: __("AC orders allowed on this day"),
					default: load.has_override ? String(load.max_orders) : "",
					description: __("Empty = follow the global limit ({0}). 0 = day off: he cannot be assigned any AC order on this day.",
						[globalMax || __("no limit")]),
				},
			],
			primary_action_label: __("Save"),
			primary_action: async (v) => {
				const raw = String(v.max_orders ?? "").trim();
				if (raw && !/^\d+$/.test(raw)) {
					frappe.msgprint(__("Enter a whole number (0 or more), or leave it empty."));
					return;
				}
				const value = raw === "" ? "" : cint(raw);
				await D.call("set_ac_limit", { driver: load.user, work_date: day, max_orders: value }, { post: true, freeze: true });
				d.hide();
				this.renderLoads();
			},
			secondary_action_label: __("Use global limit"),
			secondary_action: async () => {
				await D.call("set_ac_limit", { driver: load.user, work_date: day, max_orders: "" }, { post: true, freeze: true });
				d.hide();
				this.renderLoads();
			},
		});
		d.show();
		d.$wrapper.on("hidden.bs.modal", () => d.$wrapper.remove());
	}
}
