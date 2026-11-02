// Goods in Transit: every order whose goods are out of the Main Store (Delivery Transit),
// with whom, since when, for which customer, and what of it is delivered, back in the store
// or still coming back. Receiving is done on the Stock Keeper page.

frappe.pages["goods-in-transit"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Goods in Transit"), single_column: true });
	wrapper.dm_page = new GoodsInTransitPage(page);
};

frappe.pages["goods-in-transit"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.load();
};

class GoodsInTransitPage {
	constructor(page) {
		this.D = window.dm_drivers;
		this.page = page;
		this.f = { search: "", driver: "", status: "" };
		this.$root = $(`<div class="dd-page">
			<div class="dd-panel dd-filter-panel"></div>
			<div class="dd-panel dd-table-panel">
				<div class="dd-scroll"><table class="dd-table dd-cards">
					<thead><tr><th>${__("Delivery Note")}</th><th>${__("Customer")}</th><th>${__("Driver / AC Tech")}</th>
						<th>${__("Left the store")}</th><th>${__("Status")}</th><th>${__("Items")}</th></tr></thead>
					<tbody></tbody></table></div>
			</div>
		</div>`).appendTo(page.main);
		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		page.set_primary_action(__("Stock Keeper"), () => frappe.set_route("stock-keeper"), "package");
		this.renderFilters();
	}

	async renderFilters() {
		const D = this.D;
		const drivers = await frappe.xcall("delivery_management.api.stock_keeper.get_drivers");
		const $p = this.$root.find(".dd-filter-panel").html(`
			<div class="dd-filters">
				<div class="dd-field dd-search"><label>${__("Search")}</label>
					<span class="dd-search-icon">${frappe.utils.icon("search", "sm")}</span>
					<input class="form-control" data-f="search" placeholder="${__("DN number, customer, phone, driver")}"></div>
				<div class="dd-field"><label>${__("Driver / AC Tech")}</label>
					<select class="form-control" data-f="driver">${D.select(
						[{ value: "", label: __("All") }, ...drivers.map((d) => ({ value: d.user, label: d.full_name }))], "")}</select></div>
				<div class="dd-field"><label>${__("Status")}</label>
					<select class="form-control" data-f="status">${D.select([
						{ value: "", label: __("All") },
						{ value: "On the way", label: __("On the way") },
						{ value: "Coming back", label: __("Coming back") },
					], "")}</select></div>
			</div>
			<div class="dd-filter-foot"><span class="dd-hint">${__("Goods out of the Main Store. An order leaves this list when all its goods are delivered or back in the store.")}</span>
				<span class="dd-count" data-count-label>–</span></div>`);
		const reload = frappe.utils.debounce(() => this.load(), 300);
		$p.find("[data-f]").on("input change", (e) => {
			this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
			reload();
		});
	}

	async load() {
		const D = this.D;
		const $tbody = this.$root.find(".dd-table tbody").html(D.skeleton(6));
		const rows = await frappe.xcall("delivery_management.api.stock_keeper.get_transit", this.f);
		this.$root.find("[data-count-label]").text(__("{0} orders", [rows.length]));
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
			<td data-label="${__("Customer")}"><b>${D.esc(r.customer_name)}</b><span class="dd-sub">${D.phoneLink(r.mobile_no)}</span>
				<span class="dd-sub">${D.esc(r.address)}</span></td>
			<td data-label="${__("Driver / AC Tech")}">${D.esc(r.driver_name || r.driver)}
				${r.work_date ? `<span class="dd-sub">${D.relativeDay(r.work_date)} · ${D.date(r.work_date)}</span>` : ""}</td>
			<td data-label="${__("Left the store")}">${frappe.datetime.str_to_user(r.out_at)}<span class="dd-sub">${__("by")} ${D.esc(r.out_by)}</span></td>
			<td data-label="${__("Status")}">${pill(r)}</td>
			<td data-label="${__("Items")}">${items(r)}</td>
		</tr>`).join(""));
	}
}
