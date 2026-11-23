// Spare Parts: approvals (System Manager), the workshop's Spare Parts Store (qty, held, free)
// and the history. Rules: customer_support/spare_parts.py. Shared UI: spare_parts.bundle.js.

frappe.pages["spare-parts"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Spare Parts"), single_column: true });
	wrapper.dm_page = new SparePartsPage(page);
};

frappe.pages["spare-parts"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.load();
};

class SparePartsPage {
	constructor(page) {
		this.D = window.dm_drivers;
		this.S = window.dm_spare;
		this.page = page;
		this.approver = frappe.user.has_role("System Manager");
		const tabs = [
			...(this.approver ? [{ key: "approvals", label: __("Approvals"), icon: "check" }] : []),
			{ key: "store", label: __("Store"), icon: "package" },
			{ key: "history", label: __("History"), icon: "list" },
		];
		const saved = this.D.prefs("spare_parts").tab;
		this.tab = tabs.some((t) => t.key === saved) ? saved : tabs[0].key;
		this.f = { search: "", status: "" };
		this.$root = $(`<div class="dd-page">
			${this.D.tabs(tabs, this.tab)}
			<div class="dd-panel dd-filter-panel"></div>
			<div class="dd-panel dd-table-panel">
				<div class="dd-scroll"><table class="dd-table dd-cards"><thead></thead><tbody></tbody></table></div>
			</div>
		</div>`).appendTo(page.main);
		this.$root.on("click", ".dd-tab-btn", (e) => {
			this.tab = $(e.currentTarget).data("tab");
			this.$root.find(".dd-tab-btn").removeClass("active");
			$(e.currentTarget).addClass("active");
			this.D.prefs("spare_parts", { tab: this.tab });
			this.renderFilters();
			this.load();
		});
		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		if (this.approver) {
			page.add_menu_item(__("Add parts to the store"), () => this.newEntry("Stock Entry"));
			page.add_menu_item(__("Count the store"), () => this.newEntry("Stock Reconciliation"));
		}
		page.add_menu_item(__("Maintenance Issues"), () => frappe.set_route("maintenance-issues"));
		this.renderFilters();
	}

	newEntry(doctype) {
		const wh = this.warehouse;
		frappe.new_doc(doctype, doctype === "Stock Entry"
			? { stock_entry_type: "Material Receipt", to_warehouse: wh } : { set_warehouse: wh, purpose: "Stock Reconciliation" });
	}

	renderFilters() {
		const hint = {
			approvals: __("Parts the workshop asked for. Approve (click the row to lower the qty) or reject. Asked parts are held until taken, rejected or cancelled."),
			store: __("Free = in the store minus what is held for open requests (waiting or approved). Nothing leaves this store without an approved request."),
			history: this.approver ? __("Every request, newest first.") : __("Your requests, newest first."),
		}[this.tab];
		const statuses = ["", "Pending", "Approved", "Taken", "Returned", "Rejected", "Cancelled"];
		const $p = this.$root.find(".dd-filter-panel").html(`
			<div class="dd-filters">
				${this.tab === "history" ? `<div class="dd-field"><label>${__("Status")}</label>
					<select class="form-control" data-f="status">${this.D.select(statuses.map((s) => ({ value: s, label: s ? __(s) : __("All") })), this.f.status)}</select></div>` : ""}
				${this.tab !== "approvals" ? `<div class="dd-field dd-search"><label>${__("Search")}</label>
					<span class="dd-search-icon">${frappe.utils.icon("search", "sm")}</span>
					<input class="form-control" data-f="search" placeholder="${this.tab === "store" ? __("Part name or code") : __("Request, ticket, technician")}"
						value="${this.D.esc(this.f.search)}"></div>` : ""}
			</div>
			<div class="dm-sp-month"></div>
			<div class="dd-filter-foot"><span class="dd-hint">${hint}</span><span class="dd-count" data-count-label>–</span></div>`);
		const reload = frappe.utils.debounce(() => this.load(), 300);
		$p.find("[data-f]").on("input change", (e) => {
			this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
			reload();
		});
	}

	async load() {
		const seq = (this.seq = (this.seq || 0) + 1);
		const fresh = () => seq === this.seq;
		if (this.tab === "approvals") return this.loadApprovals(fresh);
		if (this.tab === "store") return this.loadStore(fresh);
		return this.loadHistory(fresh);
	}

	head(cols) {
		this.$root.find(".dd-table thead").html(`<tr>${cols.map((c) => `<th${c[1] ? ` class="${c[1]}"` : ""}>${c[0]}</th>`).join("")}</tr>`);
		return this.$root.find(".dd-table tbody").html(this.D.skeleton(cols.length));
	}

	partsCell(r) {
		return r.items.map((i) => `<div>${this.D.esc(i.item_name || i.item_code)} <b>× ${this.S.num(i.qty)}</b>${"free" in i && +i.free < +i.qty
			? ` <span class="dd-no small">(${__("{0} free", [this.S.num(i.free)])})</span>` : ""}</div>`).join("");
	}

	ticketCell(r) {
		const info = r.ticket_info || {};
		return `<a class="dd-dn" data-ticket="${this.D.esc(r.ticket)}">${this.D.esc(r.ticket)}</a>
			<span class="dd-sub">${this.D.esc(info.subject || "")}${info.customer_name ? " · " + this.D.esc(info.customer_name) : ""}</span>`;
	}

	bindTickets($tbody) {
		$tbody.find("[data-ticket]").on("click", (e) => {
			e.stopPropagation();
			window.dm_support.openTicket($(e.currentTarget).data("ticket"), () => this.load(), "maintenance");
		});
	}

	async loadApprovals(fresh) {
		const D = this.D, S = this.S;
		const $tbody = this.head([[__("Request")], [__("Ticket")], [__("Technician")], [__("Parts")], [__("Value"), "text-right"], [""]]);
		const rows = await S.call("get_pending");
		if (!fresh()) return;
		this.$root.find("[data-count-label]").text(__("{0} waiting", [rows.length]));
		this.$root.find('[data-count="approvals"]').text(rows.length);
		if (!rows.length) return $tbody.html(D.emptyRow(6, __("All done"), __("No spare parts request is waiting.")));
		$tbody.html(rows.map((r, i) => `
			<tr data-i="${i}" tabindex="0">
				<td data-label="${__("Request")}"><b>${D.esc(r.name)}</b><span class="dd-sub">${S.when(r.requested_on)}</span></td>
				<td data-label="${__("Ticket")}">${this.ticketCell(r)}</td>
				<td data-label="${__("Technician")}">${D.esc(r.technician_name || r.technician)}${r.reason
					? `<span class="dd-sub">${D.esc(r.reason)}</span>` : ""}</td>
				<td data-label="${__("Parts")}">${this.partsCell(r)}</td>
				<td data-label="${__("Value")}" class="text-right">${S.money(r.estimate)}</td>
				<td data-label=""><div class="dd-btn-row" style="justify-content:flex-end">
					<button class="btn btn-xs btn-default" data-do="reject">${__("Reject")}</button>
					<button class="btn btn-xs btn-success" data-do="approve">${frappe.utils.icon("check", "xs")} ${__("Approve")}</button></div></td>
			</tr>`).join(""));
		this.bindTickets($tbody);
		$tbody.find("tr[data-i]").on("click", (e) => {
			const r = rows[$(e.currentTarget).data("i")];
			const act = $(e.target).closest("[data-do]").data("do");
			const done = (msg) => { frappe.show_alert({ message: msg, indicator: "green" }); this.load(); };
			if (act === "approve") {
				const short = r.items.some((x) => +x.free < +x.qty);
				if (short) return S.decide(r, () => done(__("Approved")));  // lower the qty first
				const list = r.items.map((x) => `${D.esc(x.item_name || x.item_code)} × ${S.num(x.qty)}`).join("<br>");
				return S.sure(__("Approve {0} for {1}?<br><br>{2}", [r.name, D.esc(r.technician_name || r.technician), list]))
					.then((yes) => yes && S.call("approve", { request: r.name }, true).then(() => done(__("{0} approved", [r.name]))));
			}
			if (act === "reject") {
				const d = new frappe.ui.Dialog({
					title: __("Reject {0}", [r.name]),
					fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Why"), reqd: 1 }],
					primary_action_label: __("Reject"),
					primary_action: async (v) => {
						if (!(await S.sure(__("Reject {0}? Its parts are freed for other repairs.", [r.name])))) return;
						await S.call("reject", { request: r.name, reason: v.reason }, true);
						d.hide();
						done(__("{0} rejected", [r.name]));
					},
				});
				return d.show();
			}
			S.decide(r, () => done(__("Done")));
		});
	}

	async loadStore(fresh) {
		const D = this.D, S = this.S;
		const $tbody = this.head([[__("Part")], [__("In store"), "text-right"], [__("Held"), "text-right"], [__("Free"), "text-right"], [__("Value"), "text-right"]]);
		const r = await S.call("get_store", { search: this.f.search });
		if (!fresh()) return;
		this.warehouse = r.warehouse;
		const value = r.rows.reduce((s, x) => s + (+x.stock_value || 0), 0);
		this.$root.find("[data-count-label]").text(`${D.esc(r.warehouse)} · ${__("{0} parts", [r.rows.length])} · ${S.money(value)}`);
		this.$root.find('[data-count="store"]').text(r.rows.length);
		if (!r.rows.length) return $tbody.html(D.emptyRow(5, __("Empty"), __("No part in the Spare Parts Store.")));
		$tbody.html(r.rows.map((x) => `
			<tr>
				<td data-label="${__("Part")}"><b>${D.esc(x.item_name)}</b><span class="dd-sub">${D.esc(x.item_code)}${+x.has_serial_no ? " · " + __("serial") : ""}</span></td>
				<td data-label="${__("In store")}" class="text-right">${S.num(x.in_store)} <span class="dd-muted small">${D.esc(x.stock_uom || "")}</span></td>
				<td data-label="${__("Held")}" class="text-right">${+x.held ? `<span class="dd-pill dd-pill-blue">${S.num(x.held)}</span>` : "—"}</td>
				<td data-label="${__("Free")}" class="text-right"><b class="${+x.free > 0 ? "dd-ok" : "dd-no"}">${S.num(x.free)}</b></td>
				<td data-label="${__("Value")}" class="text-right">${S.money(x.stock_value)}</td>
			</tr>`).join(""));
	}

	async loadHistory(fresh) {
		const D = this.D, S = this.S;
		const $tbody = this.head([[__("Request")], [__("Ticket")], [__("Technician")], [__("Parts")], [__("Status")], [__("Cost"), "text-right"]]);
		const r = await S.call("get_history", { search: this.f.search, status: this.f.status });
		if (!fresh()) return;
		this.$root.find(".dm-sp-month").html(r.month.length ? `<div class="dd-muted small" style="align-self:center">${__("This month")}:</div>`
			+ r.month.map((m) => `<div>${D.esc(m.technician_name || m.technician)} · <b>${S.money(m.cost)}</b>
				<span class="dd-muted">(${__("{0} requests", [m.requests])})</span></div>`).join("") : "");
		this.$root.find("[data-count-label]").text(__("{0} requests", [r.requests.length]));
		this.$root.find('[data-count="history"]').text(r.requests.length);
		if (!r.requests.length) return $tbody.html(D.emptyRow(6, __("Nothing here"), __("No spare parts request found.")));
		$tbody.html(r.requests.map((x) => `
			<tr>
				<td data-label="${__("Request")}"><a href="/desk/spare-part-request/${encodeURIComponent(x.name)}"><b>${D.esc(x.name)}</b></a>
					<span class="dd-sub">${S.when(x.requested_on)}</span></td>
				<td data-label="${__("Ticket")}">${this.ticketCell(x)}</td>
				<td data-label="${__("Technician")}">${D.esc(x.technician_name || x.technician)}</td>
				<td data-label="${__("Parts")}">${S.itemLines({ ...x, items: x.items })}</td>
				<td data-label="${__("Status")}">${S.statusPill(x.status)}${x.reject_reason ? `<span class="dd-sub">${D.esc(x.reject_reason)}</span>` : ""}</td>
				<td data-label="${__("Cost")}" class="text-right">${+x.total_cost ? S.money(x.total_cost) : "—"}</td>
			</tr>`).join(""));
		this.bindTickets(this.$root.find(".dd-table tbody"));
	}
}
