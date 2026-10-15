// Installation Statistics: units each driver / AC Tech delivered or installed, counted
// from submitted Delivery Notes. Port of the old "Installation Statistics" page.
// A driver sees only his own numbers. No caching: every change reads the database.

frappe.pages["installation-statistics"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Installation Statistics"), single_column: true });
	wrapper.dm_page = new InstallationStatsPage(page);
};

frappe.pages["installation-statistics"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.load();
};

class InstallationStatsPage {
	constructor(page) {
		const D = window.dm_drivers;
		this.D = D;
		this.page = page;
		const today = frappe.datetime.get_today();
		this.f = { start_date: frappe.datetime.month_start(), end_date: today, technician: "", unit: "all" };
		this.preset = "month";
		this.technicians = null;
		this.$root = $(`<div class="dd-page">
			<div class="dd-panel dd-filter-panel"></div>
			<div class="dd-kpis"></div>
			<div class="dd-panel">
				<div class="dd-panel-head"><div><h4>${frappe.utils.icon("users", "sm")} ${__("Technician performance")}</h4>
					<p>${__("Submitted Delivery Notes only (delivered / installed). AC units = AC items; TV & non-AC = other stock items.")}</p></div></div>
				<div class="dd-scroll"><table class="dd-table dd-cards dd-stats-table"><thead><tr>
					<th>${__("Technician / Driver")}</th><th>${__("AC units")}</th><th>${__("TV & non-AC units")}</th>
					<th>${__("Orders")}</th><th>${__("Total units")}</th></tr></thead><tbody></tbody><tfoot></tfoot></table></div>
			</div>
		</div>`).appendTo(page.main);
		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		this.renderFilters();
	}

	renderFilters() {
		const D = this.D;
		const f = this.f;
		const presets = [["today", __("Today")], ["7", __("Last 7 days")], ["month", __("This month")]];
		const techs = this.technicians || [];
		this.$root.find(".dd-filter-panel").html(`
			<div class="dd-panel-head">
				<h4>${frappe.utils.icon("calendar", "sm")} ${__("Period and filters")}</h4>
				<div class="dd-presets">${presets.map(([k, l]) =>
					`<button class="dd-preset ${this.preset === k ? "active" : ""}" data-preset="${k}">${l}</button>`).join("")}</div>
			</div>
			<div class="dd-filters">
				<div class="dd-field"><label>${__("Start date")}</label><input type="date" class="form-control" data-f="start_date" value="${f.start_date}"></div>
				<div class="dd-field"><label>${__("End date")}</label><input type="date" class="form-control" data-f="end_date" value="${f.end_date}"></div>
				${this.canManage === false ? "" : `<div class="dd-field"><label>${__("Technician / Driver")}</label>
					<select class="form-control" data-f="technician">${D.select(
						[{ value: "", label: __("All technicians") }, ...techs.map((t) => ({ value: t.user, label: t.full_name }))],
						f.technician)}</select></div>`}
				<div class="dd-field"><label>${__("Unit type")}</label>
					<select class="form-control" data-f="unit">${D.select([
						{ value: "all", label: __("All unit types") },
						{ value: "ac", label: __("AC units only") },
						{ value: "non_ac", label: __("TV & non-AC only") },
					], f.unit)}</select></div>
			</div>`);
		const $p = this.$root.find(".dd-filter-panel");
		$p.find("[data-f]").on("change", (e) => {
			this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
			if (["start_date", "end_date"].includes($(e.currentTarget).data("f"))) {
				this.preset = "";
				$p.find(".dd-preset").removeClass("active");
			}
			this.load();
		});
		$p.find("[data-preset]").on("click", (e) => {
			const today = frappe.datetime.get_today();
			this.preset = String($(e.currentTarget).data("preset"));
			this.f.end_date = today;
			this.f.start_date = this.preset === "today" ? today
				: this.preset === "7" ? frappe.datetime.add_days(today, -7) : frappe.datetime.month_start();
			this.renderFilters();
			this.load();
		});
	}

	async load() {
		const D = this.D;
		if (this.f.start_date > this.f.end_date) {
			frappe.show_alert({ message: __("Start date is after the end date."), indicator: "orange" });
			return;
		}
		const $tbody = this.$root.find(".dd-stats-table tbody");
		if (!this.rows) $tbody.html(D.skeleton(5, 4));
		const seq = (this.seq = (this.seq || 0) + 1);
		const res = await D.call("get_installation_stats", this.f);
		if (seq !== this.seq) return;
		this.rows = res.rows;
		const firstLoad = this.canManage === undefined;
		this.canManage = res.can_manage;
		if (firstLoad || (res.technicians.length && !this.technicians)) {
			this.technicians = res.technicians;
			this.renderFilters();
		}
		this.render();
	}

	render() {
		const D = this.D;
		const rows = this.rows;
		const ac = rows.reduce((s, r) => s + r.ac_units, 0);
		const tv = rows.reduce((s, r) => s + r.non_ac_units, 0);
		const orders = rows.reduce((s, r) => s + r.orders, 0);
		const total = ac + tv;
		const n = (v) => format_number(v, null, v % 1 ? 2 : 0);
		this.$root.find(".dd-kpis").html(`
			<div class="dd-kpi dd-kpi-ac"><div><span>${__("AC units installed")}</span><b>${n(ac)}</b><small>${__("Items in the AC item groups")}</small></div>
				<div class="dd-kpi-icon">${frappe.utils.icon("snowflake", "lg")}</div></div>
			<div class="dd-kpi dd-kpi-tv"><div><span>${__("TV & non-AC delivered")}</span><b>${n(tv)}</b><small>${__("TVs and other stock items")}</small></div>
				<div class="dd-kpi-icon">${frappe.utils.icon("truck", "lg")}</div></div>
			<div class="dd-kpi dd-kpi-all"><div><span>${__("Total units")}</span><b>${n(total)}</b><small>${__("{0} orders", [orders])}</small></div>
				<div class="dd-kpi-icon">${frappe.utils.icon("check", "lg")}</div></div>`);
		const $tbody = this.$root.find(".dd-stats-table tbody");
		const $tfoot = this.$root.find(".dd-stats-table tfoot");
		if (!rows.length) {
			$tbody.html(D.emptyRow(5, __("No installations"), __("No submitted Delivery Note with a driver in this period.")));
			$tfoot.empty();
			return;
		}
		const pct = (v) => (total ? Math.min(100, (v / total) * 100) : 0).toFixed(1);
		$tbody.html(rows.map((r) => `
			<tr data-user="${D.esc(r.technician)}">
				<td data-label="${__("Technician")}"><span style="display:inline-flex;align-items:center;gap:8px">
					${D.avatar(r.full_name)}<b>${D.esc(r.full_name)}</b></span></td>
				<td data-label="${__("AC units")}"><div class="dd-bar-cell"><span class="dd-num dd-num-ac">${n(r.ac_units)}</span>
					<span class="dd-bar dd-bar-ac"><i style="width:${pct(r.ac_units)}%"></i></span></div></td>
				<td data-label="${__("TV & non-AC")}"><div class="dd-bar-cell"><span class="dd-num dd-num-tv">${n(r.non_ac_units)}</span>
					<span class="dd-bar dd-bar-tv"><i style="width:${pct(r.non_ac_units)}%"></i></span></div></td>
				<td data-label="${__("Orders")}">${r.orders}</td>
				<td data-label="${__("Total")}"><b>${n(r.total_units)}</b></td>
			</tr>`).join(""));
		$tfoot.html(`<tr><th>${__("Total")}</th><th>${n(ac)}</th><th>${n(tv)}</th><th>${orders}</th><th>${n(total)}</th></tr>`);
		$tbody.find("tr[data-user]").on("click", (e) => {
			const row = rows.find((r) => r.technician === $(e.currentTarget).data("user"));
			frappe.route_options = { name: ["in", row.delivery_notes] };
			frappe.set_route("List", "Delivery Note");
		});
	}
}
