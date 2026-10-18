// Delivery Schedule: the orders assigned to a driver (TV / Non AC) or an AC Tech (AC).
// A driver sees only his own orders; System Manager / Delivery Manager see everyone's.
// Port of the old "Delivery Schedule" and "AC Delivery Schedule" pages, without Ship,
// Return and Submit (the office submits the Delivery Note).

frappe.pages["delivery-schedule"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Delivery Schedule"), single_column: true });
	wrapper.dm_page = new DeliverySchedulePage(page);
};

frappe.pages["delivery-schedule"].on_page_show = function (wrapper) {
	wrapper.dm_page && wrapper.dm_page.load();
};

const DS_TYPES = { tv: "Non AC", ac: "AC" };

class DeliverySchedulePage {
	constructor(page) {
		const D = window.dm_drivers;
		this.D = D;
		this.page = page;
		this.isManager = frappe.user.has_role(["System Manager", "Delivery Manager"]) || frappe.session.user === "Administrator";
		const saved = D.prefs("schedule");
		// A driver with only one of the two roles starts on his own tab.
		const onlyAc = !this.isManager && frappe.user.has_role("AC Tech") && !frappe.user.has_role("TV Driver");
		this.tab = saved.tab || (onlyAc ? "ac" : "tv");
		this.pageSize = saved.pageSize || 20;
		this.pageNo = 0;
		const today = frappe.datetime.get_today();
		this.f = {
			start_date: today, end_date: frappe.datetime.add_months(today, 1),
			status: "", search: "", driver: "", area: "",
		};
		this.rows = null;

		this.$root = $(`<div class="dd-page">
			${D.tabs([
				{ key: "tv", label: __("TV / Non AC deliveries"), icon: "truck" },
				{ key: "ac", label: __("AC installations"), icon: "snowflake" },
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
			this.pageNo = 0;
			this.rows = null;
			this.savePrefs();
			this.renderFilters();
			this.load();
		});
		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		if (this.isManager) {
			page.add_menu_item(__("Driver Assignment"), () => frappe.set_route("driver-assignment"));
		}
		page.add_menu_item(__("Installation Statistics"), () => frappe.set_route("installation-statistics"));
		this.renderFilters();
	}

	get type() {
		return DS_TYPES[this.tab];
	}

	savePrefs() {
		this.D.prefs("schedule", { tab: this.tab, pageSize: this.pageSize });
	}

	async renderFilters() {
		const D = this.D;
		const f = this.f;
		const ac = this.tab === "ac";
		const [areas, drivers] = await Promise.all([
			D.areas(),
			this.isManager ? D.call("get_drivers", { order_type: this.type }) : Promise.resolve([]),
		]);
		this.$root.find(".dd-filter-panel").html(`
			<div class="dd-filters">
				<div class="dd-field" style="grid-column: span 2; min-width: 260px">
					<label>${ac ? __("Installation dates") : __("Delivery dates")}</label>
					<div class="dd-range">
						<input type="date" class="form-control" data-f="start_date" value="${f.start_date}">
						<span>${__("to")}</span>
						<input type="date" class="form-control" data-f="end_date" value="${f.end_date}">
						<button class="btn btn-default btn-sm" data-today>${__("Today")}</button>
					</div>
				</div>
				<div class="dd-field"><label>${__("Status")}</label>
					<select class="form-control" data-f="status">${D.select([
						{ value: "", label: __("All statuses") },
						{ value: "pending", label: ac ? __("Pending install") : __("Pending delivery") },
						{ value: "delivered", label: ac ? __("Installed") : __("Delivered") },
					], f.status)}</select></div>
				<div class="dd-field dd-search"><label>${__("Search")}</label>
					<span class="dd-search-icon">${frappe.utils.icon("search", "sm")}</span>
					<input class="form-control" data-f="search" placeholder="${__("DN number, customer, phone")}" value="${D.esc(f.search)}"></div>
				${this.isManager ? `<div class="dd-field"><label>${ac ? __("AC Tech") : __("Driver")}</label>
					<select class="form-control" data-f="driver">${D.select(
						[{ value: "", label: __("All") }, ...drivers.map((d) => ({ value: d.user, label: d.full_name }))], f.driver)}</select></div>` : ""}
				<div class="dd-field"><label>${__("Area")}</label>
					<select class="form-control" data-f="area">${D.select(
						[{ value: "", label: __("All areas") }, ...areas.map((a) => ({ value: a, label: a }))], f.area)}</select></div>
			</div>
			<div class="dd-filter-foot">
				<span class="dd-hint">${this.isManager ? __("All assigned orders.") : __("Your assigned orders.")}
					${__("Tap an order for details, payment, files and the customer's signature.")}</span>
				<div class="dd-actions">
					<span class="dd-count" data-count-label>–</span>
					<select class="form-control input-xs" data-page-size style="width:auto;height:30px">${D.select(
						[10, 20, 50, 100].map((n) => ({ value: n, label: __("{0} per page", [n]) })), this.pageSize)}</select>
					<div class="dd-pager">
						<button class="btn btn-xs btn-default" data-prev>${frappe.utils.icon("left", "xs")}</button>
						<span data-page-label>–</span>
						<button class="btn btn-xs btn-default" data-next>${frappe.utils.icon("right", "xs")}</button>
					</div>
				</div>
			</div>`);
		const $p = this.$root.find(".dd-filter-panel");
		const reload = frappe.utils.debounce(() => { this.pageNo = 0; this.load(); }, 300);
		$p.find("[data-f]").on("input change", (e) => {
			this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
			reload();
		});
		$p.find("[data-today]").on("click", () => {
			const t = frappe.datetime.get_today();
			Object.assign(this.f, { start_date: t, end_date: t });
			$p.find('[data-f="start_date"], [data-f="end_date"]').val(t);
			this.pageNo = 0;
			this.load();
		});
		$p.find("[data-page-size]").on("change", (e) => {
			this.pageSize = cint($(e.currentTarget).val());
			this.pageNo = 0;
			this.savePrefs();
			this.renderRows();
		});
		$p.find("[data-prev]").on("click", () => { this.pageNo = Math.max(0, this.pageNo - 1); this.renderRows(); });
		$p.find("[data-next]").on("click", () => {
			if ((this.pageNo + 1) * this.pageSize < (this.rows || []).length) { this.pageNo += 1; this.renderRows(); }
		});
	}

	async load() {
		const D = this.D;
		const ac = this.tab === "ac";
		this.$root.find(".dd-table thead").html(`<tr>
			<th>${__("Delivery Note")}</th><th>${__("Customer")}</th><th>${__("Address")}</th>
			<th>${ac ? __("Installation day") : __("Schedule")}</th><th>${ac ? __("AC Tech") : __("Driver")}</th>
			<th>${__("Status")}</th><th>${__("Payment")}</th></tr>`);
		if (!this.rows) this.$root.find(".dd-table tbody").html(D.skeleton(7));
		this.$root.find(".dd-table-panel").addClass("dd-loading");
		const seq = (this.seq = (this.seq || 0) + 1);
		let res;
		try {
			res = await D.call("get_schedule", { order_type: this.type, ...this.f });
		} finally {
			this.$root.find(".dd-table-panel").removeClass("dd-loading");
		}
		if (seq !== this.seq) return;
		this.rows = res.rows;
		this.$root.find(`[data-count="${this.tab}"]`).text(res.rows.length);
		this.renderRows();
		const other = this.tab === "tv" ? "ac" : "tv";
		D.call("get_schedule", { order_type: DS_TYPES[other], ...this.f, driver: this.f.driver })
			.then((r) => this.$root.find(`[data-count="${other}"]`).text(r.rows.length));
	}

	renderRows() {
		const D = this.D;
		const ac = this.tab === "ac";
		const rows = this.rows || [];
		const pages = Math.max(1, Math.ceil(rows.length / this.pageSize));
		this.pageNo = Math.min(this.pageNo, pages - 1);
		const slice = rows.slice(this.pageNo * this.pageSize, (this.pageNo + 1) * this.pageSize);
		this.$root.find("[data-count-label]").text(__("{0} orders", [rows.length]));
		this.$root.find("[data-page-label]").text(rows.length ? __("Page {0} / {1}", [this.pageNo + 1, pages]) : "0 / 0");
		this.$root.find("[data-prev]").prop("disabled", this.pageNo === 0);
		this.$root.find("[data-next]").prop("disabled", this.pageNo >= pages - 1);
		const $tbody = this.$root.find(".dd-table tbody");
		if (!slice.length) {
			$tbody.html(D.emptyRow(7, __("No assigned orders"), __("Nothing assigned in this date range.")));
			return;
		}
		$tbody.html(slice.map((r) => `
			<tr data-name="${D.esc(r.name)}" tabindex="0">
				<td data-label="${__("Delivery Note")}"><span class="dd-dn">${D.esc(r.name)}</span></td>
				<td data-label="${__("Customer")}"><b>${D.esc(r.customer_name || r.customer)}</b>
					<span class="dd-sub">${D.phoneLink(r.phone)}</span></td>
				<td data-label="${__("Address")}" class="dd-addr">${D.esc(r.address) || "—"}</td>
				<td data-label="${ac ? __("Installation day") : __("Schedule")}">${D.whenCell(r)}</td>
				<td data-label="${ac ? __("AC Tech") : __("Driver")}">${D.esc(r.driver_name || r.driver)}</td>
				<td data-label="${__("Status")}">${D.statusPill(r)}</td>
				<td data-label="${__("Payment")}">${D.paymentPill(r.payment)}</td>
			</tr>`).join(""));
		$tbody.find("tr[data-name]").on("click keydown", (e) => {
			if (e.type === "keydown" && e.key !== "Enter") return;
			if ($(e.target).closest("a").length) return;
			this.openDetail($(e.currentTarget).data("name"));
		});
	}

	// ------------------------------------------------------------ order detail

	async openDetail(name) {
		const D = this.D;
		const d = new frappe.ui.Dialog({ title: __("Delivery {0}", [name]), size: "extra-large", fields: [{ fieldname: "body", fieldtype: "HTML" }] });
		const $b = d.fields_dict.body.$wrapper.html(`<div class="dd-empty">${__("Loading order…")}</div>`);
		d.show();
		d.$wrapper.on("hidden.bs.modal", () => d.$wrapper.remove());
		const o = await D.call("get_order_detail", { delivery_note: name });
		const ac = o.order_type === "AC";
		const pay = o.payment || {};
		const bundles = {};
		(o.packed_items || []).forEach((p) => (bundles[p.parent_item] = bundles[p.parent_item] || []).push(p));
		const pdfUrl = `/api/method/frappe.utils.print_format.download_pdf?doctype=Delivery%20Note&name=${encodeURIComponent(o.name)}&format=${encodeURIComponent(o.print_format)}&no_letterhead=0`;

		$b.html(`
			<div class="dd-detail">
				<div style="display:flex;flex-direction:column;gap:12px">
					<div class="dd-box"><h5>${__("Customer")}</h5>
						<div class="dd-big">${D.esc(o.customer_name || o.customer)}</div>
						<div>${D.phoneLink(o.phone) || `<span class="dd-muted">${__("No phone")}</span>`}</div></div>
					<div class="dd-box"><h5>${__("Delivery address")}</h5>
						<div>${D.esc(o.address) || `<span class="dd-muted">${__("No address")}</span>`}</div>
						${o.address ? `<a class="small" target="_blank" rel="noopener"
							href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent((o.address || "") + ", Bahrain")}">
							${frappe.utils.icon("map-pin", "xs")} ${__("Open in Google Maps")}</a>` : ""}</div>
					<div class="dd-box"><h5>${__("Schedule")}</h5>
						<div class="dd-kv">
							<span>${ac ? __("Installation day") : __("Delivery day")}</span><b>${D.relativeDay(o.work_date)} · ${D.date(o.work_date)}</b>
							${ac ? `<span>${__("AC units")}</span><b>${o.ac_units || 0}</b>` : `<span>${__("Time")}</span><b>${D.time(o.delivery_time) || "—"}</b>`}
							<span>${ac ? __("AC Tech") : __("Driver")}</span><b>${D.esc(o.driver_name || o.driver || __("Unassigned"))}</b>
							<span>${__("Status")}</span><span>${D.statusPill(o)}</span>
						</div>
						${o.notes ? `<div class="small"><span class="dd-muted">${__("Notes")}:</span> ${D.esc(o.notes)}</div>` : ""}</div>
				</div>
				<div style="display:flex;flex-direction:column;gap:12px">
					<div class="dd-box"><h5>${__("Items")}</h5><div class="dd-items">${o.items.map((i) => `
						<div class="dd-item"><div>${D.esc(i.item_name || i.item_code)}
							${i.serial_no ? `<small>${__("Serial")}: ${D.esc(i.serial_no.split("\n").join(", "))}</small>` : ""}
							${(bundles[i.item_code] || []).map((p) => `<small>• ${D.esc(p.item_name || p.item_code)} × ${p.qty}</small>`).join("")}
						</div><b>× ${i.qty}</b></div>`).join("")}</div></div>
					<div class="dd-box"><h5>${__("Payment")}</h5>
						<div class="dd-kv">
							<span>${__("Total")}</span><b>${D.money(pay.total, pay.currency)}</b>
							<span>${__("Paid")}</span><b class="dd-ok">${D.money(pay.paid, pay.currency)}</b>
							<span>${__("Outstanding")}</span><b class="${pay.outstanding > 0 ? "dd-no" : ""}">${D.money(pay.outstanding, pay.currency)}</b>
						</div>
						${pay.outstanding > 0 ? `
							<div class="dd-pay-row">
								<input type="number" min="0" step="0.001" class="form-control" data-amount
									placeholder="${__("Amount")}" value="${pay.outstanding}">
								<select class="form-control" data-mop>${D.select((o.modes_of_payment || []).map((m) => ({ value: m, label: m })), "Cash")}</select>
								<button class="btn btn-primary btn-sm" data-pay>${frappe.utils.icon("money-coins-1", "xs")} ${__("Record payment")}</button>
							</div>` : `<span class="dd-pill dd-pill-green">${pay.invoices && pay.invoices.length ? __("Fully paid") : __("No invoice")}</span>`}
					</div>
				</div>
			</div>
			<div class="dd-box" style="margin-top:12px"><h5>${__("Actions")}</h5>
				<div class="dd-btn-row">
					${o.docstatus === 0 ? `<button class="btn btn-success" data-submit-dn>${frappe.utils.icon("check", "sm")} ${__("Delivered: submit Delivery Note")}</button>` : ""}
					<a class="btn btn-default" href="/desk/delivery-note/${encodeURIComponent(o.name)}">${frappe.utils.icon("file", "sm")} ${__("Open Delivery Note")}</a>
					<a class="btn btn-default" href="${pdfUrl}" target="_blank" rel="noopener">${frappe.utils.icon("printer", "sm")} ${__("Download DN PDF")}</a>
					<button class="btn btn-default" data-upload>${frappe.utils.icon("upload", "sm")} ${__("Upload photo / file")}</button>
				</div>
				<input type="file" accept="image/*,.pdf,.doc,.docx" data-file hidden>
				<div class="dd-files small">${(o.attachments || []).length
					? `<span class="dd-muted">${__("Attached to this delivery")}:</span>` + o.attachments.map((f) =>
						`<a href="${D.esc(f.file_url)}" target="_blank" rel="noopener">${D.esc(f.file_name)}</a>`).join("")
					: ""}</div>
			</div>
			<div class="dd-box" style="margin-top:12px"><h5>${__("Customer signature")}</h5>
				<div class="dd-sign"></div>
				<div class="dd-actions" style="justify-content:flex-end">
					<button class="btn btn-primary btn-sm" data-save-sign>${frappe.utils.icon("upload", "xs")} ${__("Upload signature")}</button>
				</div>
			</div>`);

		$b.find("[data-pay]").on("click", () => {
			const amount = flt($b.find("[data-amount]").val(), 3);
			const mop = $b.find("[data-mop]").val();
			if (!(amount > 0) || amount > flt(pay.outstanding, 3) + 0.0005) {
				frappe.msgprint(__("Enter an amount between 0 and {0}.", [D.money(pay.outstanding, pay.currency)]));
				return;
			}
			if (!mop) return frappe.msgprint(__("No Mode of Payment has an account for this company."));
			frappe.confirm(__("Record a payment of {0} ({1}) from {2}?", [D.money(amount, pay.currency), mop, D.esc(o.customer_name)]), async () => {
				const r = await D.call("record_payment", { delivery_note: o.name, amount, mode_of_payment: mop }, { post: true, freeze: true });
				frappe.show_alert({ message: __("Payment {0} recorded", [r.payment_entry]), indicator: "green" });
				d.hide();
				this.load();
				this.openDetail(name);
			});
		});

		$b.find("[data-submit-dn]").on("click", () => {
			frappe.confirm(__("Mark {0} as delivered to {1}? The Delivery Note will be submitted and cannot be edited after.",
				[o.name, D.esc(o.customer_name || o.customer)]), async () => {
				await D.call("submit_delivery_note", { delivery_note: o.name }, { post: true, freeze: true });
				frappe.show_alert({ message: __("Delivery Note {0} submitted", [o.name]), indicator: "green" });
				d.hide();
				this.load();
			});
		});

		// Files and the signature are attached to the Delivery Note; the order is reloaded to list them.
		const upload = async (filename, filedata) => {
			const r = await D.call("upload_attachment", { delivery_note: o.name, filename, filedata }, { post: true, freeze: true });
			frappe.show_alert({ message: __("Attached to {0}", [r.attached_to]), indicator: "green" });
			d.hide();
			this.openDetail(name);
		};
		$b.find("[data-upload]").on("click", () => $b.find("[data-file]").trigger("click"));
		$b.find("[data-file]").on("change", (e) => {
			const file = e.target.files[0];
			e.target.value = "";
			if (!file) return;
			if (file.size > 25 * 1024 * 1024) return frappe.msgprint(__("The file is too large (max 25 MB)."));
			const reader = new FileReader();
			reader.onload = () => upload(file.name, reader.result);
			reader.readAsDataURL(file);
		});

		const sign = frappe.ui.form.make_control({
			parent: $b.find(".dd-sign"),
			df: { fieldtype: "Signature", fieldname: "signature", label: __("Sign here") },
			render_input: true,
		});
		$b.find("[data-save-sign]").on("click", () => {
			const data = sign.get_value();
			if (!data) return frappe.msgprint(__("Ask the customer to sign first."));
			const safe = String(o.customer_name || "customer").replace(/[^\w\u0600-\u06FF-]+/g, "_");
			upload(`signature_${safe}_${Date.now()}.png`, data);
		});
	}
}
