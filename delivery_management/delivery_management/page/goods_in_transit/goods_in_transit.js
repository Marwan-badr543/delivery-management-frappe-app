// Goods in Transit: what is outside the store with the drivers / AC Techs, in two tabs.
// * Goods: every order whose goods are out of the Main Store (Delivery Transit): with whom,
//   since when, for which customer, and what of it is delivered, back or still coming back.
//   Receiving goods is done on the Stock Keeper page.
// * Cash (delivery office and accounts): cash taken from customers on delivery and not handed
//   over yet (Driver Cash), per driver. "Receive" moves it into the store's cash account.

frappe.pages["goods-in-transit"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Goods in Transit"), single_column: true });
	wrapper.dm_page = new GoodsInTransitPage(page);
};

frappe.pages["goods-in-transit"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.load();
};

const GIT_API = "delivery_management.api.stock_keeper.";

// Where the order's goods are now → pill colour.
const GIT_GOODS = {
	Delivered: "green",
	"With the driver": "blue",
	"Coming back": "orange",
	"Back in the store": "gray",
};

class GoodsInTransitPage {
	constructor(page) {
		this.D = window.dm_drivers;
		this.page = page;
		this.f = { search: "", driver: "", status: "" };
		this.tab = "goods";
		this.cash = null;
		this.$root = $(`<div class="dd-page">
			<div class="git-tabs"></div>
			<div class="dd-panel dd-filter-panel"></div>
			<div class="git-goods">
				<div class="dd-panel dd-table-panel">
					<div class="dd-scroll"><table class="dd-table dd-cards">
						<thead><tr><th>${__("Delivery Note")}</th><th>${__("Customer")}</th><th>${__("Driver / AC Tech")}</th>
							<th>${__("Left the store")}</th><th>${__("Status")}</th><th>${__("Items")}</th></tr></thead>
						<tbody></tbody></table></div>
				</div>
			</div>
			<div class="git-cash hidden"></div>
		</div>`).appendTo(page.main);
		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		page.set_primary_action(__("Stock Keeper"), () => frappe.set_route("stock-keeper"), "package");
		this.$root.on("click", "[data-receive-driver]", (e) => this.confirmReceive(this.byDriver[e.currentTarget.dataset.receiveDriver]));
		this.$root.on("click", "[data-receive-one]", (e) => {
			const row = this.cash.find((c) => c.name === e.currentTarget.dataset.receiveOne);
			row && this.confirmReceive([row]);
		});
		this.ready = this.init();
	}

	async init() {
		const [drivers, canCash] = await Promise.all([
			frappe.xcall(GIT_API + "get_drivers"),
			frappe.xcall(GIT_API + "can_receive_cash"),
		]);
		this.canCash = canCash;
		if (canCash) {
			this.$root.find(".git-tabs").html(this.D.tabs([
				{ key: "goods", label: __("Goods"), icon: "package" },
				{ key: "cash", label: __("Cash with drivers"), icon: "money-coins-1" },
			], this.tab)).on("click", "[data-tab]", (e) => this.switchTab(e.currentTarget.dataset.tab));
		}
		this.renderFilters(drivers);
	}

	switchTab(tab) {
		this.tab = tab;
		this.$root.find("[data-tab]").removeClass("active").filter(`[data-tab="${tab}"]`).addClass("active");
		this.$root.find(".git-goods").toggleClass("hidden", tab !== "goods");
		this.$root.find(".git-cash").toggleClass("hidden", tab !== "cash");
		this.$root.find("[data-goods-only]").toggleClass("hidden", tab !== "goods");
		this.$root.find("[data-hint]").text(tab === "goods"
			? __("Goods out of the Main Store. An order leaves this list when all its goods are delivered or back in the store.")
			: __("Cash collected on delivery. It stays here until you count it and receive it from the driver."));
		this.load();
	}

	renderFilters(drivers) {
		const D = this.D;
		const $p = this.$root.find(".dd-filter-panel").html(`
			<div class="dd-filters">
				<div class="dd-field dd-search"><label for="git-q">${__("Search")}</label>
					<span class="dd-search-icon">${frappe.utils.icon("search", "sm")}</span>
					<input id="git-q" class="form-control" data-f="search" placeholder="${__("DN number, customer, phone, driver")}"></div>
				<div class="dd-field"><label>${__("Driver / AC Tech")}</label>
					<select class="form-control" data-f="driver">${D.select(
						[{ value: "", label: __("All") }, ...drivers.map((d) => ({ value: d.user, label: d.full_name }))], "")}</select></div>
				<div class="dd-field" data-goods-only><label>${__("Status")}</label>
					<select class="form-control" data-f="status">${D.select([
						{ value: "", label: __("All") },
						{ value: "On the way", label: __("On the way") },
						{ value: "Coming back", label: __("Coming back") },
					], "")}</select></div>
			</div>
			<div class="dd-filter-foot"><span class="dd-hint" data-hint>${__("Goods out of the Main Store. An order leaves this list when all its goods are delivered or back in the store.")}</span>
				<span class="dd-count" data-count-label>–</span></div>`);
		const reload = frappe.utils.debounce(() => this.load(), 300);
		$p.find("[data-f]").on("input change", (e) => {
			this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
			reload();
		});
	}

	async load() {
		await this.ready;
		// The cash total shows on its tab even while the goods are open.
		if (this.canCash) this.loadCash();
		if (this.tab === "goods") this.loadGoods();
	}

	// ------------------------------------------------------------------ goods

	async loadGoods() {
		const D = this.D;
		const $tbody = this.$root.find(".git-goods tbody").html(D.skeleton(6));
		const rows = await frappe.xcall(GIT_API + "get_transit", this.f);
		this.$root.find('[data-count="goods"]').text(rows.length);
		if (this.tab === "goods") this.$root.find("[data-count-label]").text(__("{0} orders", [rows.length]));
		if (!rows.length) {
			$tbody.html(D.emptyRow(6, __("Nothing in transit"), __("All goods are in the store or with the customers.")));
			return;
		}
		const pill = (r) => r.whole_order_back
			? `<span class="dd-pill dd-pill-red">${__("Whole order coming back")}</span>`
			: r.status === "On the way"
				? `<span class="dd-pill dd-pill-blue">${frappe.utils.icon("truck", "xs")} ${__("On the way")}</span>`
				: `<span class="dd-pill dd-pill-orange">${__("Coming back")}</span>`;
		const items = (r) => r.items.map((i) => `<div class="small">
			<b>${D.esc(i.item_name || i.item_code)}</b>${i.bundle_item ? ` <span class="dd-muted">(${D.esc(i.bundle_item)})</span>` : ""}:
			${__("out")} ${i.qty}${i.delivered_qty ? ` · ${__("delivered")} ${i.delivered_qty}` : ""}${i.received_qty ? ` · ${__("back")} ${i.received_qty}` : ""}${i.coming_back ? ` · <b class="dd-no">${__("coming back")} ${i.coming_back}</b>` : ""}
			${i.serial_no ? `<span class="dd-sub">${D.esc(i.serial_no.split("\n").join(", "))}</span>` : ""}</div>`).join("");
		$tbody.html(rows.map((r) => `<tr>
			<td data-label="${__("Delivery Note")}"><a class="dd-dn" href="/desk/delivery-note/${encodeURIComponent(r.delivery_note)}">${D.esc(r.delivery_note)}</a>
				${D.typePill(r.delivery_type)}
				<span class="dd-sub"><a href="/desk/stock-entry/${encodeURIComponent(r.out_entry)}">${D.esc(r.out_entry)}</a>
				${(r.back_entries || "").split("\n").filter(Boolean).map((e) => `<a href="/desk/stock-entry/${encodeURIComponent(e)}">${D.esc(e)}</a>`).join(" ")}</span></td>
			<td data-label="${__("Customer")}" class="dd-long"><b>${D.esc(r.customer_name)}</b><span class="dd-sub">${D.phoneLink(r.mobile_no)}</span>
				<span class="dd-sub">${D.esc(r.address)}</span></td>
			<td data-label="${__("Driver / AC Tech")}">${D.esc(r.driver_name || r.driver)}
				${r.work_date ? `<span class="dd-sub">${D.relativeDay(r.work_date)} · ${D.date(r.work_date)}</span>` : ""}</td>
			<td data-label="${__("Left the store")}">${frappe.datetime.str_to_user(r.out_at)}<span class="dd-sub">${__("by")} ${D.esc(r.out_by)}</span></td>
			<td data-label="${__("Status")}">${pill(r)}</td>
			<td data-label="${__("Items")}" class="dd-long">${items(r)}</td>
		</tr>`).join(""));
	}

	// ------------------------------------------------------------------ cash

	async loadCash() {
		const visible = this.tab === "cash";
		const $c = this.$root.find(".git-cash");
		if (visible && !this.cash) $c.html(`<div class="dd-panel"><div class="oc-loading">${__("Loading…")}</div></div>`);
		const { search, driver } = this.f;
		this.cash = await frappe.xcall(GIT_API + "get_cash", { search, driver });
		const total = this.cash.reduce((s, r) => s + flt(r.amount), 0);
		this.$root.find('[data-count="cash"]').text(this.cash.length ? this.money(total) : 0);
		if (visible) {
			this.$root.find("[data-count-label]").text(__("{0} payments", [this.cash.length]));
			this.renderCash(total);
		}
	}

	money(v) {
		const c = (this.cash || [])[0];
		return format_currency(v, c && c.currency);
	}

	// How long the oldest cash has been out: from the next day it needs attention.
	age(when) {
		const days = frappe.datetime.get_day_diff(frappe.datetime.get_today(), when.slice(0, 10));
		return { days, label: days <= 0 ? __("Today") : days === 1 ? __("Since yesterday") : __("{0} days", [days]) };
	}

	renderCash(total) {
		const $c = this.$root.find(".git-cash");
		if (!this.cash.length) {
			$c.html(`<div class="dd-panel"><div class="oc-empty">${frappe.utils.icon("money-coins-1", "lg")}
				<b>${__("No cash with the drivers")}</b>
				<span>${__("Cash taken on delivery shows here until you receive it. Card and transfer payments do not.")}</span></div></div>`);
			return;
		}
		this.byDriver = {};
		this.cash.forEach((r) => (this.byDriver[r.driver] = this.byDriver[r.driver] || []).push(r));
		// Rows come oldest first, so the driver holding cash the longest is on top.
		const drivers = Object.values(this.byDriver);
		const oldest = this.age(this.cash[0].collected_at);
		const kpi = (label, value, icon, cls, hint = "") => `<div class="dd-kpi ${cls}"><div><span>${label}</span><b>${value}</b>
			${hint ? `<small>${hint}</small>` : ""}</div><div class="dd-kpi-icon">${frappe.utils.icon(icon, "md")}</div></div>`;

		$c.html(`
			<div class="dd-kpis git-kpis">
				${kpi(__("Cash with drivers"), this.money(total), "money-coins-1", "dd-kpi-all", __("{0} payments", [this.cash.length]))}
				${kpi(__("Drivers holding cash"), drivers.length, "users", "dd-kpi-ac")}
				${kpi(__("Oldest not handed over"), oldest.label, "calendar", oldest.days > 0 ? "dd-kpi-warn" : "dd-kpi-tv",
					oldest.days > 0 ? __("Collect it today") : __("Collected today"))}
			</div>
			${drivers.map((rows) => this.driverCard(rows)).join("")}`);
	}

	driverCard(rows) {
		const D = this.D;
		const d = rows[0];
		const sum = rows.reduce((s, r) => s + flt(r.amount), 0);
		const since = this.age(d.collected_at);
		return `<section class="dd-panel git-driver ${since.days > 0 ? "git-late" : ""}">
			<header class="git-driver-head">
				<div class="git-who">${frappe.avatar(d.driver, "avatar-medium")}
					<div><b>${D.esc(d.driver_name || d.driver)}</b>
						<span class="dd-sub">${__("{0} payments", [rows.length])} · ${__("holding since {0}", [frappe.datetime.comment_when(d.collected_at)])}
						${since.days > 0 ? ` <span class="dd-pill dd-pill-orange">${since.label}</span>` : ""}</span></div></div>
				<div class="git-hold"><span>${__("Has")}</span><b>${this.money(sum)}</b></div>
				<button type="button" class="btn btn-primary btn-sm" data-receive-driver="${D.esc(d.driver)}">
					${frappe.utils.icon("check", "xs")} ${__("Receive {0}", [this.money(sum)])}</button>
			</header>
			<div class="dd-scroll"><table class="dd-table dd-cards git-cash-table">
				<thead><tr><th>${__("Order")}</th><th>${__("Customer")}</th><th>${__("Collected")}</th><th>${__("Goods now")}</th>
					<th class="text-right">${__("Amount")}</th><th></th></tr></thead>
				<tbody>${rows.map((r) => `<tr>
					<td data-label="${__("Order")}"><a class="dd-dn" href="/desk/delivery-note/${encodeURIComponent(r.delivery_note)}">${D.esc(r.delivery_note)}</a>
						${D.typePill(r.delivery_type)}
						<span class="dd-sub"><a href="/desk/payment-entry/${encodeURIComponent(r.payment_entry)}">${D.esc(r.payment_entry)}</a></span></td>
					<td data-label="${__("Customer")}" class="dd-long"><b>${D.esc(r.customer_name || "")}</b>
						${r.sales_invoice ? `<span class="dd-sub">${D.esc(r.sales_invoice)}</span>` : ""}</td>
					<td data-label="${__("Collected")}">${frappe.datetime.str_to_user(r.collected_at.slice(0, 16))}
						<span class="dd-sub">${frappe.datetime.comment_when(r.collected_at)}</span></td>
					<td data-label="${__("Goods now")}"><span class="dd-pill dd-pill-${GIT_GOODS[r.goods] || "gray"}">${__(r.goods)}</span></td>
					<td data-label="${__("Amount")}" class="text-right"><b class="git-amount">${this.money(r.amount)}</b>
						<span class="dd-sub">${D.esc(r.mode_of_payment)}</span></td>
					<td class="text-right"><button type="button" class="btn btn-default btn-xs" data-receive-one="${D.esc(r.name)}">${__("Receive")}</button></td>
				</tr>`).join("")}</tbody>
			</table></div>
		</section>`;
	}

	// The office counts the money first, then confirms: one Journal Entry into the store's cash.
	confirmReceive(rows) {
		if (!rows || !rows.length) return;
		const D = this.D;
		const sum = rows.reduce((s, r) => s + flt(r.amount), 0);
		const who = D.esc(rows[0].driver_name || rows[0].driver);
		const dialog = new frappe.ui.Dialog({
			title: __("Receive cash from {0}", [who]),
			fields: [{ fieldtype: "HTML", fieldname: "body" }],
			primary_action_label: __("I received {0}", [this.money(sum)]),
			primary_action: async () => {
				dialog.get_primary_btn().prop("disabled", true);
				try {
					const r = await frappe.xcall(GIT_API + "receive_cash", { names: rows.map((x) => x.name) });
					dialog.hide();
					frappe.show_alert({ message: __("Received {0} from {1} ({2}).", [this.money(sum), who, r.journal_entries.join(", ")]),
						indicator: "green" }, 7);
					this.loadCash();
				} catch (e) {
					dialog.get_primary_btn().prop("disabled", false);
				}
			},
			secondary_action_label: __("Cancel"),
		});
		dialog.fields_dict.body.$wrapper.html(`
			<div class="git-confirm">
				<div class="git-confirm-total"><span>${__("Count the money first")}</span><b>${this.money(sum)}</b></div>
				<ul>${rows.map((r) => `<li><span>${D.esc(r.delivery_note)} · ${D.esc(r.customer_name || "")}</span><b>${this.money(r.amount)}</b></li>`).join("")}</ul>
				<p class="dd-muted small">${__("The money moves from Cash With Drivers into the store's cash, the orders log it and these payments leave this list.")}</p>
			</div>`);
		dialog.show();
	}
}
