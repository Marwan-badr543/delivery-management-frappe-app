// Sales Statistics: per salesman (the cashier who made the sale) for a period: sales and
// small items against their monthly targets, invoices, units and commission.
// Everything, the commission included, is computed by a background job
// (delivery_management.api.sales_stats); this page only shows it. The only choice made here
// is the commission basis (Total or Net Total), and both are in the result. "Exclude services"
// (on by default) leaves the Service Item Groups of Delivery Settings out of the sales.
// The invoices behind the numbers are loaded a page at a time.

const SS_API = "delivery_management.api.sales_stats.";
const SS_STORE = "dm_sales_stats_prefs";

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
		this.inv = null;

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
							<p class="ss-range"></p>
						</div>
					</header>
					<div class="ss-table"></div>
				</section>
				<section class="ss-panel ss-breakdown-panel hidden"></section>
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
		this.pos_profile = make({ fieldtype: "Link", options: "POS Profile", label: __("POS Profile"), fieldname: "pos_profile",
			placeholder: __("All POS Profiles") });
		this.exclude_services = make({ fieldtype: "Check", label: __("Exclude services"), fieldname: "exclude_services",
			default: 1,
			change: () => this.result && this.run() });
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
			exclude_services: this.exclude_services.get_value() ? 1 : 0,
			pos_profile: this.pos_profile.get_value() || null,
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
			this.load_invoices();
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
						<li>${__("Targets are monthly and set on each User. Small items are items marked Small Item.")}</li>
						<li>${__("Commission % = Min + (small items sold ÷ target) × (Max − Min), never above Max. 75% of the commission is paid on it; the other 25% follows the latest Sales Evaluation in the period.")}</li>
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

	money(value) {
		return format_currency(value, this.result.currency);
	}

	pct(value) {
		return `${flt(value, 2)}%`;
	}

	// Progress toward a target: a bar and a "Reached" badge or the % reached so far.
	progress(value, target, format) {
		if (!flt(target)) return `<div class="ss-target ss-muted">${__("No target")}</div>`;
		const ratio = flt(value) / flt(target);
		const reached = ratio >= 1;
		return `<div class="ss-target">
			<div class="ss-target-line"><span>${__("of {0}", [format(target)])}</span>
				<span class="ss-badge ${reached ? "ss-badge-ok" : "ss-badge-no"}">${reached
					? `${frappe.utils.icon("check", "xs")} ${__("Reached")}` : this.pct(ratio * 100)}</span></div>
			<div class="ss-bar ${reached ? "ss-bar-ok" : ""}"><i style="width:${Math.min(Math.max(ratio, 0), 1) * 100}%"></i></div>
		</div>`;
	}

	evaluation_cell(row) {
		const ev = row.evaluation;
		if (ev) {
			return `<a class="ss-eval" href="${frappe.utils.get_form_link("Sales Evaluation", ev.name)}" target="_blank"
				title="${__("{0} to {1}", [frappe.datetime.str_to_user(ev.from_date), frappe.datetime.str_to_user(ev.to_date)])}${ev.overlaps ? "" : " · " + __("latest evaluation, outside this period")}">${this.pct(ev.score)}${ev.overlaps ? "" : "*"}</a>`;
		}
		return this.setup.can_evaluate
			? `<button type="button" class="btn btn-xs btn-default ss-evaluate">${__("Evaluate")}</button>`
			: `<span class="ss-muted">${__("Not evaluated")}</span>`;
	}

	evaluate(row) {
		const f = this.result.filters;
		frappe.new_doc("Sales Evaluation", { salesman: row.user, from_date: f.from_date, to_date: f.to_date });
	}

	render() {
		if (!this.result) return;
		const rows = this.result.rows;
		const t = this.result.totals;
		const basis = this.basis_key();
		const range = this.result.commission_range;

		const card = (label, value, icon, cls = "", hint = "") =>
			`<div class="ss-card ${cls}">
				<div class="ss-card-top"><span class="ss-card-label">${label}</span>
				<span class="ss-card-icon">${frappe.utils.icon(icon, "sm")}</span></div>
				<b>${value}</b>${hint ? `<small>${hint}</small>` : ""}
			</div>`;
		const of_target = (value, target, format) => flt(target)
			? __("{0}% of the {1} target", [flt((flt(value) * 100) / flt(target), 1), format(target)]) : __("No targets set");
		this.$body.find(".ss-cards").html(
			card(__("Total sales"), this.money(t.total), "income", "ss-card-hero",
				`${__("Net {0}", [this.money(t.net_total)])} · ${of_target(t[basis], t.sales_target, (v) => this.money(v))}`
				+ (flt(t.services) ? ` · ${this.result.filters.exclude_services
					? __("{0} services left out", [this.money(t.services)]) : __("incl. {0} services", [this.money(t.services)])}` : "")) +
				card(__("Commission"), this.money(t.commission[basis].amount), "star", "ss-card-green",
					__("on {0}", [this.basis_labels[basis]])) +
				card(__("Small items"), t.small_units, "tag", "ss-card-orange",
					of_target(t.small_units, t.small_items_target, (v) => v)) +
				card(__("Invoices"), t.invoices, "file", "", t.returns ? __("{0} returns", [t.returns]) : "") +
				card(__("Delivery orders"), t.orders, "assets") +
				card(__("AC units"), t.ac_units, "snowflake", "ss-card-blue") +
				card(__("Other units"), t.non_ac_units, "package", "ss-card-purple")
		);

		this.$body.find(".ss-range").html(flt(range.max)
			? __("Commission from {0} to {1} by small items sold against the target. Click a salesman to see how it was calculated and their invoices.",
				[`<b>${this.pct(range.min)}</b>`, `<b>${this.pct(range.max)}</b>`])
			: `<span class="ss-warn">${frappe.utils.icon("es-line-alert-circle", "xs")} ${__("No commission range yet: set Min and Max Commission % in {0}.",
				[`<a href="/desk/delivery-settings" target="_blank">${__("Delivery Settings")}</a>`])}</span>`);

		this.$body.find(".ss-table-panel").removeClass("hidden");
		const $t = this.$body.find(".ss-table").empty();
		this.render_breakdown();
		if (!rows.length) {
			$t.html(`<div class="ss-empty">
				${frappe.utils.icon("search", "lg")}
				<b>${__("No paid invoices in this period")}</b>
				<span>${__("Try a longer period, another company, or check that the invoices are paid.")}</span>
			</div>`);
			return;
		}
		const esc = frappe.utils.escape_html;
		const head = [
			[__("Salesman"), ""], [__("Sales"), ""], [__("Small items"), ""], [__("Invoices"), "r"],
			[__("AC units"), "r"], [__("Other units"), "r"], [__("Commission %"), "r"], [__("Evaluation"), "r"],
			[__("Commission"), "r"],
		];
		const $table = $(`<div class="ss-scroll"><table class="ss-grid">
			<thead><tr>${head.map(([h, a]) => `<th class="${a ? "text-right" : ""}">${h}</th>`).join("")}</tr></thead>
			<tbody></tbody><tfoot></tfoot></table></div>`).appendTo($t);

		rows.forEach((r) => {
			const c = r.commission[basis];
			const extra = [r.orders ? __("{0} orders", [r.orders]) : "", r.returns ? __("{0} returns", [r.returns]) : ""]
				.filter(Boolean).join(" · ");
			$(`<tr class="${this.invoice_user === r.user ? "ss-row-active" : ""}">
				<td>
					<button class="ss-user" type="button" title="${__("Show how the commission was calculated and the invoices")}">
						${frappe.avatar(r.user, "avatar-small")}
						<span><b>${esc(r.full_name)}</b><small>${esc(r.user)}</small></span>
					</button>
				</td>
				<td><div class="ss-amount">${this.money(r[basis])}</div>${this.progress(r[basis], r.sales_target, (v) => this.money(v))}</td>
				<td><div class="ss-amount">${r.small_units}</div>${this.progress(r.small_units, r.small_items_target, (v) => v)}</td>
				<td class="text-right">${r.invoices}${extra ? `<small class="ss-sub">${extra}</small>` : ""}</td>
				<td class="text-right">${r.ac_units}</td>
				<td class="text-right">${r.non_ac_units}</td>
				<td class="text-right"><b>${this.pct(r.commission_percent)}</b></td>
				<td class="text-right">${this.evaluation_cell(r)}</td>
				<td class="text-right"><div class="ss-commission">${this.money(c.amount)}</div>
					<small class="ss-sub" title="${__("Sales part (75%) + evaluation part (25%)")}">${this.money(c.items)} + ${this.money(c.evaluation)}</small></td>
			</tr>`)
				.appendTo($table.find("tbody"))
				.on("click", ".ss-user", () => {
					this.invoice_user = r.user;
					this.invoice_page = 0;
					this.render();
					this.load_invoices();
					this.$body.find(".ss-breakdown-panel")[0].scrollIntoView({ behavior: "smooth", block: "start" });
				})
				.on("click", ".ss-evaluate", () => this.evaluate(r));
		});
		$table.find("tfoot").html(`<tr>
			<th>${__("Total")}</th>
			<th>${this.money(t[basis])}</th>
			<th>${t.small_units}</th>
			<th class="text-right">${t.invoices}</th>
			<th class="text-right">${t.ac_units}</th>
			<th class="text-right">${t.non_ac_units}</th>
			<th></th><th></th>
			<th class="text-right">${this.money(t.commission[basis].amount)}</th>
		</tr>`);
	}

	// How the clicked salesman's commission was calculated, step by step with their own numbers.
	render_breakdown() {
		const $b = this.$body.find(".ss-breakdown-panel");
		const r = this.invoice_user && this.result.rows.find((row) => row.user === this.invoice_user);
		if (!r) {
			$b.addClass("hidden").empty();
			return;
		}
		const esc = frappe.utils.escape_html;
		const basis = this.basis_key();
		const range = this.result.commission_range;
		const f = this.result.filters;
		const c = r.commission[basis];
		const sales = this.money(r[basis]);
		const reached = r.small_items_target ? Math.min(Math.max(flt(r.small_units), 0) / r.small_items_target, 1) : 1;
		const ev = r.evaluation;
		const step = (n, title, formula, result, cls = "") => `<li class="ss-step ${cls}">
			<span class="ss-step-n">${n}</span>
			<div class="ss-step-text"><b>${title}</b><span class="ss-formula">${formula}</span></div>
			<span class="ss-step-result">${result}</span></li>`;
		$b.removeClass("hidden").html(`
			<header class="ss-panel-head">
				<div><h4>${__("Commission of {0}", [esc(r.full_name)])}</h4>
					<p>${__("On {0}, from {1} to {2}.", [this.basis_labels[basis], frappe.datetime.str_to_user(f.from_date),
						frappe.datetime.str_to_user(f.to_date)])}</p></div>
				<button class="btn btn-default btn-sm ss-close">${frappe.utils.icon("close", "xs")} ${__("Close")}</button>
			</header>
			<ol class="ss-steps">
				${step(1, __("Small items target reached"), r.small_items_target
					? __("{0} sold of a target of {1}", [r.small_units, r.small_items_target]) + (reached >= 1 ? ` · ${__("at most 100%")}` : "")
					: __("No Small Items Target on the user, so the Max % applies"), this.pct(reached * 100))}
				${step(2, __("Commission %"), __("{0} + {1} × ({2} − {0})", [this.pct(range.min), this.pct(reached * 100), this.pct(range.max)]),
					this.pct(r.commission_percent))}
				${step(3, __("Sales part (75%)"), __("{0} × {1} × 75%", [sales, this.pct(r.commission_percent)]), this.money(c.items))}
				${step(4, __("Evaluation part (25%)"), ev
					? __("{0} × {1} × 25% × score {2}", [sales, this.pct(r.commission_percent), this.pct(ev.score)]) +
						` · <a href="${frappe.utils.get_form_link("Sales Evaluation", ev.name)}" target="_blank">${esc(ev.name)}</a>` +
						(ev.overlaps ? "" : ` · ${__("none in this period: the latest one ({0} to {1}) is used", [
							frappe.datetime.str_to_user(ev.from_date), frappe.datetime.str_to_user(ev.to_date)])}`)
					: __("This salesman has no Sales Evaluation, so this part is 0."), this.money(c.evaluation), ev ? "" : "ss-step-missing")}
				${step("=", __("Commission"), __("Sales part + evaluation part"), this.money(c.amount), "ss-step-total")}
			</ol>`);
		$b.find(".ss-close").on("click", () => {
			this.invoice_user = null;
			this.invoice_page = 0;
			this.render();
			this.load_invoices();
		});
		if (!ev && this.setup.can_evaluate) {
			$(`<button type="button" class="btn btn-primary btn-xs ss-evaluate">${__("Evaluate {0}", [esc(r.full_name)])}</button>`)
				.appendTo($b.find(".ss-step-missing .ss-step-text")).on("click", () => this.evaluate(r));
		}
	}

	// The counted invoices of the shown run's filters (one salesman when a row is clicked).
	invoice_args(extra = {}) {
		const f = this.result.filters;
		return { from_date: f.from_date, to_date: f.to_date, company: f.company, exclude_services: f.exclude_services,
			pos_profile: f.pos_profile,
			user: this.invoice_user || f.user, search: this.invoice_search, ...extra };
	}

	load_invoices() {
		if (!this.result) return;
		const ask = (this.ask = (this.ask || 0) + 1);
		this.$body.find(".ss-invoice-panel").addClass("ss-is-loading");
		frappe.xcall(SS_API + "get_invoices", this.invoice_args({ page: this.invoice_page })).then((r) => {
			if (ask !== this.ask) return; // an older answer
			this.inv = r;
			this.render_invoices();
		});
	}

	render_invoices() {
		const $w = this.$body.find(".ss-invoice-panel").empty().removeClass("hidden ss-is-loading");
		if (!this.result || !this.inv) return;
		const esc = frappe.utils.escape_html;
		const names = Object.fromEntries(this.result.rows.map((r) => [r.user, r.full_name]));
		const shown = this.inv.invoices;
		const per_page = this.inv.page_len;
		const pages = Math.max(1, Math.ceil(this.inv.count / per_page));
		const q = (this.invoice_search || "").trim();
		const who = this.invoice_user ? names[this.invoice_user] || this.invoice_user : null;

		const $head = $(`<header class="ss-panel-head">
			<div>
				<h4>${__("Invoices counted")} <span class="ss-count">${this.inv.count}</span></h4>
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
			this.load_invoices();
		});
		$head.on("input", ".ss-inv-search", frappe.utils.debounce((e) => {
			this.invoice_search = e.target.value;
			this.invoice_page = 0;
			this.refocus = true;
			this.load_invoices();
		}, 300));
		if (this.refocus) {
			// The panel was redrawn while typing: keep the cursor in the search box.
			this.refocus = false;
			const input = $head.find(".ss-inv-search")[0];
			input.focus();
			input.setSelectionRange(input.value.length, input.value.length);
		}

		if (!shown.length) {
			$w.append(`<div class="ss-empty ss-empty-sm"><span>${q ? __("No invoice matches your search.") : __("No invoices.")}</span></div>`);
			return;
		}
		const type_class = { Order: "blue", Invoice: "gray", Return: "red" };
		const $table = $(`<div class="ss-scroll"><table class="ss-grid ss-grid-compact"><thead><tr>
			<th>${__("Invoice No.")}</th><th>${__("Created")}</th><th>${__("Posting date")}</th><th>${__("Salesman")}</th>
			<th>${__("Customer")}</th><th>${__("Type")}</th><th>${__("Status")}</th>
			<th class="text-right">${__("Total")}</th><th class="text-right">${__("Net total")}</th>
			<th class="text-right" title="${this.result.filters.exclude_services ? __("Left out of the Total") : __("Included in the Total")}">${__("Services")}</th>
			<th class="text-right">${__("AC")}</th><th class="text-right">${__("Other")}</th><th class="text-right">${__("Small")}</th>
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
				<td class="text-right text-muted">${flt(inv.services) ? this.money(inv.services) : ""}</td>
				<td class="text-right">${inv.ac_units}</td>
				<td class="text-right">${inv.non_ac_units}</td>
				<td class="text-right">${inv.small_units}</td></tr>`);
		});
		if (pages > 1) {
			const $nav = $(`<div class="ss-inv-nav">
				<span class="text-muted">${__("{0}–{1} of {2}", [this.invoice_page * per_page + 1, this.invoice_page * per_page + shown.length, this.inv.count])}</span>
				<div class="btn-group">
					<button class="btn btn-default btn-sm ss-prev" ${this.invoice_page ? "" : "disabled"}>‹ ${__("Previous")}</button>
					<button class="btn btn-default btn-sm ss-next" ${this.invoice_page < pages - 1 ? "" : "disabled"}>${__("Next")} ›</button>
				</div>
			</div>`).appendTo($w);
			$nav.on("click", ".ss-prev", () => { this.invoice_page--; this.load_invoices(); });
			$nav.on("click", ".ss-next", () => { this.invoice_page++; this.load_invoices(); });
		}
	}

	async export_csv() {
		if (!this.result) {
			frappe.show_alert({ message: __("Click Show first."), indicator: "orange" });
			return;
		}
		const all = await frappe.xcall(SS_API + "get_invoices",
			this.invoice_args({ user: this.result.filters.user, search: "", all_pages: 1 }));
		const basis = this.basis_key();
		const services = this.result.filters.exclude_services ? __("Services (left out)") : __("Services (included)");
		const rows = [[__("Salesman"), __("User"), __("Invoices"), __("Delivery Orders"), __("Returns"), __("Total"),
			__("Net Total"), services, __("Sales Target"), __("AC Units"), __("Non-AC Units"), __("Small Items"), __("Small Items Target"),
			__("Commission %"), __("Evaluation"), __("Evaluation Score %"), __("Commission On"), __("Sales Part (75%)"),
			__("Evaluation Part (25%)"), __("Commission")]];
		this.result.rows.forEach((r) => {
			const c = r.commission[basis];
			rows.push([r.full_name, r.user, r.invoices, r.orders, r.returns, r.total, r.net_total, r.services, r.sales_target, r.ac_units,
				r.non_ac_units, r.small_units, r.small_items_target, r.commission_percent, r.evaluation ? r.evaluation.name : "",
				r.evaluation ? r.evaluation.score : "", this.basis_labels[basis], c.items, c.evaluation, c.amount]);
		});
		rows.push([]);
		rows.push([__("Invoice No."), __("Created"), __("Posting Date"), __("User"), __("Customer"), __("Type"),
			__("Status"), __("Total"), __("Net Total"), services, __("AC Units"), __("Non-AC Units"), __("Small Items")]);
		all.invoices.forEach((i) =>
			rows.push([i.name, i.created, i.posting_date, i.user, i.customer_name, i.type, i.status, i.total, i.net_total, i.services,
				i.ac_units, i.non_ac_units, i.small_units])
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
