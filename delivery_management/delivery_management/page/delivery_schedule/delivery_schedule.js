// Delivery Schedule: the orders assigned to a driver (TV / Non AC) or an AC Tech (AC).
// A driver sees only his own orders; System Manager / Delivery Manager see everyone's.
// Port of the old "Delivery Schedule" and "AC Delivery Schedule" pages. It lists the
// Delivery Notes whose goods the stock keeper sent out ("On the way", still drafts). At the
// customer the driver takes off the note what is not installed (credited on the sale), or
// brings the whole order back (customer not found); the customer signs what is left and the
// driver marks it Delivered (the note is submitted). He prints the invoice and Credit Notes.

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
						{ value: "on_the_way", label: __("On the way") },
						{ value: "delivered", label: ac ? __("Installed") : __("Delivered") },
						{ value: "returned", label: __("Coming back") },
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
			$tbody.html(D.emptyRow(7, __("No orders on the way"), __("Nothing on the way in this date range.")));
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
		const files = o.attachments || [];
		const signed = files.some((f) => f.file_name.startsWith("signature_"));
		const hasMedia = files.some((f) => !f.file_name.startsWith("signature_")
			&& /\.(jpe?g|png|gif|webp|heic|heif|mp4|mov|webm|3gp|mkv)$/i.test(f.file_name));
		const pdfUrl = `/api/method/frappe.utils.print_format.download_pdf?doctype=Delivery%20Note&name=${encodeURIComponent(o.name)}&format=${encodeURIComponent(o.print_format)}&no_letterhead=0`;
		const onTheWay = o.dm_delivery_status === "On the way";
		const draft = o.docstatus === 0;
		const saleUrl = (inv) => `/api/method/delivery_management.api.drivers.print_sale_document?delivery_note=${encodeURIComponent(o.name)}&invoice=${encodeURIComponent(inv)}`;

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
							${i.returned_qty > 0 ? `<small class="dd-no">${__("Returned")}: ${i.returned_qty} · ${__("Received")}: ${i.qty - i.returned_qty}</small>` : ""}
						</div><b>× ${i.qty - (i.returned_qty || 0)}</b></div>`).join("")}</div></div>
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
					<a class="btn btn-default" href="/desk/delivery-note/${encodeURIComponent(o.name)}">${frappe.utils.icon("file", "sm")} ${__("Open Delivery Note")}</a>
					<a class="btn btn-default" href="${pdfUrl}" target="_blank" rel="noopener">${frappe.utils.icon("printer", "sm")} ${__("Download DN PDF")}</a>
					<button class="btn btn-default" data-upload>${frappe.utils.icon("upload", "sm")} ${__("Upload photo / video")}</button>
					${(o.documents || []).map((doc) => `<a class="btn btn-default" href="${saleUrl(doc.name)}" target="_blank" rel="noopener">
						${frappe.utils.icon("printer", "sm")} ${doc.is_return ? __("Credit Note {0}", [D.esc(doc.name)]) : __("Invoice {0}", [D.esc(doc.name)])}</a>`).join("")}
				</div>
				${onTheWay && draft ? `<div class="dd-btn-row" style="margin-top:8px">
					<button class="btn btn-danger" data-take-off>${frappe.utils.icon("undo", "sm")} ${ac ? __("Items not installed") : __("Items not delivered")}</button>
					<button class="btn btn-warning" data-not-found>${frappe.utils.icon("calendar", "sm")} ${__("Customer not available: deliver later")}</button>
				</div>` : ""}
				<input type="file" accept="image/*,video/*,.pdf,.doc,.docx" data-file hidden>
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
			</div>
			${o.dm_delivery_status === "On the way" ? `<div class="dd-box" style="margin-top:12px"><h5>${ac ? __("Installed") : __("Delivered")}</h5>
				<div class="dd-kv">
					<span>${__("Photo / video")}</span><b class="${hasMedia ? "dd-ok" : "dd-no"}">${hasMedia ? __("Uploaded") : __("Missing")}</b>
					<span>${__("Customer signature")}</span><b class="${signed ? "dd-ok" : "dd-no"}">${signed ? __("Uploaded") : __("Missing")}</b>
				</div>
				<div class="dd-actions" style="justify-content:flex-end">
					<button class="btn btn-success" data-deliver ${hasMedia && signed ? "" : "disabled"}>${frappe.utils.icon("check", "sm")} ${__("Mark as delivered")}</button>
				</div>
			</div>` : ""}`);

		$b.find("[data-not-found]").on("click", () => {
			// Not a cancellation: the order is kept and sent again, so say it plainly.
			const ask = new frappe.ui.Dialog({
				title: __("Deliver {0} on another day?", [o.name]),
				primary_action_label: __("Yes, bring it back for later"),
				primary_action: async () => {
					await D.call("bring_back_all", { delivery_note: o.name }, { post: true, freeze: true });
					ask.hide();
					frappe.show_alert({ message: __("{0} is coming back to the store and will be sent again", [o.name]),
						indicator: "orange" });
					d.hide();
					this.load();
				},
				secondary_action_label: __("Go back"),
			});
			ask.$body.html(`<div class="dd-later">
				<p>${__("Use this when you could not hand over the order: the customer is not at home, does not answer or asks for another day.")}</p>
				<ul>
					<li>${frappe.utils.icon("check", "sm")} ${__("The order is <b>not cancelled</b> and nothing is refunded.")}</li>
					<li>${frappe.utils.icon("check", "sm")} ${__("Bring all its goods back to the store; they stay <b>reserved for this order</b>.")}</li>
					<li>${frappe.utils.icon("check", "sm")} ${__("The office plans a new day and the order is sent out again.")}</li>
				</ul>
				<p class="dd-muted">${__("If the customer refuses some items, go back and use <b>{0}</b> instead.", [ac ? __("Items not installed") : __("Items not delivered")])}</p>
			</div>`);
			ask.show();
		});

		$b.find("[data-take-off]").on("click", () => this.takeOff(o, () => { d.hide(); this.load(); this.openDetail(name); }));

		$b.find("[data-deliver]").on("click", () => {
			frappe.confirm(__("Mark {0} as delivered?", [o.name]), async () => {
				await D.call("mark_delivered", { delivery_note: o.name }, { post: true, freeze: true });
				frappe.show_alert({ message: __("{0} delivered", [o.name]), indicator: "green" });
				d.hide();
				this.load();
			});
		});

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
				frappe.show_alert({ message: r.held_by_driver
					? __("Payment {0} recorded. You hold {1} in cash: hand it to the office.", [r.payment_entry, D.money(amount, pay.currency)])
					: __("Payment {0} recorded", [r.payment_entry]), indicator: "green" }, r.held_by_driver ? 8 : 5);
				d.hide();
				this.load();
				this.openDetail(name);
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

	/** The driver chooses what the customer does not get: a quantity per line, and for serialised
	 * goods (a bundle's components too) which serials come back. It is credited on the sale. */
	takeOff(o, done) {
		const D = this.D;
		const d = new frappe.ui.Dialog({
			title: __("Not delivered: {0}", [o.name]), size: "large",
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
			primary_action_label: __("Take off the note"),
			primary_action: () => {
				const lines = o.items.map((i) => ({
					dn_detail: i.name,
					qty: flt(d.$wrapper.find(`[data-back-qty="${i.name}"]`).val()),
					serials: d.$wrapper.find(`[data-back-sn="${i.name}"]:checked`).map((_k, el) => el.value).get(),
				})).filter((l) => l.qty > 0);
				if (!lines.length) return frappe.msgprint(__("Enter what comes back."));
				frappe.confirm(__("These goods come back to the store and are credited to the customer. Continue?"), async () => {
					const r = await D.call("take_off_note", { delivery_note: o.name, lines: JSON.stringify(lines) }, { post: true, freeze: true });
					frappe.show_alert({ message: __("Credit Note {0} made", [(r.credit_notes || []).join(", ")]), indicator: "green" });
					d.hide();
					done();
				});
			},
		});
		// A line with serials comes back by the serials ticked; only a line without serials takes a quantity.
		const hasSn = (i) => (i.serials || []).some((g) => g.serials.length);
		const $body = d.fields_dict.body.$wrapper.html(`<p class="text-muted small">${__("The customer signs only for what stays. Tick the serials that come back; enter a quantity only for items without serials.")}</p>
			<div class="dd-items">${o.items.map((i) => `<div class="dd-item"><div><b>${D.esc(i.item_name || i.item_code)}</b> × ${i.qty}
				${(i.serials || []).map((g) => `<small>${D.esc(g.item_code)}: ${g.serials.map((sn) => `<label style="margin-right:10px;font-weight:normal">
					<input type="checkbox" data-back-sn="${D.esc(i.name)}" value="${D.esc(sn)}"> ${D.esc(sn)}</label>`).join("")}</small>`).join("")}</div>
				<input type="number" class="form-control input-xs" style="width:90px" min="0" max="${i.qty}" step="1"
					data-back-qty="${D.esc(i.name)}" value="0" ${hasSn(i) ? `readonly tabindex="-1" title="${__("Set by the serials ticked")}"` : ""}></div>`).join("")}</div>`);
		$body.on("change", "[data-back-sn]", (e) => {
			const row = o.items.find((i) => i.name === e.currentTarget.dataset.backSn);
			// A bundle has one serial per component per unit: the units back are the most ticked in one component.
			const qty = Math.max(0, ...row.serials.map((g) => g.serials.filter((sn) =>
				$body.find(`[data-back-sn="${row.name}"][value="${sn}"]`).prop("checked")).length));
			$body.find(`[data-back-qty="${row.name}"]`).val(Math.min(qty, row.qty));
		});
		d.show();
	}
}
