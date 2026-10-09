// Sales Statistics: per salesman (the cashier who made the sale) for a period:
// invoices, delivery orders, returns, totals, AC / non-AC units and commission.
// The numbers are computed by a background job (delivery_management.api.sales_stats);
// this page only shows them. The commission is the only figure computed here:
// amount (Total or Net Total) x the salesman's commission % / 100.

const SS_API = "delivery_management.api.sales_stats.";
const SS_STORE = "dm_sales_stats_prefs";
const SS_PER_PAGE = 100;

frappe.pages["sales-statistics"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Sales Statistics"),
		single_column: true,
	});
	wrapper.sales_statistics = new SalesStatisticsPage(page);
};

class SalesStatisticsPage {
	constructor(page) {
		this.page = page;
		this.prefs = this.load_prefs();
		this.result = null;
		this.key = null;
		this.poll = null;
		this.invoice_user = null;
		this.invoice_page = 0;
		this.invoice_search = "";

		this.$body = $(`
			<div class="ss-page">
				<section class="ss-panel ss-filter-panel">
					<div class="ss-filters"></div>
					<div class="ss-filter-foot">
						<span class="ss-presets-label">${__("Quick period")}</span>
						<div class="ss-presets" role="group" aria-label="${__("Quick period")}"></div>
					</div>
				</section>
				<div class="ss-state"></div>
				<div class="ss-cards"></div>
				<section class="ss-panel ss-table-panel hidden">
					<header class="ss-panel-head">
						<div>
							<h4>${__("Sales by salesman")}</h4>
							<p>${__("Click a salesman to see their invoices. Type a commission % in a row to give that salesman their own rate.")}</p>
						</div>
					</header>
					<div class="ss-table"></div>
				</section>
				<section class="ss-panel ss-invoice-panel hidden"></section>
			</div>`).appendTo(page.main);

		// Every Show is a fresh calculation (nothing is reused from an earlier run).
		page.set_primary_action(__("Show"), () => this.run(), "search");
		page.add_inner_button(__("Export CSV"), () => this.export_csv());

		frappe.call({ method: SS_API + "get_setup" }).then((r) => {
			this.setup = r.message;
			// Controls apply their values asynchronously; run once they are all set.
			Promise.all(this.make_filters()).then(() => this.run());
		});

		frappe.realtime.on("dm_sales_stats", (data) => {
			if (data && data.key === this.key) this.check_status();
		});
	}

	// ------------------------------------------------------------ filters

	make_filters() {
		const $f = this.$body.find(".ss-filters");
		const today = frappe.datetime.get_today();
		const pending = [];
		const make = (df, cls = "") => {
			const field = frappe.ui.form.make_control({
				df,
				parent: $(`<div class="ss-filter ${cls}">`).appendTo($f),
				render_input: true,
			});
			if (df.default !== undefined) pending.push(field.set_value(df.default));
			return field;
		};
		this.from_date = make({ fieldtype: "Date", label: __("From"), fieldname: "from_date",
			default: frappe.datetime.month_start(), reqd: 1 });
		this.to_date = make({ fieldtype: "Date", label: __("To"), fieldname: "to_date", default: today, reqd: 1 });
		if (this.setup.is_manager) {
			this.user = make({ fieldtype: "Link", options: "User", label: __("Salesman"), fieldname: "user",
				placeholder: __("All salesmen"),
				get_query: () => ({ filters: { user_type: "System User", enabled: 1 } }) });
		}
		this.company = make({ fieldtype: "Link", options: "Company", label: __("Company"), fieldname: "company",
			default: this.setup.company });
		this.commission = make({ fieldtype: "Float", label: __("Commission %"), fieldname: "commission",
			default: this.prefs.commission || 0,
			description: __("For every salesman"),
			change: () => {
				this.prefs.commission = flt(this.commission.get_value());
				this.save_prefs();
				this.render();
			} }, "ss-filter-sm");
		this.basis_labels = { total: __("Total (incl. VAT)"), net_total: __("Net Total (excl. VAT)") };
		this.basis = make({ fieldtype: "Select", label: __("Commission on"), fieldname: "basis",
			options: Object.values(this.basis_labels).join("\n"),
			default: this.basis_labels[this.prefs.basis] || this.basis_labels.total,
			change: () => {
				const label = this.basis.get_value();
				this.prefs.basis = Object.keys(this.basis_labels).find((k) => this.basis_labels[k] === label) || "total";
				this.save_prefs();
				this.render();
			} });

		const $p = this.$body.find(".ss-presets");
		const last_month_end = frappe.datetime.add_days(frappe.datetime.month_start(), -1);
		const presets = [
			[__("Today"), today, today],
			[__("Last 7 days"), frappe.datetime.add_days(today, -6), today],
			[__("This month"), frappe.datetime.month_start(), today],
			[__("Last month"), moment(last_month_end).startOf("month").format("YYYY-MM-DD"), last_month_end],
		];
		presets.forEach(([label, from, to]) => {
			$(`<button class="ss-preset" type="button">${label}</button>`)
				.attr("data-from", from)
				.attr("data-to", to)
				.appendTo($p)
				.on("click", () => {
					Promise.all([this.from_date.set_value(from), this.to_date.set_value(to)]).then(() => this.run());
				});
		});
		return pending;
	}

	filters() {
		return {
			from_date: this.from_date.get_value(),
			to_date: this.to_date.get_value(),
			company: this.company.get_value(),
			user: this.user ? this.user.get_value() : null,
		};
	}

	mark_preset() {
		const f = this.filters();
		this.$body.find(".ss-preset").each((_i, el) => {
			$(el).toggleClass("active", el.dataset.from === f.from_date && el.dataset.to === f.to_date);
		});
	}

	// ------------------------------------------------------------ job

	run() {
		const filters = this.filters();
		if (!filters.from_date || !filters.to_date) {
			frappe.msgprint(__("Choose the From and To dates."));
			return;
		}
		this.mark_preset();
		this.stop_poll();
		this.set_state("loading");
		frappe
			.call({ method: SS_API + "start", args: filters })
			.then((r) => this.handle(r.message))
			.catch(() => this.set_state("idle"));
	}

	handle(state) {
		if (!state) return;
		this.key = state.key;
		if (state.status === "done") {
			this.stop_poll();
			this.result = state.result;
			this.invoice_user = null;
			this.invoice_page = 0;
			this.invoice_search = "";
			this.set_state("done");
			this.render();
		} else if (state.status === "failed" || state.status === "expired") {
			this.stop_poll();
			this.set_state("error", state.error || __("The result expired. Click Show again."));
		} else {
			this.set_state("loading");
			// Realtime tells us when it is ready; poll too in case the socket is down.
			if (!this.poll) this.poll = setInterval(() => this.check_status(), 3000);
		}
	}

	check_status() {
		if (!this.key) return;
		frappe.call({ method: SS_API + "status", args: { key: this.key } }).then((r) => this.handle(r.message));
	}

	stop_poll() {
		if (this.poll) clearInterval(this.poll);
		this.poll = null;
	}

	set_state(state, message) {
		const $s = this.$body.find(".ss-state").empty();
		this.$body.toggleClass("ss-is-loading", state === "loading");
		this.page.btn_primary && this.page.btn_primary.prop("disabled", state === "loading");
		if (state === "loading") {
			$s.html(`<div class="ss-banner ss-banner-info">
				<span class="ss-spinner"></span>
				<div><b>${__("Calculating...")}</b>
				<span>${__("This runs in the background. You can keep working; the numbers appear here when ready.")}</span></div>
			</div>`);
			if (!this.result) this.render_skeleton();
		} else if (state === "error") {
			$s.html(`<div class="ss-banner ss-banner-error">${frappe.utils.icon("es-line-alert-circle", "md")}
				<div><b>${__("Could not calculate")}</b><span>${frappe.utils.escape_html(message)}</span></div></div>`);
		} else if (state === "done" && this.result) {
			const f = this.result.filters;
			const at = frappe.datetime.str_to_user(this.result.generated_at.slice(0, 10)) + " " + this.result.generated_at.slice(11, 16);
			$s.html(`<div class="ss-period">
				<div class="ss-period-main">
					${frappe.utils.icon("calendar", "sm")}
					<b>${__("{0} to {1}", [frappe.datetime.str_to_user(f.from_date), frappe.datetime.str_to_user(f.to_date)])}</b>
					<span class="ss-dot">·</span><span>${frappe.utils.escape_html(f.company)}</span>
					<span class="ss-dot">·</span><span class="text-muted">${__("calculated at {0}", [at])}</span>
				</div>
				<details class="ss-rules">
					<summary>${__("What is counted?")}</summary>
					<ul>
						<li>${__("Only <b>paid</b> sales invoices (nothing outstanding). Unpaid orders count once they are fully paid.")}</li>
						<li>${__("By the day the invoice was <b>created</b>, even if its Posting Date was changed in the POS.")}</li>
						<li>${__("The salesman is the cashier who made the sale.")}</li>
						<li>${__("Returns against paid invoices are subtracted. Drafts and cancelled invoices never count.")}</li>
						<li>${__("Total = what the customer pays (rounded, with VAT). Net Total = before VAT.")}</li>
					</ul>
				</details>
			</div>`);
		}
	}

	render_skeleton() {
		this.$body.find(".ss-cards").html(Array.from({ length: 7 }, () =>
			`<div class="ss-card ss-skeleton"><span></span><b></b></div>`).join(""));
	}

	// ------------------------------------------------------------ render

	basis_key() {
		return this.prefs.basis === "net_total" ? "net_total" : "total";
	}

	commission_pct(user) {
		const own = (this.prefs.per_user || {})[user];
		return own === undefined || own === null || own === "" ? flt(this.prefs.commission) : flt(own);
	}

	commission_of(row) {
		return (flt(row[this.basis_key()]) * this.commission_pct(row.user)) / 100;
	}

	money(value) {
		return format_currency(value, this.result.currency);
	}

	render() {
		if (!this.result) return;
		const rows = this.result.rows;
		const t = this.result.totals;
		const total_commission = rows.reduce((sum, r) => sum + this.commission_of(r), 0);
		const basis_label = this.basis_labels[this.basis_key()];

		const card = (label, value, icon, cls = "", hint = "") =>
			`<div class="ss-card ${cls}">
				<div class="ss-card-top"><span class="ss-card-label">${label}</span>
				<span class="ss-card-icon">${frappe.utils.icon(icon, "sm")}</span></div>
				<b>${value}</b>${hint ? `<small>${hint}</small>` : ""}
			</div>`;
		this.$body.find(".ss-cards").html(
			card(__("Total sales"), this.money(t.total), "income", "ss-card-hero",
				__("Net {0}", [this.money(t.net_total)])) +
				card(__("Commission"), this.money(total_commission), "star", "ss-card-green", __("on {0}", [basis_label])) +
				card(__("Invoices"), t.invoices, "file", "", t.returns ? __("{0} returns", [t.returns]) : "") +
				card(__("Delivery orders"), t.orders, "assets") +
				card(__("AC units"), t.ac_units, "snowflake", "ss-card-blue") +
				card(__("Other units"), t.non_ac_units, "package", "ss-card-purple") +
				card(__("All units"), flt(t.ac_units) + flt(t.non_ac_units), "list")
		);

		this.render_invoices();
		this.$body.find(".ss-table-panel").removeClass("hidden");
		const $t = this.$body.find(".ss-table").empty();
		if (!rows.length) {
			$t.html(`<div class="ss-empty">
				${frappe.utils.icon("search", "lg")}
				<b>${__("No paid invoices in this period")}</b>
				<span>${__("Try a longer period, another company, or check that the invoices are paid.")}</span>
			</div>`);
			return;
		}
		const esc = frappe.utils.escape_html;
		const max_total = Math.max(...rows.map((r) => Math.abs(flt(r.total))), 1);
		const head = [
			[__("Salesman"), ""], [__("Invoices"), "r"], [__("Delivery orders"), "r"], [__("Returns"), "r"],
			[__("Total"), "r"], [__("Net total"), "r"], [__("AC units"), "r"], [__("Other units"), "r"],
			[__("Commission %"), "r"], [__("Commission"), "r"],
		];
		const $table = $(`<div class="ss-scroll"><table class="ss-grid">
			<thead><tr>${head.map(([h, a]) => `<th class="${a ? "text-right" : ""}">${h}</th>`).join("")}</tr></thead>
			<tbody></tbody><tfoot></tfoot></table></div>`).appendTo($t);

		rows.forEach((r) => {
			const own = (this.prefs.per_user || {})[r.user];
			const share = Math.round((Math.abs(flt(r.total)) / max_total) * 100);
			const selected = this.invoice_user === r.user ? "ss-row-active" : "";
			$(`<tr class="${selected}">
				<td>
					<button class="ss-user" type="button" title="${__("Show this salesman's invoices")}">
						${frappe.avatar(r.user, "avatar-small")}
						<span><b>${esc(r.full_name)}</b><small>${esc(r.user)}</small></span>
					</button>
				</td>
				<td class="text-right">${r.invoices}</td>
				<td class="text-right">${r.orders}</td>
				<td class="text-right">${r.returns || `<span class="text-muted">0</span>`}</td>
				<td class="text-right">
					<div class="ss-amount">${this.money(r.total)}</div>
					<div class="ss-bar"><i style="width:${share}%"></i></div>
				</td>
				<td class="text-right text-muted">${this.money(r.net_total)}</td>
				<td class="text-right">${r.ac_units}</td>
				<td class="text-right">${r.non_ac_units}</td>
				<td class="text-right"><input type="number" min="0" step="0.01" class="form-control ss-pct"
					aria-label="${__("Commission % for {0}", [esc(r.full_name)])}"
					placeholder="${flt(this.prefs.commission)}" value="${own === undefined || own === null ? "" : own}"></td>
				<td class="text-right ss-commission">${this.money(this.commission_of(r))}</td>
			</tr>`)
				.appendTo($table.find("tbody"))
				.on("click", ".ss-user", () => {
					this.invoice_user = r.user;
					this.invoice_page = 0;
					this.render();
					this.$body.find(".ss-invoice-panel")[0].scrollIntoView({ behavior: "smooth", block: "start" });
				})
				.on("change", ".ss-pct", (e) => {
					this.prefs.per_user = this.prefs.per_user || {};
					const v = $(e.target).val();
					if (v === "") delete this.prefs.per_user[r.user];
					else this.prefs.per_user[r.user] = flt(v);
					this.save_prefs();
					this.render();
				});
		});
		$table.find("tfoot").html(`<tr>
			<th>${__("Total")}</th>
			<th class="text-right">${t.invoices}</th>
			<th class="text-right">${t.orders}</th>
			<th class="text-right">${t.returns}</th>
			<th class="text-right">${this.money(t.total)}</th>
			<th class="text-right">${this.money(t.net_total)}</th>
			<th class="text-right">${t.ac_units}</th>
			<th class="text-right">${t.non_ac_units}</th>
			<th></th>
			<th class="text-right">${this.money(total_commission)}</th>
		</tr>`);
	}

	render_invoices() {
		const $w = this.$body.find(".ss-invoice-panel").empty().removeClass("hidden");
		if (!this.result) return;
		const esc = frappe.utils.escape_html;
		const names = Object.fromEntries(this.result.rows.map((r) => [r.user, r.full_name]));
		const all = this.result.invoices || [];
		const q = (this.invoice_search || "").trim().toLowerCase();
		const list = all.filter((i) =>
			(!this.invoice_user || i.user === this.invoice_user) &&
			(!q || [i.name, i.customer_name, i.customer, i.return_against].some((v) => (v || "").toLowerCase().includes(q)))
		);
		const pages = Math.max(1, Math.ceil(list.length / SS_PER_PAGE));
		this.invoice_page = Math.min(this.invoice_page || 0, pages - 1);
		const shown = list.slice(this.invoice_page * SS_PER_PAGE, (this.invoice_page + 1) * SS_PER_PAGE);
		const who = this.invoice_user ? names[this.invoice_user] || this.invoice_user : null;

		const $head = $(`<header class="ss-panel-head">
			<div>
				<h4>${__("Invoices counted")} <span class="ss-count">${list.length}</span></h4>
				<p>${who ? __("Showing {0} only.", [`<b>${esc(who)}</b>`]) : __("Every invoice behind the numbers above.")}</p>
			</div>
			<div class="ss-inv-tools">
				${who ? `<button class="btn btn-default btn-sm ss-inv-all">${frappe.utils.icon("close", "xs")} ${__("All salesmen")}</button>` : ""}
				<input type="search" class="form-control ss-inv-search" placeholder="${__("Search invoice or customer")}" value="${esc(this.invoice_search)}">
			</div>
		</header>`).appendTo($w);
		$head.on("click", ".ss-inv-all", () => {
			this.invoice_user = null;
			this.invoice_page = 0;
			this.render();
		});
		$head.on("input", ".ss-inv-search", frappe.utils.debounce((e) => {
			this.invoice_search = e.target.value;
			this.invoice_page = 0;
			this.render_invoices();
			const $input = this.$body.find(".ss-inv-search");
			const len = $input.val().length;
			$input.focus()[0].setSelectionRange(len, len);
		}, 250));

		if (!list.length) {
			$w.append(`<div class="ss-empty ss-empty-sm"><span>${q ? __("No invoice matches your search.") : __("No invoices.")}</span></div>`);
			return;
		}
		const type_class = { Order: "blue", Invoice: "gray", Return: "red" };
		const $table = $(`<div class="ss-scroll"><table class="ss-grid ss-grid-compact"><thead><tr>
			<th>${__("Invoice No.")}</th><th>${__("Created")}</th><th>${__("Posting date")}</th><th>${__("Salesman")}</th>
			<th>${__("Customer")}</th><th>${__("Type")}</th><th>${__("Status")}</th>
			<th class="text-right">${__("Total")}</th><th class="text-right">${__("Net total")}</th>
			<th class="text-right">${__("AC")}</th><th class="text-right">${__("Other")}</th>
		</tr></thead><tbody></tbody></table></div>`).appendTo($w);
		shown.forEach((inv) => {
			const against = inv.return_against ? `<small class="text-muted">${__("against")} ${esc(inv.return_against)}</small>` : "";
			const moved = inv.posting_date !== inv.created.slice(0, 10);
			$table.find("tbody").append(`<tr>
				<td><a class="ss-inv-link" href="${frappe.utils.get_form_link("Sales Invoice", inv.name)}" target="_blank">${esc(inv.name)}</a></td>
				<td class="text-nowrap">${frappe.datetime.str_to_user(inv.created.slice(0, 10))} <span class="text-muted">${inv.created.slice(11, 16)}</span></td>
				<td class="text-nowrap ${moved ? "ss-moved" : ""}" ${moved ? `title="${__("Posting date differs from the day it was created")}"` : ""}>${frappe.datetime.str_to_user(inv.posting_date)}</td>
				<td>${esc(names[inv.user] || inv.user)}</td>
				<td>${esc(inv.customer_name)}</td>
				<td><span class="indicator-pill ${type_class[inv.type] || "gray"}">${__(inv.type)}</span> ${against}</td>
				<td>${__(inv.status)}</td>
				<td class="text-right ss-amount">${this.money(inv.total)}</td>
				<td class="text-right text-muted">${this.money(inv.net_total)}</td>
				<td class="text-right">${inv.ac_units}</td>
				<td class="text-right">${inv.non_ac_units}</td></tr>`);
		});
		if (pages > 1) {
			const $nav = $(`<div class="ss-inv-nav">
				<span class="text-muted">${__("{0}–{1} of {2}", [this.invoice_page * SS_PER_PAGE + 1, this.invoice_page * SS_PER_PAGE + shown.length, list.length])}</span>
				<div class="btn-group">
					<button class="btn btn-default btn-sm ss-prev" ${this.invoice_page ? "" : "disabled"}>‹ ${__("Previous")}</button>
					<button class="btn btn-default btn-sm ss-next" ${this.invoice_page < pages - 1 ? "" : "disabled"}>${__("Next")} ›</button>
				</div>
			</div>`).appendTo($w);
			$nav.on("click", ".ss-prev", () => { this.invoice_page--; this.render_invoices(); });
			$nav.on("click", ".ss-next", () => { this.invoice_page++; this.render_invoices(); });
		}
	}

	export_csv() {
		if (!this.result) {
			frappe.show_alert({ message: __("Click Show first."), indicator: "orange" });
			return;
		}
		const rows = [[__("Salesman"), __("User"), __("Invoices"), __("Delivery Orders"), __("Returns"), __("Total"),
			__("Net Total"), __("AC Units"), __("Non-AC Units"), __("Commission %"), __("Commission")]];
		this.result.rows.forEach((r) =>
			rows.push([r.full_name, r.user, r.invoices, r.orders, r.returns, r.total, r.net_total, r.ac_units,
				r.non_ac_units, this.commission_pct(r.user), flt(this.commission_of(r), 3)])
		);
		rows.push([]);
		rows.push([__("Invoice No."), __("Created"), __("Posting Date"), __("User"), __("Customer"), __("Type"),
			__("Status"), __("Total"), __("Net Total"), __("AC Units"), __("Non-AC Units")]);
		(this.result.invoices || []).forEach((i) =>
			rows.push([i.name, i.created, i.posting_date, i.user, i.customer_name, i.type, i.status, i.total, i.net_total,
				i.ac_units, i.non_ac_units])
		);
		const f = this.result.filters;
		frappe.tools.downloadify(rows, null, `sales-statistics-${f.from_date}-to-${f.to_date}`);
	}

	// ------------------------------------------------------------ prefs (this browser only)

	load_prefs() {
		try {
			return JSON.parse(localStorage.getItem(SS_STORE)) || {};
		} catch (e) {
			return {};
		}
	}

	save_prefs() {
		try {
			localStorage.setItem(SS_STORE, JSON.stringify(this.prefs));
		} catch (e) {
			// private mode: keep the values for this visit only
		}
	}
}
