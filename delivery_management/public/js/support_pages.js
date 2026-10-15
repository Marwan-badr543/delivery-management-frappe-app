// delivery_management: the three Support pages (port of the old admin /support,
// /support/technician and /support/maint-tech). window.dm_support.SupportPage(view).
// A ticket is an ERPNext Issue; the rules are in customer_support/tickets.py.
(function () {
	const API = "delivery_management.api.support.";
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));

	function call(method, args, opts = {}) {
		return frappe
			.call({ method: API + method, args, type: opts.post ? "POST" : "GET", freeze: !!opts.freeze,
				freeze_message: opts.freeze_message })
			.then((r) => r.message);
	}

	const dt = (v) => (v ? moment(v).format("DD/MM/YYYY h:mm A") : "");
	const day = (v) => (v ? frappe.datetime.str_to_user(String(v).slice(0, 10)) : "");
	const toInput = (v) => (v ? moment(v).format("YYYY-MM-DDTHH:mm") : "");
	const fromInput = (v) => (v ? moment(v, "YYYY-MM-DDTHH:mm").format("YYYY-MM-DD HH:mm:00") : "");
	const nowInput = () => moment().format("YYYY-MM-DDTHH:mm");

	function pill(text, color) {
		return `<span class="dd-pill dd-pill-${color}">${esc(text)}</span>`;
	}
	const priorityPill = (p) => pill(__(p || "Medium"), { High: "red", Medium: "orange" }[p] || "blue");
	const stagePill = (s) => pill(__(s), { "Customer Service": "gray", Technician: "orange", "Maintenance Technician": "blue" }[s] || "gray");
	const statusPill = (r) => (r.resolved
		? `<span class="dd-pill dd-pill-green">${frappe.utils.icon("check", "xs")} ${__("Resolved")}</span>`
		: pill(__("Open"), "orange"));

	const VIEWS = {
		cs: {
			title: __("Support Tickets"), subtitle: __("Customer Service stage"), icon: "support",
			empty: __("No support tickets found"),
			search: __("Search by ticket, subject, customer or phone…"),
		},
		technician: {
			title: __("Technician Issues"), subtitle: __("Technician stage: your visits"), icon: "tool",
			empty: __("No assigned technician issues found"),
			search: __("Search assigned issues…"), dateFilter: true,
		},
		maintenance: {
			title: __("Maintenance Issues"), subtitle: __("Maintenance Technician stage: the workshop"), icon: "setting-gear",
			empty: __("No assigned maintenance issues found"),
			search: __("Search maintenance issues…"),
		},
	};

	class SupportPage {
		constructor(wrapper, view) {
			this.view = view;
			this.cfg = VIEWS[view];
			this.page = frappe.ui.make_app_page({ parent: wrapper, title: this.cfg.title, single_column: true });
			this.f = { search: "", status: "open", visit_date: "" };
			this.$root = $(`<div class="dd-page dm-support">
				<div class="dd-kpis"></div>
				<div class="dd-panel">
					<div class="dd-panel-head"><div><h4>${frappe.utils.icon(this.cfg.icon, "sm")} ${esc(this.cfg.title)}</h4>
						<p>${esc(this.cfg.subtitle)}</p></div></div>
					<div class="dd-filters">
						<div class="dd-field" style="grid-column: span 2"><label>${__("Search")}</label>
							<input type="search" class="form-control" data-f="search" placeholder="${esc(this.cfg.search)}"></div>
						<div class="dd-field"><label>${__("Status")}</label><select class="form-control" data-f="status">
							<option value="open">${__("Open")}</option><option value="resolved">${__("Resolved")}</option>
							<option value="">${__("All")}</option></select></div>
						${this.cfg.dateFilter ? `<div class="dd-field"><label>${__("Visit date")}</label>
							<input type="date" class="form-control" data-f="visit_date"></div>` : ""}
					</div>
				</div>
				<div class="dd-panel"><div class="dd-scroll"><table class="dd-table dd-cards"><thead><tr>
					<th>${__("Ticket")}</th><th>${__("Customer")}</th><th>${__("Priority")}</th>
					<th>${view === "cs" ? __("Stage") : __("Visit")}</th><th>${__("Assigned")}</th><th>${__("Status")}</th>
				</tr></thead><tbody></tbody></table></div></div>
			</div>`).appendTo(this.page.main);

			this.page.set_secondary_action(__("Refresh"), () => this.load(true), "refresh");
			if (view === "cs") this.page.set_primary_action(__("New Ticket"), () => this.newTicket(), "add");
			this.page.add_menu_item(__("Issue list"), () => frappe.set_route("List", "Issue", { sm_stage: ["is", "set"] }));
			this.page.add_menu_item(__("Support settings (Delivery Settings)"), () => frappe.set_route("Form", "Delivery Settings"));

			let timer;
			this.$root.find("[data-f]").on("input change", (e) => {
				this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
				clearTimeout(timer);
				timer = setTimeout(() => this.load(), e.type === "input" ? 300 : 0);
			});
			this.$root.find("[data-f=status]").val(this.f.status);
		}

		show() {
			const opts = frappe.route_options || {};
			frappe.route_options = null;
			this.load().then(() => opts.ticket && this.openDetail(opts.ticket));
		}

		async load(feedback) {
			const $tbody = this.$root.find("tbody");
			if (!this.rows) $tbody.html(window.dm_drivers.skeleton(6, 4));
			const seq = (this.seq = (this.seq || 0) + 1);
			const res = await call("get_tickets", { view: this.view, ...this.f });
			if (seq !== this.seq) return;
			this.rows = res.rows;
			this.isAdmin = res.is_admin;
			if (this.view === "cs" && !res.can_create) this.page.clear_primary_action();
			this.render();
			if (feedback) frappe.show_alert({ message: __("Data refreshed"), indicator: "green" });
		}

		render() {
			const rows = this.rows;
			const open = rows.filter((r) => !r.resolved).length;
			const kpi = (cls, label, value, icon, sub) => `<div class="dd-kpi ${cls}"><div><span>${esc(label)}</span>
				<b>${value}</b><small>${esc(sub)}</small></div><div class="dd-kpi-icon">${frappe.utils.icon(icon, "lg")}</div></div>`;
			this.$root.find(".dd-kpis").html(
				kpi("dd-kpi-tv", __("Active tickets"), open, "clock", __("Not resolved yet"))
				+ kpi("dd-kpi-all", __("Resolved"), rows.length - open, "check", __("In this list"))
				+ kpi("dd-kpi-ac", __("High priority"), rows.filter((r) => r.priority === "High").length, "solid-warning", __("Any status")));

			const $tbody = this.$root.find("tbody");
			if (!rows.length) {
				$tbody.html(window.dm_drivers.emptyRow(6, this.cfg.empty, __("Change the filters or refresh.")));
				return;
			}
			const who = (r) => {
				const parts = [];
				if (r.technician_name) parts.push(`${frappe.utils.icon("tool", "xs")} ${esc(r.technician_name)}`);
				if (r.maint_technician_name && r.stage === "Maintenance Technician")
					parts.push(`${frappe.utils.icon("settings", "xs")} ${esc(r.maint_technician_name)}`);
				return parts.join("<br>") || `<span class="dd-muted">${__("Not assigned")}</span>`;
			};
			const fourth = (r) => {
				if (this.view === "cs") return stagePill(r.stage);
				const when = r.visit_datetime ? dt(r.visit_datetime) : `<span class="dd-muted">${__("Not set")}</span>`;
				const extra = this.view === "maintenance"
					? pill(r.has_maint_report ? __("Reported") : __("Pending report"), r.has_maint_report ? "green" : "gray") : "";
				return `<span class="dd-when">${when}</span>${extra ? `<span class="dd-sub">${extra}</span>` : ""}`;
			};
			$tbody.html(rows.map((r) => `
				<tr data-name="${esc(r.name)}" tabindex="0">
					<td data-label="${__("Ticket")}"><b>${esc(r.subject)}</b><span class="dd-sub">${esc(r.name)} · ${day(r.creation)}</span></td>
					<td data-label="${__("Customer")}"><span>${esc(r.customer_name)}</span>
						<span class="dd-sub">${window.dm_drivers.phoneLink(r.customer_phone)}</span></td>
					<td data-label="${__("Priority")}">${priorityPill(r.priority)}</td>
					<td data-label="${this.view === "cs" ? __("Stage") : __("Visit")}">${fourth(r)}</td>
					<td data-label="${__("Assigned")}">${who(r)}</td>
					<td data-label="${__("Status")}">${statusPill(r)}</td>
				</tr>`).join(""));
			$tbody.find("tr[data-name]").on("click keydown", (e) => {
				if (e.type === "keydown" && e.key !== "Enter") return;
				this.openDetail($(e.currentTarget).data("name"));
			});
		}

		// ------------------------------------------------------------ new ticket

		newTicket() {
			const d = new frappe.ui.Dialog({ title: __("Open Support Ticket"), size: "extra-large",
				fields: [{ fieldname: "body", fieldtype: "HTML" }] });
			d.$wrapper.on("hidden.bs.modal", () => d.$wrapper.remove());
			const $b = d.fields_dict.body.$wrapper.html(`
				<div class="dd-detail">
					<div class="dm-sup-form">
						<div class="dd-field"><label>${__("Subject / Issue Title")} *</label>
							<input class="form-control" data-k="subject" placeholder="${__("e.g. AC Installation Delay")}"></div>
						<div class="dm-sup-row">
							<div class="dd-field"><label>${__("Customer Phone")} *</label>
								<input class="form-control" data-k="customer_phone" inputmode="tel" placeholder="33XXXXXX"></div>
							<div class="dd-field"><label>${__("Priority")}</label><select class="form-control" data-k="priority">
								<option value="Low">${__("Low")}</option><option value="Medium" selected>${__("Medium")}</option>
								<option value="High">${__("High")}</option></select></div>
						</div>
						<div class="dm-sup-customer dd-hint"></div>
						<div class="dm-sup-row">
							<div class="dd-field"><label>${__("Customer Email")}</label>
								<input class="form-control" type="email" data-k="customer_email" placeholder="customer@domain.com"></div>
							<div class="dd-field"><label>${__("Issue Type")}</label>
								<input class="form-control" data-k="issue_type" list="dm-issue-types" placeholder="${__("e.g. Black Screen")}">
								<datalist id="dm-issue-types"></datalist></div>
						</div>
						<div class="dm-sup-row">
							<div class="dd-field"><label>${__("Item Code")}</label>
								<input class="form-control" data-k="item_code" placeholder="${__("Pick it from the orders, or type it")}"></div>
							<div class="dd-field"><label>${__("Serial No")}</label>
								<input class="form-control" data-k="serial_no" placeholder="${__("Filled from the order")}"></div>
						</div>
						<div class="dd-field"><label>${__("Visit Location Address")}</label>
							<input class="form-control" data-k="address" placeholder="${__("Building, Road, Block, Area")}"></div>
						<div class="dd-field"><label>${__("Scheduled Visit Date/Time")}</label>
							<input class="form-control" type="datetime-local" data-k="visit_datetime" min="${nowInput()}"></div>
						<div class="dd-field"><label>${__("Issue Description")} *</label>
							<textarea class="form-control" rows="3" data-k="description" placeholder="${__("Describe the problem in detail...")}"></textarea></div>
						<div class="dd-field"><label>${__("Internal CS Note / Report")} *</label>
							<textarea class="form-control" rows="3" data-k="report" placeholder="${__("Write internal support notes...")}"></textarea></div>
						<div class="dm-sup-picked small dd-muted"></div>
					</div>
					<div class="dd-box dm-sup-orders"><h5>${__("Customer Past Orders")}</h5>
						<div class="dd-muted small">${__("Type the customer's phone (8 digits or more) to see the orders. Click an item to fill the ticket.")}</div></div>
				</div>`);
			const val = (k) => ($b.find(`[data-k=${k}]`).val() || "").trim();
			const state = { customer: null, sales_invoice: null };
			frappe.db.get_list("Issue Type", { pluck: "name", limit: 200 }).then((types) =>
				$b.find("#dm-issue-types").html(types.map((t) => `<option value="${esc(t)}">`).join("")));

			let timer;
			$b.find("[data-k=customer_phone]").on("input", () => {
				clearTimeout(timer);
				state.customer = null;
				const phone = val("customer_phone");
				if (phone.length < 8) {
					$b.find(".dm-sup-customer").empty();
					return;
				}
				timer = setTimeout(async () => {
					const res = await call("lookup_customer", { phone });
					if (val("customer_phone") !== phone) return;
					state.customer = res.customer;
					$b.find(".dm-sup-customer").html(res.customer
						? `${frappe.utils.icon("users", "xs")} <b>${esc(res.customer_name)}</b> (${esc(res.customer)})`
						: `<span class="text-danger">${__("No customer with this phone. Create the customer first.")}</span>`);
					this.renderOrders($b.find(".dm-sup-orders"), res.orders || [], (order, item, serial) => {
						$b.find("[data-k=item_code]").val(item.item_code);
						$b.find("[data-k=serial_no]").val(serial || "");
						if (order.address) $b.find("[data-k=address]").val(order.address);
						state.sales_invoice = order.name;
						$b.find(".dm-sup-picked").html(__("Linked to invoice {0}", [`<b>${esc(order.name)}</b>`]));
						frappe.show_alert({ message: __("Selected {0} and loaded the delivery address", [item.item_code]), indicator: "green" });
					});
				}, 350);
			});
			$b.find("[data-k=item_code]").on("input", () => {
				// Typed by hand: the order link and serial no longer apply.
				state.sales_invoice = null;
				$b.find("[data-k=serial_no]").val("");
				$b.find(".dm-sup-picked").empty();
			});

			d.set_primary_action(__("Create Ticket"), async () => {
				const missing = [["subject", __("Subject")], ["customer_phone", __("Customer Phone")],
					["description", __("Issue Description")], ["report", __("Internal CS Note / Report")]]
					.filter(([k]) => !val(k)).map(([, l]) => l);
				if (missing.length) return frappe.msgprint(__("Please fill: {0}", [missing.join(", ")]));
				const data = {
					subject: val("subject"), customer_phone: val("customer_phone"), priority: val("priority"),
					customer_email: val("customer_email"), issue_type: val("issue_type"), item_code: val("item_code"),
					serial_no: val("serial_no"), address: val("address"), visit_datetime: fromInput(val("visit_datetime")),
					description: val("description"), report: val("report"),
					customer: state.customer, sales_invoice: state.sales_invoice,
				};
				const r = await call("create_ticket", { data }, { post: true, freeze: true });
				d.hide();
				frappe.show_alert({ message: __("Support ticket {0} created", [r.name]), indicator: "green" });
				await this.load();
			});
			d.show();
		}

		renderOrders($box, orders, onPick) {
			const head = `<h5>${__("Customer Past Orders")}</h5>`;
			if (!orders.length) {
				$box.html(`${head}<div class="dd-muted small">${__("No order history found for this phone.")}</div>`);
				return;
			}
			$box.html(head + orders.map((o, oi) => `
				<div class="dm-sup-order">
					<div class="dm-sup-order-head"><b>${esc(o.name)}</b><span>${day(o.posting_date)}</span>
						<span>${format_currency(o.grand_total, o.currency)}</span>${o.status ? pill(__(o.status), "gray") : ""}</div>
					${o.address ? `<div class="small dd-muted">${frappe.utils.icon("map-pin", "xs")} ${esc(o.address)}</div>` : ""}
					<div class="dm-sup-items">${(o.items || []).map((it, ii) => {
						const serials = it.serials && it.serials.length ? it.serials : [null];
						return serials.map((sn) => `<button class="btn btn-xs btn-default" ${onPick ? "" : "disabled"}
							data-o="${oi}" data-i="${ii}" data-sn="${esc(sn || "")}">
							${it.is_component ? "↳ " : ""}${esc(it.item_code)} · ${__("Qty")}: ${it.qty}${sn ? ` · ${esc(sn)}` : ""}</button>`).join("");
					}).join("") || `<span class="dd-muted small">${__("No items listed")}</span>`}</div>
				</div>`).join(""));
			if (onPick)
				$box.find("button[data-o]").on("click", (e) => {
					const $t = $(e.currentTarget);
					const order = orders[$t.data("o")];
					onPick(order, order.items[$t.data("i")], $t.data("sn") || null);
				});
		}

		// ------------------------------------------------------------ detail

		async openDetail(name) {
			const d = new frappe.ui.Dialog({ title: __("Ticket {0}", [name]), size: "extra-large",
				fields: [{ fieldname: "body", fieldtype: "HTML" }] });
			d.$wrapper.on("hidden.bs.modal", () => d.$wrapper.remove());
			const $b = d.fields_dict.body.$wrapper.html(`<div class="dd-empty">${__("Loading ticket…")}</div>`);
			d.show();
			const reopen = async (msg) => {
				if (msg) frappe.show_alert({ message: msg, indicator: "green" });
				d.hide();
				await this.load();
				this.openDetail(name);
			};
			const close = async (msg) => {
				frappe.show_alert({ message: msg, indicator: "green" });
				d.hide();
				await this.load();
			};
			const t = await call("get_ticket_detail", { name });
			d.set_title(`${t.subject} · ${t.name}`);

			const kv = (pairs) => `<div class="dd-kv">${pairs.filter(([, v]) => v).map(([k, v]) =>
				`<span>${esc(k)}</span><b>${v}</b>`).join("")}</div>`;
			const link = (doctype, n) => (n ? `<a href="/desk/${frappe.router.slug(doctype)}/${encodeURIComponent(n)}" target="_blank">${esc(n)}</a>` : "");
			const maps = t.address ? `<a class="small" target="_blank" rel="noopener"
				href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(t.address + ", Bahrain")}">
				${frappe.utils.icon("map-pin", "xs")} ${__("Open in Google Maps")}</a>` : "";
			const header = `<div class="dm-sup-head">${statusPill(t)} ${priorityPill(t.priority)} ${stagePill(t.stage)}
				${t.technician_name ? pill(`${__("Tech")}: ${t.technician_name}`, "orange") : ""}
				${t.maint_technician_name && t.stage === "Maintenance Technician" ? pill(`${__("Maint")}: ${t.maint_technician_name}`, "blue") : ""}
				<span class="dd-muted small">${__("Created")} ${dt(t.creation)}${t.cs_user_name ? " · " + esc(t.cs_user_name) : ""}</span></div>`;
			const customerBox = `<div class="dd-box"><h5>${__("Customer details")}</h5>${kv([
				[__("Name"), esc(t.customer_name)], [__("Phone"), window.dm_drivers.phoneLink(t.customer_phone)],
				[__("Email"), esc(t.customer_email)], [__("Item"), t.item_code ? `${esc(t.item_code)}${t.item_name && t.item_name !== t.item_code ? " · " + esc(t.item_name) : ""}` : ""],
				[__("Serial No"), link("Serial No", t.serial_no)], [__("Issue Type"), esc(t.issue_type)],
				[__("Sales Invoice"), link("Sales Invoice", t.sales_invoice)], [__("Delivery Note"), link("Delivery Note", t.delivery_note)],
				[__("Visit"), esc(dt(t.visit_datetime))], [__("Address"), esc(t.address)],
			])}${maps}</div>`;
			const descBox = `<div class="dd-box"><h5>${__("Customer description")}</h5><div class="dm-sup-text">${esc(t.description) || "—"}</div></div>`;
			const csReportBox = (editable) => `<div class="dd-box"><h5>${__("Customer service report")}</h5>
				${editable ? `<textarea class="form-control" rows="3" data-k="cs_report">${esc(t.cs_report)}</textarea>
					<div><button class="btn btn-default btn-sm" data-act="cs-report">${__("Update Report")}</button></div>`
					: `<div class="dm-sup-text">${esc(t.cs_report) || `<span class="dd-muted">${__("No CS notes provided")}</span>`}</div>`}</div>`;
			const visitsBox = (editable) => `<div class="dd-box"><h5>${__("Technician visits")}</h5>${t.visits.length
				? t.visits.map((v) => `<div class="dm-sup-visit" data-row="${esc(v.name)}">
					<div class="dm-sup-visit-head"><b>${dt(v.visit_on)}</b> ${esc(v.technician_name || v.technician)}
						${pill(__(v.completion_status || ""), v.completion_status === "Fully Completed" ? "green" : "orange")}
						${v.maintenance_visit ? link("Maintenance Visit", v.maintenance_visit) : ""}</div>
					<div class="dm-sup-text" data-text>${esc(v.report)}</div>
					${v.warranty_claim ? `<div class="small">${__("Warranty Claim")}: ${link("Warranty Claim", v.warranty_claim)}</div>` : ""}
					${editable && v.can_edit ? `<button class="btn btn-xs btn-default" data-act="edit-visit">${__("Edit Report")}</button>` : ""}
				</div>`).join("")
				: `<div class="dd-muted small">${__("No technician visits logged yet.")}</div>`}</div>`;
			const resolveBtn = t.can.resolve ? `<button class="btn btn-success btn-sm" data-act="resolve">
				${frappe.utils.icon("check", "xs")} ${__("Resolve Ticket")}</button>` : "";
			const issueBtn = `<a class="btn btn-default btn-sm" href="/desk/issue/${encodeURIComponent(t.name)}" target="_blank">${__("Open Issue")}</a>`;

			let left = customerBox + descBox;
			let right = "";
			if (this.view === "cs") {
				right += `<div class="dd-box"><h5>${__("Schedule & address")}</h5>
					<div class="dd-field"><label>${__("Visit Address")}</label><input class="form-control" data-k="address" value="${esc(t.address)}"
						${t.can.update_schedule ? "" : "disabled"}></div>
					<div class="dd-field"><label>${__("Scheduled Visit Time")}</label><input class="form-control" type="datetime-local"
						data-k="visit_datetime" value="${toInput(t.visit_datetime)}" ${t.can.update_schedule ? "" : "disabled"}></div>
					${t.can.update_schedule ? `<div><button class="btn btn-default btn-sm" data-act="schedule">${__("Update Schedule / Address")}</button></div>` : ""}
				</div>`;
				right += csReportBox(t.can.update_cs_report);
				if (t.can.move_to_technician)
					right += `<div class="dd-box"><h5>${__("Assign technician")}</h5>
						<div class="small dd-muted dm-sup-techday"></div><div class="dd-pick-list dm-sup-techs"></div>
						<div><button class="btn btn-warning btn-sm" data-act="move-tech">${__("Assign & Move to Technician")}</button></div></div>`;
				if (t.stage !== "Customer Service") right += visitsBox(false);
				left += `<div class="dd-box dm-sup-history"><h5>${__("Order history")}</h5><div class="dd-muted small">${__("Loading…")}</div></div>`;
			} else if (this.view === "technician") {
				left += csReportBox(false) + visitsBox(true);
				if (t.can.log_visit)
					right += `<div class="dd-box"><h5>${__("Log maintenance visit")}</h5>
						<div class="dd-field"><label>${__("Completion Status")} *</label><select class="form-control" data-k="completion_status">
							<option value="Fully Completed">${__("Fully Completed")}</option>
							<option value="Partially Completed">${__("Partially Completed")}</option></select></div>
						<div class="dd-field"><label>${__("Technician Visit Report")} *</label><textarea class="form-control" rows="4" data-k="visit_report"
							placeholder="${__("Write details of work done, issues identified, parts replaced etc...")}"></textarea></div>
						<div><button class="btn btn-primary btn-sm" data-act="log-visit">${__("Log Visit (Maintenance Visit + Warranty)")}</button></div></div>`;
				if (t.can.move_to_maintenance)
					right += `<div class="dd-box"><h5>${__("Escalate to maintenance technician")}</h5>
						<div class="small dd-muted">${__("Reserve a pickup delivery slot (optional)")}</div>
						<div class="dm-sup-row">
							<div class="dd-field"><label>${__("1. Date")}</label><input class="form-control" type="date" data-k="pickup_date"
								min="${frappe.datetime.get_today()}"></div>
							<div class="dd-field"><label>${__("2. Area")}</label><select class="form-control" data-k="pickup_area"></select></div>
						</div>
						<div class="dd-field"><label>${__("3. Available slots")}</label><div class="dm-sup-slots dd-muted small">
							${__("Select area & date to load available delivery slots.")}</div></div>
						<div class="dd-field"><label>${__("Maintenance Technician")} *</label><select class="form-control" data-k="maint_tech">
							<option value="">${__("-- Choose maintenance technician --")}</option></select></div>
						<div><button class="btn btn-warning btn-sm" data-act="escalate">${__("Assign & Escalate Stage")}</button></div></div>`;
				if (t.pickup) right += `<div class="dd-box"><h5>${__("Workshop pickup")}</h5>${kv([[__("Area"), esc(t.pickup.area)],
					[__("Date"), day(t.pickup.date)], [__("Time"), esc(String(t.pickup.time || "").slice(0, 5))]])}</div>`;
			} else {
				left += csReportBox(false) + visitsBox(false);
				right += `<div class="dd-box"><h5>${__("Maintenance report")}</h5>
					<div class="dm-sup-maint-view"><div class="dm-sup-text">${esc(t.maint_report) || `<span class="dd-muted">${__("No report written yet.")}</span>`}</div>
						${t.maint_report_by_name ? `<div class="small dd-muted">${esc(t.maint_report_by_name)}</div>` : ""}
						${t.can.maint_report ? `<div><button class="btn btn-default btn-sm" data-act="edit-maint">${__("Edit Report")}</button></div>` : ""}</div>
					<div class="dm-sup-maint-edit" style="display:none">
						<textarea class="form-control" rows="5" data-k="maint_report"
							placeholder="${__("Write final diagnostic findings, parts replacement, actions taken to finalize this maintenance ticket...")}">${esc(t.maint_report)}</textarea>
						<div class="dd-btn-row">${t.maint_report ? `<button class="btn btn-default btn-sm" data-act="cancel-maint">${__("Cancel")}</button>` : ""}
							<button class="btn btn-primary btn-sm" data-act="save-maint">${__("Save Report")}</button></div></div></div>`;
				if (t.pickup) right += `<div class="dd-box"><h5>${__("Workshop pickup")}</h5>${kv([[__("Area"), esc(t.pickup.area)],
					[__("Date"), day(t.pickup.date)], [__("Time"), esc(String(t.pickup.time || "").slice(0, 5))]])}</div>`;
			}

			$b.html(`${header}<div class="dd-detail">
				<div class="dm-sup-col">${left}</div><div class="dm-sup-col">${right}</div></div>
				<div class="dd-btn-row dm-sup-foot">${issueBtn}${resolveBtn}</div>`);
			const val = (k) => ($b.find(`[data-k=${k}]`).val() || "").trim();
			const act = (a, fn) => $b.find(`[data-act=${a}]`).on("click", fn);

			// Maintenance: no report yet = start in edit mode, as the old page.
			if (this.view === "maintenance" && t.can.maint_report && !t.maint_report) {
				$b.find(".dm-sup-maint-view").hide();
				$b.find(".dm-sup-maint-edit").show();
			}

			act("resolve", () => {
				const need = {
					"Customer Service": !(this.view === "cs" ? val("cs_report") || t.cs_report : t.cs_report)
						&& __("A customer service report is required before resolving this ticket."),
					Technician: !t.visits.some((v) => (v.report || "").trim())
						&& __("A technician report is required before resolving this ticket. Please log a maintenance visit report first."),
					"Maintenance Technician": !(t.maint_report || "").trim()
						&& __("A maintenance technician report is required before resolving this ticket. Please save the maintenance report first."),
				}[t.stage];
				if (need) return frappe.msgprint(need);
				frappe.confirm(__("Are you sure you want to resolve this ticket?"), async () => {
					const r = await call("resolve", { name }, { post: true, freeze: true });
					const claims = (r.warranty_claims_closed || []).length;
					close(claims ? __("Ticket resolved, {0} warranty claim(s) closed", [claims]) : __("Ticket resolved successfully"));
				});
			});
			act("schedule", async () => {
				const r = await call("update_schedule", { name, address: val("address"), visit_datetime: fromInput(val("visit_datetime")) },
					{ post: true, freeze: true });
				reopen(r.changed.length ? __("Ticket updated successfully") : __("Nothing changed"));
			});
			act("cs-report", async () => {
				await call("update_cs_report", { name, report: val("cs_report") }, { post: true, freeze: true });
				reopen(__("Customer service report updated"));
			});

			// Customer service: technicians of the visit day with their visits (±gap unavailable).
			if (this.view === "cs" && t.can.move_to_technician) {
				let chosen = t.technician || null;
				const loadTechs = async () => {
					const when = fromInput(val("visit_datetime"));
					const res = await call("get_technician_options", { name, visit_datetime: when || null,
						day: when ? null : frappe.datetime.get_today() });
					$b.find(".dm-sup-techday").html(__("Visits on {0}. Busy = another visit within {1} minutes of {2}.",
						[`<b>${day(res.date)}</b>`, res.gap_minutes, when ? moment(when).format("h:mm A") : __("the chosen time")]));
					$b.find(".dm-sup-techs").html(res.technicians.length ? res.technicians.map((o) => `
						<button class="dd-pick ${o.user === chosen ? "current" : ""}" data-user="${esc(o.user)}" ${o.available ? "" : "disabled"}>
							${window.dm_drivers.avatar(o.full_name)}<span class="dd-pick-body"><b>${esc(o.full_name)}</b>
							<small class="${o.available ? "dd-ok" : "dd-no"}">${o.available ? __("{0} visits", [o.number_of_visits]) : esc(o.reason)}</small>
							<small class="dd-muted">${o.visit_times.map(esc).join(" · ")}</small></span></button>`).join("")
						: `<div class="dd-muted small">${__("No user has the Technician role.")}</div>`);
					$b.find(".dm-sup-techs .dd-pick").on("click", (e) => {
						chosen = $(e.currentTarget).data("user");
						$b.find(".dm-sup-techs .dd-pick").removeClass("current");
						$(e.currentTarget).addClass("current");
					});
				};
				loadTechs();
				$b.find("[data-k=visit_datetime]").on("change", loadTechs);
				act("move-tech", async () => {
					if (!val("cs_report"))
						return frappe.msgprint(__("A customer service report is required before moving to the technician stage. Please write and update the report first."));
					if (!chosen) return frappe.msgprint(__("Please select a technician first"));
					await call("move_to_technician", { name, technician: chosen }, { post: true, freeze: true });
					close(__("Ticket moved to technician stage"));
				});
			}
			if (this.view === "cs") {
				call("lookup_customer", { customer: t.customer }).then((res) =>
					this.renderOrders($b.find(".dm-sup-history"), res.orders || [], null)).catch(() =>
					$b.find(".dm-sup-history").html(`<h5>${__("Order history")}</h5><div class="dd-muted small">${__("Failed to load customer orders")}</div>`));
			}

			// Technician: visit log, editing a visit report, escalation with a pickup slot.
			act("log-visit", async () => {
				if (!val("visit_report")) return frappe.msgprint(__("Please fill in the technician visit report"));
				const r = await call("log_visit", { name, completion_status: val("completion_status"), report: val("visit_report") },
					{ post: true, freeze: true, freeze_message: __("Creating the Maintenance Visit…") });
				reopen(r.warranty_claim
					? __("Maintenance Visit {0} logged, Warranty Claim {1}", [r.maintenance_visit, r.warranty_claim])
					: __("Maintenance Visit {0} logged", [r.maintenance_visit]));
			});
			$b.find("[data-act=edit-visit]").on("click", (e) => {
				const $v = $(e.currentTarget).closest("[data-row]");
				const row = $v.data("row");
				const old = t.visits.find((v) => v.name === row).report;
				$v.find("[data-text]").html(`<textarea class="form-control" rows="3">${esc(old)}</textarea>
					<div class="dd-btn-row"><button class="btn btn-xs btn-default" data-cancel>${__("Cancel")}</button>
					<button class="btn btn-xs btn-primary" data-save>${__("Save Change")}</button></div>`);
				$(e.currentTarget).hide();
				$v.find("[data-cancel]").on("click", () => reopen());
				$v.find("[data-save]").on("click", async () => {
					await call("update_visit_report", { name, row, report: $v.find("textarea").val() }, { post: true, freeze: true });
					reopen(__("Technician note updated"));
				});
			});
			if (this.view === "technician" && t.can.move_to_maintenance) {
				let slot = null;
				call("get_maintenance_technicians").then((list) => $b.find("[data-k=maint_tech]").append(list.map((u) =>
					`<option value="${esc(u.user)}">${esc(u.full_name)}</option>`).join("")));
				call("get_pickup_options").then((r) => $b.find("[data-k=pickup_area]").html(
					`<option value="">${__("-- Area --")}</option>` + r.areas.map((a) => `<option>${esc(a)}</option>`).join("")));
				const loadSlots = async () => {
					slot = null;
					const area = val("pickup_area");
					const date = val("pickup_date");
					const $s = $b.find(".dm-sup-slots");
					if (!area || !date) return $s.html(__("Select area & date to load available delivery slots."));
					$s.html(__("Loading slots..."));
					const r = await call("get_pickup_options", { area, date });
					$s.html(r.slots.length ? `<div class="dm-sup-slot-list">${r.slots.map((s) =>
						`<button class="btn btn-sm btn-default" data-time="${esc(s.delivery_time)}" ${s.available ? "" : "disabled"}>
							${moment(s.time, "HH:mm").format("h:mm A")}${s.available ? "" : ` · ${__("Fully Booked")}`}</button>`).join("")}</div>`
						: __("No slots available. Try another date."));
					$s.find("button[data-time]").on("click", (e) => {
						slot = $(e.currentTarget).data("time");
						$s.find("button").removeClass("btn-primary").addClass("btn-default");
						$(e.currentTarget).removeClass("btn-default").addClass("btn-primary");
					});
				};
				$b.find("[data-k=pickup_area], [data-k=pickup_date]").on("change", loadSlots);
				act("escalate", () => {
					if (!t.visits.some((v) => (v.report || "").trim()))
						return frappe.msgprint(__("A technician report is required before escalating. Please log a maintenance visit report first."));
					if (!val("maint_tech")) return frappe.msgprint(__("Please select a maintenance technician"));
					const go = async () => {
						await call("move_to_maintenance", { name, maint_technician: val("maint_tech"),
							area: slot ? val("pickup_area") : null, delivery_date: slot ? val("pickup_date") : null, delivery_time: slot },
							{ post: true, freeze: true });
						close(__("Ticket moved to maintenance technician stage"));
					};
					if (slot) return go();
					frappe.confirm(__("You didn't reserve a delivery slot for this. Are you sure you want to escalate without a reservation?"), go);
				});
			}

			// Maintenance report.
			act("edit-maint", () => { $b.find(".dm-sup-maint-view").hide(); $b.find(".dm-sup-maint-edit").show(); });
			act("cancel-maint", () => { $b.find(".dm-sup-maint-edit").hide(); $b.find(".dm-sup-maint-view").show(); });
			act("save-maint", async () => {
				if (!val("maint_report")) return frappe.msgprint(__("Report content cannot be empty"));
				await call("save_maint_report", { name, report: val("maint_report") }, { post: true, freeze: true });
				reopen(t.maint_report ? __("Maintenance report updated successfully") : __("Maintenance report created and logged successfully"));
			});
		}
	}

	window.dm_support = { SupportPage };
})();
