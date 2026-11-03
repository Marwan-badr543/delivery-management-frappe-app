// Stock Keeper: hand an order's goods to its driver / AC Tech (Send out) and take back what
// returns (Receive back). Both scan the serials; the goods move between the Main Store and
// the Transit Store of Delivery Settings (delivery_management/transit/service.py).

frappe.pages["stock-keeper"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Stock Keeper"), single_column: true });
	wrapper.dm_page = new StockKeeperPage(page);
};

frappe.pages["stock-keeper"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.load();
};

const SK_API = "delivery_management.api.stock_keeper.";

function sk_call(method, args, post) {
	return frappe.call({ method: SK_API + method, args, type: post ? "POST" : "GET", freeze: !!post })
		.then((r) => r.message);
}

class StockKeeperPage {
	constructor(page) {
		this.D = window.dm_drivers;
		this.page = page;
		this.tab = this.D.prefs("stock_keeper").tab || "send";
		this.f = { search: "", work_date: "" };
		this.$root = $(`<div class="dd-page">
			${this.D.tabs([
				{ key: "send", label: __("Send out"), icon: "arrow-up-right" },
				{ key: "receive", label: __("Receive back"), icon: "arrow-down-left" },
			], this.tab)}
			<div class="dd-panel dd-filter-panel"></div>
			<div class="dd-panel dd-table-panel">
				<div class="dd-scroll"><table class="dd-table dd-cards"><thead></thead><tbody></tbody></table></div>
			</div>
		</div>`).appendTo(page.main);
		this.$root.on("click", ".dd-tab-btn", (e) => {
			this.tab = $(e.currentTarget).data("tab");
			this.$root.find(".dd-tab-btn").removeClass("active");
			$(e.currentTarget).addClass("active");
			this.D.prefs("stock_keeper", { tab: this.tab });
			this.renderFilters();
			this.load();
		});
		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		page.add_menu_item(__("Goods in Transit"), () => frappe.set_route("goods-in-transit"));
		sk_call("get_stores").then((s) => (this.stores = s));
		this.renderFilters();
	}

	renderFilters() {
		const send = this.tab === "send";
		const $p = this.$root.find(".dd-filter-panel").html(`
			<div class="dd-filters">
				${send ? `<div class="dd-field"><label>${__("Delivery / installation day")}</label>
					<input type="date" class="form-control" data-f="work_date" value="${this.f.work_date}"></div>` : ""}
				<div class="dd-field dd-search"><label>${__("Search")}</label>
					<span class="dd-search-icon">${frappe.utils.icon("search", "sm")}</span>
					<input class="form-control" data-f="search" placeholder="${__("DN number, customer, phone")}"
						value="${this.D.esc(this.f.search)}"></div>
			</div>
			<div class="dd-filter-foot"><span class="dd-hint">${send
				? __("Orders with a driver, waiting in the store. Open one, scan its serials and send it out.")
				: __("Goods out with drivers that are coming back. Open one and scan what came back.")}</span>
				<span class="dd-count" data-count-label>–</span></div>`);
		const reload = frappe.utils.debounce(() => this.load(), 300);
		$p.find("[data-f]").on("input change", (e) => {
			this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
			reload();
		});
	}

	async load() {
		const D = this.D;
		const send = this.tab === "send";
		this.$root.find(".dd-table thead").html(send
			? `<tr><th>${__("Delivery Note")}</th><th>${__("Customer")}</th><th>${__("Address")}</th>
				<th>${__("Day")}</th><th>${__("Driver / AC Tech")}</th><th>${__("Qty")}</th></tr>`
			: `<tr><th>${__("Delivery Note")}</th><th>${__("Customer")}</th><th>${__("Driver / AC Tech")}</th>
				<th>${__("Left the store")}</th><th>${__("Coming back")}</th></tr>`);
		const $tbody = this.$root.find(".dd-table tbody").html(D.skeleton(send ? 6 : 5));
		const rows = send
			? await sk_call("get_to_send", { search: this.f.search, work_date: this.f.work_date })
			: await sk_call("get_transit", { search: this.f.search, coming_back_only: 1 });
		this.$root.find("[data-count-label]").text(__("{0} orders", [rows.length]));
		this.$root.find(`[data-count="${this.tab}"]`).text(rows.length);
		if (!rows.length) {
			$tbody.html(D.emptyRow(send ? 6 : 5, __("Nothing here"),
				send ? __("No order with a driver is waiting in the store.") : __("Nothing is coming back.")));
			return;
		}
		$tbody.html(rows.map((r) => send ? `
			<tr data-name="${D.esc(r.name)}" tabindex="0">
				<td data-label="${__("Delivery Note")}"><span class="dd-dn">${D.esc(r.name)}</span> ${D.typePill(r.order_type)}</td>
				<td data-label="${__("Customer")}"><b>${D.esc(r.customer_name)}</b></td>
				<td data-label="${__("Address")}" class="dd-addr">${D.esc(r.address) || "—"}</td>
				<td data-label="${__("Day")}">${r.work_date ? `${D.relativeDay(r.work_date)} · ${D.date(r.work_date)}` : "—"}
					<span class="dd-sub">${D.time(r.delivery_time)}</span></td>
				<td data-label="${__("Driver / AC Tech")}">${D.esc(r.driver_name || r.driver)}</td>
				<td data-label="${__("Qty")}">${flt(r.total_qty)}</td>
			</tr>` : `
			<tr data-name="${D.esc(r.name)}" tabindex="0">
				<td data-label="${__("Delivery Note")}"><span class="dd-dn">${D.esc(r.delivery_note)}</span>
					${r.whole_order_back ? `<span class="dd-pill dd-pill-red">${__("Whole order")}</span>` : ""}</td>
				<td data-label="${__("Customer")}"><b>${D.esc(r.customer_name)}</b><span class="dd-sub">${D.phoneLink(r.mobile_no)}</span></td>
				<td data-label="${__("Driver / AC Tech")}">${D.esc(r.driver_name || r.driver)}</td>
				<td data-label="${__("Left the store")}">${frappe.datetime.str_to_user(r.out_at)}</td>
				<td data-label="${__("Coming back")}" class="dd-long">${r.items.filter((i) => i.coming_back > 0)
					.map((i) => `${D.esc(i.item_name || i.item_code)} × ${i.coming_back}`).join("<br>")}</td>
			</tr>`).join(""));
		this.rows = rows;
		$tbody.find("tr[data-name]").on("click keydown", (e) => {
			if (e.type === "keydown" && e.key !== "Enter") return;
			const name = $(e.currentTarget).data("name");
			send ? this.openSend(name) : this.openReceive(this.rows.find((r) => r.name === name));
		});
	}

	// ------------------------------------------------------------ scanning

	/** A dialog listing ``lines`` ({key, item_code, item_name, qty, has_serial_no}) with a scan box. */
	scanDialog({ title, lines, warehouse, intro, extra = [], action, onSubmit, qtyInputs }) {
		const D = this.D;
		const scanned = {};
		lines.forEach((l) => (scanned[l.key] = []));
		const d = new frappe.ui.Dialog({
			title, size: "large",
			fields: [
				{ fieldname: "intro", fieldtype: "HTML" },
				{ fieldname: "scan", fieldtype: "Data", label: __("Scan serial number"), options: "Barcode" },
				{ fieldname: "lines", fieldtype: "HTML" },
				...extra,
			],
			primary_action_label: action,
			primary_action: (values) => {
				const missing = lines.filter((l) => l.has_serial_no && scanned[l.key].length < l.need);
				const qty = {};
				d.$wrapper.find("[data-qty]").each((_i, el) => (qty[$(el).data("qty")] = flt($(el).val())));
				onSubmit({ scanned, qty, values, missing }, d);
			},
		});
		d.fields_dict.intro.$wrapper.html(intro);
		const render = () => d.fields_dict.lines.$wrapper.html(`<div class="dd-items">${lines.map((l) => `
			<div class="dd-item"><div><b>${D.esc(l.item_name || l.item_code)}</b>
				${l.bundle_item ? `<small>${__("in")} ${D.esc(l.bundle_item)}</small>` : ""}
				${l.has_serial_no ? `<small>${scanned[l.key].map((s) => `<span class="dd-pill dd-pill-green">${D.esc(s)}
					<a data-drop="${D.esc(l.key)}" data-sn="${D.esc(s)}">×</a></span>`).join(" ") || __("Scan {0} serial(s)", [l.need])}</small>` : ""}
			</div>
			${l.has_serial_no
				? `<b class="${scanned[l.key].length === l.need ? "dd-ok" : "dd-no"}">${scanned[l.key].length} / ${l.need}</b>`
				: qtyInputs ? `<input type="number" class="form-control input-xs" style="width:90px" min="0" max="${l.need}"
					data-qty="${D.esc(l.key)}" value="${l.need}">` : `<b>× ${l.need}</b>`}
			</div>`).join("")}</div>`);
		render();
		d.fields_dict.lines.$wrapper.on("click", "[data-drop]", (e) => {
			const k = $(e.currentTarget).data("drop");
			scanned[k] = scanned[k].filter((s) => s !== String($(e.currentTarget).data("sn")));
			render();
		});
		// A scanner types the serial fast; a paste puts it at once: either is added a moment after
		// the input stops changing, no Enter needed (Enter still adds it at once).
		const $scan = d.fields_dict.scan.$input;
		const add = async () => {
			const sn = ($scan.val() || "").trim();
			$scan.val("");
			if (!sn) return;
			if (Object.values(scanned).some((list) => list.includes(sn)))
				return frappe.show_alert({ message: __("{0} is already scanned.", [sn]), indicator: "orange" });
			const r = await sk_call("scan_serial", { serial_no: sn, warehouse });
			if (r.problem) return frappe.show_alert({ message: r.problem, indicator: "red" }, 6);
			const line = lines.find((l) => l.has_serial_no && l.item_code === r.item_code
				&& scanned[l.key].length < l.need && (!l.allowed || l.allowed.includes(r.serial_no)));
			if (!line) return frappe.show_alert({ message: __("{0} ({1}) is not expected here.", [r.serial_no, r.item_code]), indicator: "red" }, 6);
			scanned[line.key].push(r.serial_no);
			render();
		};
		const addSoon = frappe.utils.debounce(add, 400);
		$scan.on("input", addSoon);
		$scan.on("keydown", (e) => {
			if (e.key !== "Enter") return;
			e.preventDefault();
			add();
		});
		d.show();
		setTimeout(() => $scan.focus(), 300);
		return d;
	}

	async openSend(name) {
		const note = await sk_call("get_note_to_send", { delivery_note: name });
		const lines = note.lines.map((l) => ({ ...l, need: flt(l.qty) }));
		this.scanDialog({
			title: __("Send out {0}", [name]),
			lines,
			warehouse: this.stores.main,
			action: __("Send out"),
			intro: `<p>${__("Customer")}: <b>${this.D.esc(note.customer_name)}</b> · ${note.order_type === "AC" ? __("AC Tech") : __("Driver")}:
				<b>${this.D.esc(note.driver_name || note.driver)}</b></p>
				<p class="text-muted small">${__("Scan every serial. The goods move from {0} to {1}.", [this.stores.main, this.stores.transit])}</p>`,
			onSubmit: ({ scanned, missing }, d) => {
				if (missing.length) return frappe.msgprint(__("Scan the serials of {0} first.", [missing.map((l) => l.item_code).join(", ")]));
				frappe.confirm(__("Send the goods of {0} out with {1}?", [name, note.driver_name || note.driver]), async () => {
					await sk_call("send_out", { delivery_note: name, scans: JSON.stringify(scanned) }, true);
					frappe.show_alert({ message: __("{0} is on the way", [name]), indicator: "green" });
					d.hide();
					this.load();
				});
			},
		});
	}

	openReceive(row) {
		const lines = row.items.filter((i) => i.coming_back > 0).map((i) => ({
			key: i.name, item_code: i.item_code, item_name: i.item_name, bundle_item: i.bundle_item,
			has_serial_no: i.has_serial_no, need: i.coming_back, allowed: i.coming_back_serials,
		}));
		this.scanDialog({
			title: __("Receive back {0}", [row.delivery_note]),
			lines,
			warehouse: this.stores.transit,
			qtyInputs: true,
			action: __("Receive into {0}", [this.stores.main]),
			intro: `<p>${__("Customer")}: <b>${this.D.esc(row.customer_name)}</b> · ${__("Driver")}: <b>${this.D.esc(row.driver_name || row.driver)}</b></p>
				<p class="text-muted small">${__("Scan the serials that came back; set the quantity of the other items. What is not received stays in {0}.", [this.stores.transit])}</p>`,
			extra: row.whole_order_back ? [{ fieldname: "reserve_again", fieldtype: "Check", default: 1,
				label: __("Reserve the goods for the order again (it will be sent another day)") }] : [],
			onSubmit: ({ scanned, qty, values }, d) => {
				const scans = {};
				lines.forEach((l) => {
					if (l.has_serial_no) { if (scanned[l.key].length) scans[l.key] = scanned[l.key]; }
					else if (qty[l.key] > 0) scans[l.key] = qty[l.key];
				});
				if (!Object.keys(scans).length) return frappe.msgprint(__("Scan what came back."));
				frappe.confirm(__("Receive these goods into {0}?", [this.stores.main]), async () => {
					await sk_call("receive", { transit_name: row.name, scans: JSON.stringify(scans),
						reserve_again: values.reserve_again ? 1 : 0 }, true);
					frappe.show_alert({ message: __("Received into {0}", [this.stores.main]), indicator: "green" });
					d.hide();
					this.load();
				});
			},
		});
	}
}
