// Order Control: what happened to each order, from the office to the customer. The list shows
// the orders with activity in the filters; the order shows where it is now (money, goods) and
// its logistic and money steps (Order Log) in time order.

frappe.pages["order-control"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Order Control"), single_column: true });
	wrapper.dm_page = new OrderControlPage(page);
};

frappe.pages["order-control"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.loadList();
};

const OC_API = "delivery_management.api.control.";

// Event → [icon, tone]: blue logistics, green money in / done, orange needs attention, red cancelled.
const OC_EVENTS = {
	"Order created": ["file", "gray"],
	"Store sale": ["file", "gray"],
	"Driver assigned": ["users", "blue"],
	"Driver changed": ["users", "orange"],
	"Driver removed": ["users", "orange"],
	"Sent out": ["truck", "blue"],
	"Delivered": ["check", "green"],
	"Not delivered items": ["undo", "orange"],
	"Customer not available": ["calendar", "orange"],
	"Received back": ["package", "blue"],
	"POS return": ["undo", "orange"],
	"Payment": ["money-coins-1", "green"],
	"Refund": ["money-coins-1", "orange"],
	"Credit note": ["file", "orange"],
	"Payment cancelled": ["close", "red"],
	"Invoice cancelled": ["close", "red"],
	"Order cancelled": ["close", "red"],
	"Cash handed over": ["money-coins-1", "green"],
	"Delivery split": ["calendar", "blue"],
	"Delivery merged back": ["calendar", "gray"],
	// Support tickets
	"Ticket opened": ["support", "gray"],
	"Visit booked": ["calendar", "blue"],
	"Return booked": ["calendar", "blue"],
	"Ticket driver assigned": ["users", "blue"],
	"Ticket driver changed": ["users", "orange"],
	"Visit logged": ["tool", "blue"],
	"Resolved at the customer": ["check", "green"],
	"Customer not home": ["calendar", "orange"],
	"Item picked up": ["package", "blue"],
	"Received in workshop": ["package", "blue"],
	"Repaired": ["check", "green"],
	"Not repairable": ["close", "orange"],
	"Sent out from workshop": ["truck", "blue"],
	"Sent out from store": ["truck", "blue"],
	"Received back in store": ["arrow-down-left", "orange"],
	"Parts requested": ["tool", "orange"],
	"Parts approved": ["check", "blue"],
	"Parts rejected": ["close", "red"],
	"Parts request cancelled": ["close", "gray"],
	"Parts taken": ["package", "green"],
	"Parts returned": ["arrow-down-left", "orange"],
	"Returned to customer": ["check", "green"],
	"Ticket resolved": ["check", "green"],
};

// The events of the Support tickets tab: every event from "Ticket opened" on.
const OC_TICKET_EVENTS = new Set(Object.keys(OC_EVENTS).slice(Object.keys(OC_EVENTS).indexOf("Ticket opened")));

class OrderControlPage {
	constructor(page) {
		this.D = window.dm_drivers;
		this.page = page;
		this.f = { search: "", kind: "", event: "", user: "", period: "7" };
		this.current = null;
		const saved = this.D.prefs("order_control").tab;
		this.tab = saved === "tickets" ? "tickets" : "orders";
		this.$root = $(`<div class="dd-page oc-page">
			${this.D.tabs([
				{ key: "orders", label: __("Orders"), icon: "file" },
				{ key: "tickets", label: __("Support tickets"), icon: "support" },
			], this.tab)}
			<div class="dd-panel dd-filter-panel"></div>
			<div class="oc-split">
				<div class="dd-panel oc-list" aria-label="${__("Orders")}"></div>
				<div class="dd-panel oc-detail"></div>
			</div>
		</div>`).appendTo(page.main);
		page.set_secondary_action(__("Refresh"), () => this.refresh(), "refresh");
		this.$root.on("click", "[data-order]", (e) => this.open(e.currentTarget.dataset.order));
		this.$root.on("click", "[data-back]", () => this.$root.removeClass("oc-show-detail"));
		this.$root.on("click", ".dd-tab-btn", (e) => {
			const tab = e.currentTarget.dataset.tab;
			if (tab === this.tab) return;
			this.tab = tab;
			this.D.prefs("order_control", { tab });
			this.$root.find(".dd-tab-btn").removeClass("active");
			$(e.currentTarget).addClass("active");
			// Each tab has its own steps and types: start its filters clean.
			Object.assign(this.f, { search: "", kind: "", event: "" });
			this.current = null;
			this.$root.removeClass("oc-show-detail");
			this.renderEmptyDetail();
			this.renderFilters().then(() => this.loadList());
		});
		this.renderFilters();
		this.renderEmptyDetail();
	}

	refresh() {
		this.loadList();
		this.current && this.open(this.current);
	}

	async renderFilters() {
		const D = this.D;
		const users = this.users || (this.users = await frappe.xcall(OC_API + "get_users"));
		const tickets = this.tab === "tickets";
		const events = Object.keys(OC_EVENTS).filter((e) => OC_TICKET_EVENTS.has(e) === tickets);
		const periods = [["1", __("Today")], ["7", __("7 days")], ["30", __("30 days")], ["90", __("90 days")]];
		const $p = this.$root.find(".dd-filter-panel").html(`
			<div class="dd-filters">
				<div class="dd-field dd-search"><label for="oc-q">${tickets ? __("Find a ticket") : __("Find an order")}</label>
					<span class="dd-search-icon">${frappe.utils.icon("search", "sm")}</span>
					<input id="oc-q" class="form-control" data-f="search" placeholder="${tickets
						? __("Ticket, customer or phone") : __("Order, invoice, delivery note, customer or phone")}"></div>
				${tickets ? "" : `<div class="dd-field"><label>${__("Type")}</label>
					<select class="form-control" data-f="kind">${D.select([{ value: "", label: __("Orders and store sales") },
						{ value: "order", label: __("Delivery orders") }, { value: "sale", label: __("Store sales") }], "")}</select></div>`}
				<div class="dd-field"><label>${__("Step")}</label>
					<select class="form-control" data-f="event">${D.select(
						[{ value: "", label: __("All steps") }, ...events.map((e) => ({ value: e, label: __(e) }))], "")}</select></div>
				<div class="dd-field"><label>${__("Done by")}</label>
					<select class="form-control" data-f="user">${D.select([{ value: "", label: __("Anyone") }, ...users], "")}</select></div>
			</div>
			<div class="dd-filter-foot">
				<div class="oc-periods" role="group" aria-label="${__("Period")}">${periods.map(([v, l]) =>
					`<button type="button" class="oc-chip ${v === this.f.period ? "on" : ""}" data-period="${v}">${l}</button>`).join("")}</div>
				<div class="oc-range" role="group" aria-label="${__("Dates")}">
					<label>${__("From")} <input type="date" class="form-control input-xs" data-range="from_date"></label>
					<label>${__("To")} <input type="date" class="form-control input-xs" data-range="to_date"></label></div>
				<span class="dd-count" data-count-label>–</span></div>`);
		const reload = frappe.utils.debounce(() => this.loadList(), 300);
		$p.find("[data-f]").on("input change", (e) => {
			this.f[e.currentTarget.dataset.f] = $(e.currentTarget).val();
			reload();
		});
		$p.find("[data-period]").on("click", (e) => {
			this.f.period = e.currentTarget.dataset.period;
			this.f.from_date = this.f.to_date = "";
			$p.find("[data-range]").val("");
			$p.find("[data-period]").removeClass("on");
			$(e.currentTarget).addClass("on");
			this.loadList();
		});
		// Any dates: they replace the period buttons.
		$p.find("[data-range]").on("change", (e) => {
			this.f[e.currentTarget.dataset.range] = $(e.currentTarget).val();
			this.f.period = "";
			$p.find("[data-period]").removeClass("on");
			if (!this.f.from_date && !this.f.to_date) $p.find('[data-period="7"]').trigger("click");
			else this.loadList();
		});
	}

	async loadList() {
		const D = this.D;
		const $list = this.$root.find(".oc-list").html(`<div class="oc-loading">${__("Loading…")}</div>`);
		const { period, ...f } = this.f;
		f.kind = this.tab === "tickets" ? "ticket" : f.kind || "orders";
		if (period) f.from_date = frappe.datetime.add_days(frappe.datetime.get_today(), 1 - Number(period));
		if (!f.from_date) delete f.from_date;
		if (!f.to_date) delete f.to_date;
		const rows = await frappe.xcall(OC_API + "get_orders", f);
		const tickets = this.tab === "tickets";
		this.$root.find("[data-count-label]").text(tickets ? __("{0} tickets", [rows.length]) : __("{0} orders", [rows.length]));
		this.$root.find(`[data-count="${this.tab}"]`).text(rows.length);
		if (!rows.length) {
			$list.html(`<div class="oc-empty">${frappe.utils.icon("search", "lg")}<b>${tickets ? __("No tickets found") : __("No orders found")}</b>
				<span>${__("Try another period (or older dates in From / To) or clear the filters.")}</span></div>`);
			return;
		}
		$list.html(rows.map((r) => {
			const [icon, tone] = OC_EVENTS[r.last_event] || ["dot", "gray"];
			return `<button type="button" class="oc-row ${r.name === this.current ? "on" : ""}" data-order="${D.esc(r.name)}">
				<span class="oc-row-top"><b>${D.esc(r.name)}</b><span class="oc-ago">${frappe.datetime.comment_when(r.last_at, true)}</span></span>
				<span class="oc-row-name">${D.esc(r.customer_name || "")}</span>
				<span class="oc-row-last oc-${tone}">${frappe.utils.icon(icon, "xs")} ${__(r.last_event || "")}
					<span class="dd-muted">· ${__("{0} steps", [r.steps])}</span></span>
			</button>`;
		}).join(""));
	}

	renderEmptyDetail() {
		const tickets = this.tab === "tickets";
		this.$root.find(".oc-detail").html(`<div class="oc-empty">${frappe.utils.icon("list", "lg")}
			<b>${tickets ? __("Choose a ticket") : __("Choose an order")}</b><span>${tickets
				? __("See where the customer's item is now and every step: visits, workshop, spare parts and the return.")
				: __("See where it is now and every step: who assigned the driver, who sent the goods out, what came back and every payment.")}</span></div>`);
	}

	async open(name) {
		const D = this.D;
		this.current = name;
		this.$root.find(".oc-row").removeClass("on").filter(`[data-order="${CSS.escape(name)}"]`).addClass("on");
		this.$root.addClass("oc-show-detail");
		const $d = this.$root.find(".oc-detail").html(`<div class="oc-loading">${__("Loading…")}</div>`);
		const o = await frappe.xcall(OC_API + "get_order", { name });
		const money = (v) => format_currency(v, o.currency);
		const link = (dt, dn) => dn ? `<a href="/desk/${frappe.router.slug(dt)}/${encodeURIComponent(dn)}">${D.esc(dn)}</a>` : "";
		const tile = (label, value, tone = "") => `<div class="oc-tile ${tone}"><span>${label}</span><b>${value}</b></div>`;
		if (o.kind === "ticket") return this.renderTicket($d, o, link);
		const notes = o.notes.map((n) => `<div class="oc-note">
			<div>${link("Delivery Note", n.name)} ${D.typePill(n.order_type)}</div>
			<div class="oc-where">${D.esc(n.where)}</div>
			<div class="dd-muted small">${D.esc(n.driver_name || __("No driver yet"))}${n.work_date
				? ` · ${D.date(n.work_date)}${n.slot ? " " + n.slot : ""}` : ""}</div></div>`).join("");
		$d.html(`
			<button type="button" class="btn btn-default btn-sm oc-back" data-back>${frappe.utils.icon("left", "xs")} ${__("Orders")}</button>
			<div class="oc-head">
				<div><h4>${link(o.kind === "order" ? "Sales Order" : "Sales Invoice", o.name)}</h4>
					<div class="dd-muted">${o.kind === "order" ? __("Delivery order") : __("Branch sale")} · ${__("made {0} by {1}",
						[frappe.datetime.str_to_user(o.created), D.esc(o.created_by)])}</div></div>
				<span class="dd-pill dd-pill-gray">${__(o.status)}</span>
			</div>
			<div class="oc-customer"><b>${D.esc(o.customer_name)}</b>${o.mobile ? ` · ${D.phoneLink(o.mobile)}` : ""}
				${o.address ? `<div class="dd-muted">${D.esc(o.address)}</div>` : ""}</div>
			<div class="oc-tiles">
				${tile(__("Total"), money(o.billed || o.total))}
				${tile(__("Paid"), money(o.paid), "oc-green")}
				${tile(__("Still due"), money(o.due), o.due > 0 ? "oc-red" : "")}
				${tile(__("Credited"), money(o.credited), o.credited > 0 ? "oc-orange" : "")}
			</div>
			${notes ? `<h5 class="oc-h">${__("Where the goods are")}</h5><div class="oc-notes">${notes}</div>` : ""}
			<h5 class="oc-h">${__("What happened")}</h5>
			${o.steps.length ? this.timeline(o.steps, money, link) : `<div class="dd-muted">${__("No steps logged yet.")}</div>`}`);
	}

	/** A support ticket: where the customer's item is now, and every step. */
	renderTicket($d, o, link) {
		const D = this.D;
		const S = window.dm_support;
		const tile = (label, value) => `<div class="oc-tile"><span>${label}</span><b>${value || "—"}</b></div>`;
		$d.html(`
			<button type="button" class="btn btn-default btn-sm oc-back" data-back>${frappe.utils.icon("left", "xs")} ${__("Tickets")}</button>
			<div class="oc-head">
				<div><h4><a href="#" data-ticket="${D.esc(o.name)}">${D.esc(o.name)}</a> · ${D.esc(o.subject)}</h4>
					<div class="dd-muted">${__("Support ticket")} · ${__("made {0} by {1}",
						[frappe.datetime.str_to_user(o.created), D.esc(o.created_by)])}</div></div>
				${S.stepPill(o.status)}
			</div>
			<div class="oc-customer"><b>${D.esc(o.customer_name || "")}</b>${o.mobile ? ` · ${D.phoneLink(o.mobile)}` : ""}
				${o.address ? `<div class="dd-muted">${D.esc(o.address)}</div>` : ""}</div>
			<div class="oc-tiles">
				${tile(__("Item"), D.esc(o.item_code))}
				${tile(__("Serial"), D.esc(o.serial_no))}
				${tile(__("Item is in"), D.esc(o.where || __("With the customer")))}
				${tile(__("Warranty"), S.warrantyPill(o))}
			</div>
			${o.trip.date ? `<div class="dd-muted small">${__("Trip")}: ${D.date(o.trip.date)} ${D.esc(o.trip.time || "")}
				· ${D.esc(o.trip.driver_name || __("No driver yet"))}</div>` : ""}
			<h5 class="oc-h">${__("What happened")}</h5>
			${o.steps.length ? this.timeline(o.steps, (v) => format_currency(v), link) : `<div class="dd-muted">${__("No steps logged yet.")}</div>`}`);
		$d.find("[data-ticket]").on("click", (e) => {
			e.preventDefault();
			S.openTicket(o.name, () => this.open(o.name));
		});
	}

	timeline(steps, money, link) {
		const D = this.D;
		let day = null;
		return `<ol class="oc-timeline">${steps.map((s) => {
			const [icon, tone] = OC_EVENTS[s.event] || ["dot", "gray"];
			const d = s.at.slice(0, 10);
			const head = d !== day ? `<li class="oc-day">${D.relativeDay(d)} · ${D.date(d)}</li>` : "";
			day = d;
			const who = s.driver_name && s.driver !== s.user
				? __("{0} · driver {1}", [D.esc(s.user_name), D.esc(s.driver_name)]) : D.esc(s.user_name);
			return `${head}<li class="oc-step">
				<span class="oc-dot oc-${tone}">${frappe.utils.icon(icon, "sm")}</span>
				<div class="oc-step-body">
					<div class="oc-step-top"><b>${__(s.event)}</b>${s.amount ? `<b class="oc-amount">${money(s.amount)}${s.mode_of_payment
						? ` <span class="dd-muted">${D.esc(s.mode_of_payment)}</span>` : ""}</b>` : ""}</div>
					<div class="dd-muted small">${s.at.slice(11, 16)} · ${who}${s.delivery_note ? " · " + link("Delivery Note", s.delivery_note) : ""}</div>
					${s.items ? `<div class="oc-items">${s.items.split("\n").map((i) => `<div>${D.esc(i)}</div>`).join("")}</div>` : ""}
					${s.note ? `<div class="small">${D.esc(s.note)}</div>` : ""}
					${s.reference_name ? `<div class="small">${link(s.reference_doctype, s.reference_name)}</div>` : ""}
				</div></li>`;
		}).join("")}</ol>`;
	}
}
