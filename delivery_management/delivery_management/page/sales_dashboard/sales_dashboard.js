// Sales Dashboard: port of the old admin "Dashboard Summary" page — totals, invoices by
// status, orders by branch (and Online) and the top products of each. Like Sales
// Statistics, every load is a fresh background job; the page waits for its result.

const SD_API = "delivery_management.api.dashboard.";

frappe.pages["sales-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Sales Dashboard"), single_column: true });
	wrapper.dm_page = new SalesDashboardPage(page);
};

frappe.pages["sales-dashboard"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.load();
};

const STATUS_COLORS = {
	Paid: "#22c55e", Unpaid: "#f59e0b", "Partly Paid": "#eab308", Overdue: "#ef4444", Cancelled: "#94a3b8",
	Return: "#a855f7", "Credit Note Issued": "#6366f1", Draft: "#64748b",
};

class SalesDashboardPage {
	constructor(page) {
		this.D = window.dm_drivers;
		this.f = { start_date: frappe.datetime.month_start(), end_date: frappe.datetime.get_today() };
		this.area = "";
		this.preset = "month";
		this.$root = $(`<div class="dd-page">
			<div class="dd-panel dd-filter-panel"></div>
			<div class="dd-state"></div>
			<div class="dd-kpis dd-kpis-4"></div>
			<div class="dd-chart-grid">
				<div class="dd-panel"><div class="dd-panel-head"><div><h4>${frappe.utils.icon("chart", "sm")} ${__("Order status distribution")}</h4>
					<p>${__("Sales Invoices of the period by their current status")}</p></div></div><div class="dd-chart" data-chart="status"></div></div>
				<div class="dd-panel"><div class="dd-panel-head"><div><h4>${frappe.utils.icon("map", "sm")} ${__("Orders by area")}</h4>
					<p>${__("Submitted sales per branch (POS Profile) and Online")}</p></div></div><div class="dd-chart" data-chart="areas"></div></div>
			</div>
			<div class="dd-panel">
				<div class="dd-panel-head"><div><h4>${frappe.utils.icon("star", "sm")} ${__("Top products by location")}</h4>
					<p>${__("The 20 best-selling items by quantity")}</p></div><div class="dd-presets dd-area-tabs"></div></div>
				<div class="dd-scroll"><table class="dd-table dd-cards dd-top-table"><thead><tr>
					<th>#</th><th>${__("Item")}</th><th>${__("Quantity")}</th></tr></thead><tbody></tbody></table></div>
			</div>
		</div>`).appendTo(page.main);
		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		this.renderFilters();
		frappe.realtime.on("dm_sales_dashboard", (data) => {
			if (data && data.key === this.key) this.checkStatus();
		});
	}

	renderFilters() {
		const f = this.f;
		const presets = [["today", __("Today")], ["7", __("Last 7 days")], ["month", __("This month")], ["year", __("This year")]];
		this.$root.find(".dd-filter-panel").html(`
			<div class="dd-panel-head">
				<h4>${frappe.utils.icon("calendar", "sm")} ${__("Period")}</h4>
				<div class="dd-presets">${presets.map(([k, l]) =>
					`<button class="dd-preset ${this.preset === k ? "active" : ""}" data-preset="${k}">${l}</button>`).join("")}</div>
			</div>
			<div class="dd-filters">
				<div class="dd-field"><label>${__("Start date")}</label><input type="date" class="form-control" data-f="start_date" value="${f.start_date}"></div>
				<div class="dd-field"><label>${__("End date")}</label><input type="date" class="form-control" data-f="end_date" value="${f.end_date}"></div>
			</div>`);
		const $p = this.$root.find(".dd-filter-panel");
		$p.find("[data-f]").on("change", (e) => {
			this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
			this.preset = "";
			$p.find(".dd-preset").removeClass("active");
			this.load();
		});
		$p.find("[data-preset]").on("click", (e) => {
			const today = frappe.datetime.get_today();
			this.preset = String($(e.currentTarget).data("preset"));
			this.f.end_date = today;
			this.f.start_date = {
				today, 7: frappe.datetime.add_days(today, -7), month: frappe.datetime.month_start(),
				year: frappe.datetime.year_start(),
			}[this.preset];
			this.renderFilters();
			this.load();
		});
	}

	load() {
		if (this.f.start_date > this.f.end_date) {
			frappe.show_alert({ message: __("Start date is after the end date."), indicator: "orange" });
			return;
		}
		this.stopPoll();
		this.key = null;
		this.setState("loading");
		frappe.call({ method: SD_API + "start", args: this.f })
			.then((r) => this.handle(r.message))
			.catch(() => this.setState("error", __("The dashboard could not be started.")));
	}

	handle(state) {
		if (!state) return;
		this.key = state.key;
		if (state.status === "done") {
			this.stopPoll();
			this.data = state.result;
			this.setState("done");
			this.render();
		} else if (state.status === "failed" || state.status === "expired") {
			this.stopPoll();
			this.setState("error", state.error || __("The result expired. Click Refresh."));
		} else if (!this.poll) {
			// Realtime says when it is ready; poll too in case the socket is down.
			this.poll = setInterval(() => this.checkStatus(), 3000);
		}
	}

	checkStatus() {
		if (!this.key) return;
		const key = this.key;
		frappe.call({ method: SD_API + "status", args: { key } }).then((r) => key === this.key && this.handle(r.message));
	}

	stopPoll() {
		if (this.poll) clearInterval(this.poll);
		this.poll = null;
	}

	setState(state, message) {
		const $s = this.$root.find(".dd-state").empty();
		if (state === "loading") {
			$s.html(`<div class="dd-panel dd-muted">${frappe.utils.icon("refresh", "sm")}
				<b>${__("Calculating...")}</b> ${__("This runs in the background; the numbers appear here when ready.")}</div>`);
			if (!this.data) this.$root.find(".dd-top-table tbody").html(this.D.skeleton(3, 5));
		} else if (state === "error") {
			$s.html(`<div class="dd-panel text-danger"><b>${__("Could not calculate")}</b> ${this.D.esc(message)}</div>`);
		} else if (state === "done") {
			const at = this.data.generated_at;
			$s.html(`<div class="dd-muted small">${__("Calculated at {0}", [frappe.datetime.str_to_user(at.slice(0, 10)) + " " + at.slice(11, 16)])}</div>`);
		}
	}

	render() {
		const { summary, status, areas } = this.data;
		const n = (v) => format_number(v, null, v % 1 ? 2 : 0);
		const kpi = (cls, label, value, note, icon, route) => `
			<div class="dd-kpi ${cls}" ${route ? `data-route="${route}" style="cursor:pointer"` : ""}>
				<div><span>${label}</span><b>${n(value)}</b><small>${note}</small></div>
				<div class="dd-kpi-icon">${frappe.utils.icon(icon, "lg")}</div></div>`;
		this.$root.find(".dd-kpis").html(
			kpi("dd-kpi-all", __("Total orders"), summary.orders, __("Sales Invoices, all time"), "file", "List/Sales Invoice") +
			kpi("dd-kpi-ac", __("Active products"), summary.products, __("Enabled sales items"), "stock", "List/Item") +
			kpi("dd-kpi-tv", __("Customers"), summary.customers, __("With a submitted invoice"), "users", "List/Customer") +
			kpi("dd-kpi-warn", __("Pending orders"), summary.pending_orders, __("Draft Delivery Notes (not delivered)"), "truck",
				"List/Delivery Note"));
		this.$root.find("[data-route]").on("click", (e) => frappe.set_route($(e.currentTarget).data("route")));

		const total = status.reduce((s, r) => s + r.count, 0);
		this.chart("status", total ? {
			type: "donut", height: 260,
			data: { labels: status.map((r) => `${__(r.status)} (${((r.count / total) * 100).toFixed(1)}%)`),
				datasets: [{ values: status.map((r) => r.count) }] },
			colors: status.map((r) => STATUS_COLORS[r.status] || "#0ea5e9"),
		} : null);
		this.chart("areas", areas.some((a) => a.count) ? {
			type: "bar", height: 260,
			data: { labels: areas.map((a) => __(a.label)), datasets: [{ name: __("Orders"), values: areas.map((a) => a.count) }] },
			colors: ["#4d8ee5"],
		} : null);

		const tabs = [{ area: "", label: __("All") }, ...areas];
		const $tabs = this.$root.find(".dd-area-tabs").html(tabs.map((t) =>
			`<button class="dd-preset ${this.area === t.area ? "active" : ""}" data-area="${this.D.esc(t.area)}">${this.D.esc(__(t.label))}</button>`).join(""));
		$tabs.find("[data-area]").on("click", (e) => {
			this.area = String($(e.currentTarget).data("area"));
			this.render();
		});

		const top_products = this.data.top_products[this.area] || [];
		const $tbody = this.$root.find(".dd-top-table tbody");
		if (!top_products.length) {
			$tbody.html(this.D.emptyRow(3, __("No sales"), __("No submitted sale in this period.")));
			return;
		}
		const max = top_products[0].qty || 1;
		$tbody.html(top_products.map((p, i) => `
			<tr data-item="${this.D.esc(p.item_code)}" style="cursor:pointer">
				<td data-label="#">${i + 1}</td>
				<td data-label="${__("Item")}"><b>${this.D.esc(p.item_name || p.item_code)}</b></td>
				<td data-label="${__("Quantity")}"><div class="dd-bar-cell"><span class="dd-num dd-num-ac">${n(p.qty)}</span>
					<span class="dd-bar dd-bar-ac"><i style="width:${Math.min(100, (p.qty / max) * 100).toFixed(1)}%"></i></span></div></td>
			</tr>`).join(""));
		$tbody.find("tr[data-item]").on("click", (e) => frappe.set_route("Form", "Item", $(e.currentTarget).data("item")));
	}

	chart(key, options) {
		const $el = this.$root.find(`[data-chart="${key}"]`).empty();
		if (!options) {
			$el.html(`<div class="dd-muted" style="padding:40px;text-align:center">${__("No data in this period.")}</div>`);
			return;
		}
		new frappe.Chart($el[0], { ...options, axisOptions: { xIsSeries: 0 }, barOptions: { spaceRatio: 0.5 } });
	}
}
