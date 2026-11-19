// delivery_management: the Support pages and the ticket dialog used everywhere.
// * window.dm_support.SupportPage(wrapper, view): Support Tickets (cs), Technician Issues
//   (technician), Maintenance Issues (maintenance).
// * window.dm_support.openTicket(name, onChange): the ticket with the buttons of its step
//   (also opened from Driver Assignment, Delivery Schedule and Goods in Transit).
// The rules are in customer_support/tickets.py and flow.py.
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
	const hm = (v) => (v ? moment(String(v).slice(0, 5), "HH:mm").format("h:mm A") : "");

	function pill(text, color) {
		return `<span class="dd-pill dd-pill-${color}">${esc(text)}</span>`;
	}
	const priorityPill = (p) => pill(__(p || "Medium"), { High: "red", Medium: "orange" }[p] || "blue");
	const STEP_COLOR = {
		"Customer Service": "gray", Pickup: "orange", "To Workshop": "blue", "Under Repair": "blue",
		"Ready for Return": "purple", Return: "orange", "Out for Return": "orange", "Back to Store": "red", Closed: "green",
	};
	// The journey, as the person sees it. "Back to Store" sits on the "On the way" stop.
	const JOURNEY = [
		["Customer Service", __("Call")], ["Pickup", __("Visit")], ["To Workshop", __("To workshop")],
		["Under Repair", __("Repair")], ["Ready for Return", __("Book return")], ["Return", __("Driver")],
		["Out for Return", __("On the way")], ["Closed", __("Done")],
	];
	function journey(step) {
		const at = JOURNEY.findIndex(([s]) => s === (step === "Back to Store" ? "Out for Return" : step || "Customer Service"));
		return `<ol class="dm-journey">${JOURNEY.map(([, label], i) => `<li class="${i < at ? "done" : i === at
			? (step === "Back to Store" ? "now bad" : "now") : ""}"><span class="dot">${i < at ? "✓" : i + 1}</span>
			<span class="lbl">${esc(i === at && step === "Back to Store" ? __("Coming back") : label)}</span></li>`).join("")}</ol>`;
	}
	/** One line: who acts next and what they do. */
	function nextStep(t) {
		const drv = esc(t.driver_name || t.driver || "");
		const from = t.take_from ? esc(t.take_from) : "";
		switch (t.step || "Customer Service") {
			case "Customer Service": return __("Customer service: solve it on the phone (Resolve), or book a visit (Assign to Tech).");
			case "Pickup": return t.driver ? __("Driver {0}: visit the customer, write the report, then Resolve or Fetch Item.", [drv])
				: __("Delivery office: assign a driver in Driver Assignment.");
			case "To Workshop": return __("The driver brings the item. Workshop: check the serial and receive it.");
			case "Under Repair": return __("Workshop: repair it, upload a photo or video, then Finish Repair.");
			case "Ready for Return": return __("Customer service: call the customer and book the return.")
				+ (from ? " " + __("The item is in {0}.", [`<b>${from}</b>`]) : "");
			case "Return": return t.driver ? __("Driver {0}: collect the item from {1}.", [drv, `<b>${from || "—"}</b>`])
				: __("Delivery office: assign a driver in Driver Assignment.");
			case "Out for Return": return __("Driver {0}: hand it over (signature + photo), or Customer not home.", [drv]);
			case "Back to Store": return __("Driver {0} brings the item back. Stock keeper: scan it into the Customer Items Store (Stock Keeper page).", [drv]);
			default: return __("Done. Nothing more to do.");
		}
	}
	const stepPill = (s) => pill(__(s || "Customer Service"), STEP_COLOR[s] || "gray");
	const warrantyPill = (r) => (r.under_warranty
		? pill(__("Under warranty until {0}", [day(r.warranty_expiry)]), "green")
		: pill(r.warranty_expiry ? __("Warranty ended {0}", [day(r.warranty_expiry)]) : __("No warranty"), "gray"));
	const tripText = (r) => (r.trip_date ? `${day(r.trip_date)} · ${hm(r.trip_time)}` : "");

	function readFile(file) {
		return new Promise((resolve, reject) => {
			if (file.size > 25 * 1024 * 1024) return reject(__("The file is too large (max 25 MB)."));
			const reader = new FileReader();
			reader.onload = () => resolve(reader.result);
			reader.onerror = () => reject(__("Could not read the file."));
			reader.readAsDataURL(file);
		});
	}

	// ---------------------------------------------------------------- slot picker (as the POS)

	/** Date, area and the free delivery slots. Resolves to {get(): {area, delivery_date, delivery_time} | null}. */
	async function slotPicker($box, area) {
		$box.html(`<div class="dm-sup-row">
				<div class="dd-field"><label>${__("Date")} *</label><input class="form-control" type="date" data-s="date"
					min="${frappe.datetime.get_today()}"></div>
				<div class="dd-field"><label>${__("Area")} *</label><select class="form-control" data-s="area"></select></div>
			</div>
			<div class="dd-field"><label>${__("Time")} *</label><div class="dm-sup-slots dd-muted small">
				${__("Choose the date and the area to see the free times.")}</div></div>`);
		const areas = await call("get_areas");
		$box.find("[data-s=area]").html(`<option value="">${__("-- Area --")}</option>`
			+ areas.map((a) => `<option ${a === area ? "selected" : ""}>${esc(a)}</option>`).join(""));
		let time = null;
		const load = async () => {
			time = null;
			const a = $box.find("[data-s=area]").val();
			const d = $box.find("[data-s=date]").val();
			const $s = $box.find(".dm-sup-slots");
			if (!a || !d) return $s.html(__("Choose the date and the area to see the free times."));
			$s.html(__("Loading slots..."));
			const r = await call("get_slot_options", { area: a, date: d });
			$s.html(r.slots.length ? `<div class="dm-sup-slot-list">${r.slots.map((s) =>
				`<button class="btn btn-sm btn-default" data-time="${esc(s.delivery_time)}" ${s.available ? "" : "disabled"}>
					${hm(s.time)}${s.available ? "" : ` · ${__("Full")}`}</button>`).join("")}</div>`
				: __("No slots on this day. Try another date."));
			$s.find("button[data-time]").on("click", (e) => {
				time = $(e.currentTarget).data("time");
				$s.find("button").removeClass("btn-primary").addClass("btn-default");
				$(e.currentTarget).removeClass("btn-default").addClass("btn-primary");
			});
		};
		$box.find("[data-s]").on("change", load);
		return {
			get: () => (time ? { area: $box.find("[data-s=area]").val(), delivery_date: $box.find("[data-s=date]").val(),
				delivery_time: time } : null),
		};
	}

	function bookTrip(t, done) {
		const ret = t.step === "Ready for Return";
		const d = new frappe.ui.Dialog({ title: ret ? __("Book the return of {0}", [t.name]) : __("Book the visit of {0}", [t.name]),
			fields: [{ fieldname: "body", fieldtype: "HTML" }] });
		d.$wrapper.on("hidden.bs.modal", () => d.$wrapper.remove());
		const $b = d.fields_dict.body.$wrapper.html(`<p class="small dd-muted">${ret
			? __("Call the customer and choose when the driver brings the item back.")
			: __("Choose when we visit the customer. The delivery office then assigns a driver.")}</p>
			<div class="dm-sup-slot-box"></div>
			<div class="dd-field"><label>${__("Your note: what you did and why")} *</label>
				<textarea class="form-control" rows="3" data-k="book_note"></textarea></div>`);
		const pickerP = slotPicker($b.find(".dm-sup-slot-box"), t.area);
		d.set_primary_action(ret ? __("Book Return") : __("Assign to Tech"), async () => {
			const slot = (await pickerP).get();
			if (!slot) return frappe.msgprint(__("Choose the date, the area and a free time."));
			const note = ($b.find("[data-k=book_note]").val() || "").trim();
			if (!note) return frappe.msgprint(__("Write your note: what you did and why."));
			await call("book_trip", { name: t.name, ...slot, note }, { post: true, freeze: true });
			d.hide();
			done(ret ? __("Return booked") : __("Visit booked: waiting for a driver"));
		});
		d.show();
	}

	// ---------------------------------------------------------------- the ticket dialog

	const FILE_LABELS = [
		["Workshop repair proof", "repair"], ["Return hand-over photo-video", "customer"], ["Customer signature at return", "signature"],
	];
	const kindOfFile = (f) => (FILE_LABELS.find(([p]) => f.file_name.startsWith(p)) || [])[1];

	/** Ask "are you sure?" before an important action. */
	const sure = (msg) => new Promise((resolve) => frappe.confirm(msg, () => resolve(true), () => resolve(false)));

	/**
	 * The ticket. ``view`` is the page it is opened from: "cs" (Support Tickets), "technician"
	 * (Technician Issues), "maintenance" (Maintenance Issues), "schedule" (Delivery Schedule).
	 * Each page shows only its own buttons; from any other page the ticket is only shown.
	 */
	async function openTicket(name, onChange, view) {
		const d = new frappe.ui.Dialog({ title: __("Ticket {0}", [name]), size: "extra-large",
			fields: [{ fieldname: "body", fieldtype: "HTML" }] });
		d.$wrapper.on("hidden.bs.modal", () => d.$wrapper.remove());
		const $b = d.fields_dict.body.$wrapper.html(`<div class="dd-empty">${__("Loading ticket…")}</div>`);
		d.show();
		const changed = async (msg, keepOpen = true) => {
			if (msg) frappe.show_alert({ message: msg, indicator: "green" });
			d.hide();
			onChange && (await onChange());
			if (keepOpen) openTicket(name, onChange, view);
		};
		const t = await call("get_ticket_detail", { name, view: view || "" });
		d.set_title(`${t.subject} · ${t.name}`);
		const can = t.can;

		const kv = (pairs) => `<div class="dd-kv">${pairs.filter(([, v]) => v).map(([k, v]) =>
			`<span>${esc(k)}</span><b>${v}</b>`).join("")}</div>`;
		const link = (doctype, n) => (n ? `<a href="/desk/${frappe.router.slug(doctype)}/${encodeURIComponent(n)}" target="_blank">${esc(n)}</a>` : "");
		const maps = t.address ? `<a class="small" target="_blank" rel="noopener"
			href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(t.address + ", Bahrain")}">
			${frappe.utils.icon("map-pin", "xs")} ${__("Open in Google Maps")}</a>` : "";
		const header = `${journey(t.step)}
			<div class="dm-next">${frappe.utils.icon(t.resolved ? "check" : "arrow-right", "sm")}<div><b>${__("Next")}:</b> ${nextStep(t)}</div></div>
			<div class="dm-sup-head">${stepPill(t.step)} ${priorityPill(t.priority)} ${warrantyPill(t)}
			${t.repair_result ? pill(__(t.repair_result), t.repair_result === "Repaired" ? "green" : "red") : ""}
			<span class="dd-muted small">${__("Created")} ${dt(t.creation)}${t.cs_user_name ? " · " + esc(t.cs_user_name) : ""}</span></div>`;
		const customerBox = `<div class="dd-box"><h5>${__("Customer")}</h5>${kv([
			[__("Name"), esc(t.customer_name)], [__("Phone"), window.dm_drivers.phoneLink(t.customer_phone)],
			[__("Email"), esc(t.customer_email)], [__("Address"), esc(t.address)],
		])}${maps}</div>`;
		const itemBox = `<div class="dd-box"><h5>${__("Item")}</h5>${kv([
			[__("Item"), `${esc(t.item_code)}${t.item_name && t.item_name !== t.item_code ? " · " + esc(t.item_name) : ""}`],
			[__("Serial No"), t.serial_no ? link("Serial No", t.serial_no) : (t.has_serial ? `<span class="dd-no">${__("Not known yet")}</span>` : __("No serial"))],
			[__("Warranty"), warrantyPill(t)], [__("Issue Type"), esc(t.issue_type)],
			[__("Sales Invoice"), link("Sales Invoice", t.sales_invoice)], [__("Delivery Note"), link("Delivery Note", t.delivery_note)],
			[__("Now in"), t.warehouse ? `<b>${esc(t.warehouse)}</b>` : ""],
		])}</div>`;
		const tripBox = t.trip_date ? `<div class="dd-box"><h5>${t.step === "Pickup" ? __("Pickup trip") : __("Return trip")}</h5>${kv([
			[__("When"), esc(tripText(t))], [__("Area"), esc(t.area)],
			[__("Driver"), t.driver_name ? esc(t.driver_name) : `<span class="dd-no">${__("Not assigned yet")}</span>`],
			[__("Collect the item from"), t.take_from && t.step === "Return" ? `<b class="dd-no">${esc(t.take_from)}</b>` : ""],
		])}</div>` : "";
		const descBox = `<div class="dd-box"><h5>${__("Problem")}</h5><div class="dm-sup-text">${esc(t.description) || "—"}</div>
			${t.cs_report ? `<h5 style="margin-top:8px">${__("Customer service report")}</h5><div class="dm-sup-text">${esc(t.cs_report)}</div>` : ""}</div>`;
		const visitsBox = t.visits.length ? `<div class="dd-box"><h5>${__("Driver visit reports")}</h5>${t.visits.map((v) =>
			`<div class="dm-sup-visit" data-row="${esc(v.name)}">
				<div class="dm-sup-visit-head"><b>${dt(v.visit_on)}</b> ${esc(v.technician_name || v.technician)}
					${pill(__(v.completion_status || ""), v.completion_status === "Fully Completed" ? "green" : "orange")}
					${v.maintenance_visit ? link("Maintenance Visit", v.maintenance_visit) : ""}</div>
				<div class="dm-sup-text" data-text>${esc(v.report)}</div>
				${v.can_edit && !t.resolved ? `<button class="btn btn-xs btn-default" data-act="edit-visit">${__("Edit Report")}</button>` : ""}
			</div>`).join("")}</div>` : "";
		const workshopBox = t.maint_report && !can.repair ? `<div class="dd-box"><h5>${__("Workshop report")}</h5>
			<div class="dm-sup-text">${esc(t.maint_report)}</div>
			${t.maint_report_by_name ? `<div class="small dd-muted">${esc(t.maint_report_by_name)}</div>` : ""}</div>` : "";
		const filesBox = t.files.length ? `<div class="dd-box"><h5>${__("Photos, videos and signatures")}</h5>${t.files.map((f) =>
			`<div class="small"><a href="${esc(f.file_url)}" target="_blank">${esc(f.file_name)}</a>
				<span class="dd-muted">${dt(f.creation)}</span></div>`).join("")}</div>` : "";
		const historyBox = t.notes.length ? `<div class="dd-box"><h5>${__("Ticket history")}</h5>${t.notes.map((n) =>
			`<div class="dm-sup-visit"><div class="small dd-muted">${dt(n.creation)}</div><div class="dm-sup-text">${n.content}</div></div>`).join("")}</div>` : "";
		const count = (kind) => t.files.filter((f) => kindOfFile(f) === kind).length;
		const fileState = (kind, ok, missing) => `<b data-state="${kind}" class="${count(kind) ? "dd-ok" : "dd-no"}">
			${count(kind) ? `${ok} (${count(kind)})` : missing}</b>`;
		const serialField = (label) => (t.has_serial ? `<div class="dd-field"><label>${esc(label)} *</label>
			<input class="form-control" data-k="serial" value="${esc(t.serial_no || "")}" placeholder="${__("Scan or type the serial")}"></div>` : "");
		const noDriver = (what) => `<div class="dd-banner dd-banner-warn">${what}</div>`;

		// The buttons of this page and step.
		let actions = "";
		if (t.waiting_driver)
			actions += noDriver(__("No driver is assigned to this trip yet. The delivery office assigns one in Driver Assignment."));
		if (can.update_address)
			actions += `<div class="dd-box"><h5>${__("Address")}</h5><div class="dm-sup-row">
				<div class="dd-field"><label>${__("Area")} *</label><select class="form-control" data-a="area"></select></div>
				<div class="dd-field"><label>${__("Block")} *</label><input class="form-control" data-a="block_no" value="${esc(t.block_no)}"></div>
				<div class="dd-field"><label>${__("Road")} *</label><input class="form-control" data-a="road_no" value="${esc(t.road_no)}"></div>
				<div class="dd-field"><label>${__("Building")} *</label><input class="form-control" data-a="building_no" value="${esc(t.building_no)}"></div></div>
				<div class="dd-field"><label>${__("Full Address")} *</label><input class="form-control" data-a="address" value="${esc(t.address_line)}"></div>
				<div><button class="btn btn-default btn-xs" data-act="address">${__("Save Address")}</button></div></div>`;
		if (can.book_pickup)
			actions += `<div class="dd-box"><h5>${__("Pickup visit")}</h5><p class="small dd-muted">${__("The call did not solve it: book a visit.")}</p>
				<div><button class="btn btn-warning btn-sm" data-act="book">${frappe.utils.icon("tool", "xs")} ${__("Assign to Tech")}</button></div></div>`;
		if (can.book_return)
			actions += `<div class="dd-box"><h5>${__("Return to the customer")}</h5><p class="small dd-muted">${__("Call the customer and book the return.")}</p>
				<div><button class="btn btn-primary btn-sm" data-act="book">${__("Book Return")}</button></div></div>`;
		if (can.fetch_item)
			actions += `<div class="dd-box"><h5>${__("This visit")}</h5>
				<div class="dd-field"><label>${__("Visit Report")} *</label><textarea class="form-control" rows="3" data-k="note"
					placeholder="${__("What you found, what you did...")}"></textarea></div>
				${serialField(__("Serial of the item you take"))}
				<div><button class="btn btn-primary btn-sm" data-act="fetch">${frappe.utils.icon("package", "xs")} ${__("Fetch Item")}</button></div></div>`;
		if (can.receive)
			actions += `<div class="dd-box"><h5>${__("Receive in the workshop")}</h5>
				${serialField(__("Scan the serial: from now the workshop answers for it"))}
				<div><button class="btn btn-primary btn-sm" data-act="receive">${frappe.utils.icon("package", "xs")} ${__("Fetch Item")}</button></div></div>`;
		if (can.repair)
			actions += `<div class="dd-box"><h5>${__("Repair")}</h5>
				<div class="dd-field"><label>${__("Result")} *</label><select class="form-control" data-k="result">
					<option value="Repaired">${__("Repaired")}</option><option value="Not repairable">${__("Not repairable")}</option></select></div>
				<div class="dd-field"><label>${__("Workshop Report")} *</label><textarea class="form-control" rows="3" data-k="maint_report"
					placeholder="${__("Findings, parts replaced, tests done...")}">${esc(t.maint_report)}</textarea></div>
				<div class="dd-field"><label>${__("Proof (photo / video)")} *</label>
					<div class="dd-btn-row"><input type="file" class="hidden" data-file="repair" accept="image/*,video/*">
					<button class="btn btn-default btn-sm" data-upload="repair">${frappe.utils.icon("upload", "xs")} ${__("Upload proof")}</button>
					${fileState("repair", __("Uploaded"), __("Missing"))}</div></div>
				<div><button class="btn btn-success btn-sm" data-act="repaired">${__("Finish Repair")}</button></div></div>`;
		if (view === "maintenance" && t.step === "Return" && !can.send_out && !t.waiting_driver)
			actions += noDriver(__("Waiting for the delivery office to assign a driver to the return trip."));
		if (can.send_out)
			actions += `<div class="dd-box"><h5>${__("Send out to {0}", [t.driver_name || t.driver])}</h5>
				${serialField(__("Scan the serial of the item you hand over"))}
				<div><button class="btn btn-primary btn-sm" data-act="send-out">${frappe.utils.icon("truck", "xs")} ${__("Send Out")}</button></div></div>`;
		if (can.receive_back)
			actions += `<div class="dd-box"><h5>${__("Receive into the Customer Items Store")}</h5>
				<p class="small dd-muted">${__("The customer was not home. Scan the serial of the item the driver brought back.")}</p>
				${t.has_serial ? `<div class="dd-field"><label>${__("Scan serial number")} *</label><input class="form-control" data-k="store_serial"></div>` : ""}
				<div><button class="btn btn-warning btn-sm" data-act="receive-back">${frappe.utils.icon("arrow-down-left", "xs")} ${__("Receive")}</button></div></div>`;
		if (can.store_send)
			actions += `<div class="dd-box"><h5>${__("Hand to {0}", [esc(t.driver_name || t.driver)])}</h5>
				<p class="small dd-muted">${__("Scan the serial of the item you hand to the driver. It moves to Goods in Transit.")}</p>
				${t.has_serial ? `<div class="dd-field"><label>${__("Scan serial number")} *</label><input class="form-control" data-k="store_serial"></div>` : ""}
				<div><button class="btn btn-primary btn-sm" data-act="store-send">${frappe.utils.icon("arrow-up-right", "xs")} ${__("Hand to driver")}</button></div></div>`;
		if (can.complete)
			actions += `<div class="dd-box"><h5>${__("Hand-over to the customer")}</h5>
				<div class="dd-btn-row"><input type="file" class="hidden" data-file="customer" accept="image/*,video/*" capture="environment">
					<button class="btn btn-default btn-sm" data-upload="customer">${frappe.utils.icon("upload", "xs")} ${__("Upload photo / video")}</button>
					${fileState("customer", __("Uploaded"), __("Missing"))}</div>
				<div class="dd-sign"></div>
				<div class="dd-btn-row"><button class="btn btn-default btn-sm" data-act="sign">${__("Save signature")}</button>
					${fileState("signature", __("Signed"), __("Not signed"))}</div>
				<div class="dd-field"><label>${__("Visit note")} <span class="dd-muted">(${__("needed for Customer not home")})</span></label>
					<textarea class="form-control" rows="2" data-k="note"></textarea></div>
				<div><button class="btn btn-success btn-sm" data-act="complete">${frappe.utils.icon("check", "xs")} ${__("Complete")}</button></div></div>`;
		const footButtons = [
			can.not_home ? `<button class="btn btn-danger btn-xs dm-sup-small" data-act="not-home">${frappe.utils.icon("close", "xs")} ${__("Customer not home")}</button>` : "",
			can.resolve ? `<button class="btn btn-success btn-xs dm-sup-small" data-act="resolve">${frappe.utils.icon("check", "xs")} ${__("Resolve")}</button>` : "",
		].join("");
		const foot = footButtons ? `<div class="dd-box dm-sup-foot">
			${can.resolve_note ? `<div class="dd-field"><label>${__("Your note (needed to resolve)")}</label>
				<textarea class="form-control" rows="2" data-k="resolve_note"></textarea></div>` : ""}
			${can.resolve && t.resolve_needs_serial ? `<div class="dd-field"><label>${__("Serial of the item (it leaves {0})", [esc(t.warehouse)])}</label>
				<input class="form-control" data-k="resolve_serial"></div>` : ""}
			<div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">${footButtons}</div></div>` : "";

		// What this person does now comes first; the facts follow.
		const work = actions || foot ? `<div class="dm-work">${actions}${foot}</div>` : "";
		$b.html(`${header}${work}<div class="dd-detail">
			<div class="dm-sup-col">${customerBox}${itemBox}${tripBox}${descBox}</div>
			<div class="dm-sup-col">${visitsBox}${workshopBox}${filesBox}${historyBox}</div></div>`);
		const val = (k) => ($b.find(`[data-k=${k}]`).val() || "").trim();
		const act = (a, fn) => $b.find(`[data-act=${a}]`).on("click", fn);
		const post = (method, args, msg, keepOpen = true) => call(method, { name, ...args }, { post: true, freeze: true })
			.then(() => changed(msg, keepOpen));
		const needSerial = () => t.has_serial && !val("serial") && (frappe.msgprint(__("Scan or type the serial first.")), true);

		if (can.update_address) {
			call("get_areas").then((areas) => $b.find("[data-a=area]").html(areas.map((a) =>
				`<option ${a === t.area ? "selected" : ""}>${esc(a)}</option>`).join("")));
			act("address", () => {
				const data = {};
				$b.find("[data-a]").each((_, el) => { data[$(el).data("a")] = ($(el).val() || "").trim(); });
				post("update_address", { data }, __("Address saved"));
			});
		}
		act("book", () => bookTrip(t, (msg) => changed(msg)));
		const needNote = (msg) => !val("note") && (frappe.msgprint(msg || __("Write your visit report first.")), true);
		act("fetch", async () => {
			if (needNote() || needSerial()) return;
			if (!(await sure(__("Take the item from the customer? It goes into Goods in Transit with you.")))) return;
			post("fetch_item", { serial_no: val("serial"), report: val("note") }, __("Item taken: it is now in Goods in Transit"), false);
		});
		act("receive", async () => {
			if (needSerial()) return;
			if (!(await sure(__("Receive the item into the workshop store?")))) return;
			post("receive_in_workshop", { serial_no: val("serial") }, __("Item received in the workshop"));
		});
		act("repaired", async () => {
			if (!val("maint_report")) return frappe.msgprint(__("Please write the workshop report"));
			if (!count("repair")) return frappe.msgprint(__("Upload a photo or video that proves the repair first."));
			if (!(await sure(__("Finish the repair as <b>{0}</b>? Customer service then books the return.", [__(val("result"))])))) return;
			post("finish_repair", { result: val("result"), report: val("maint_report") }, __("Done: customer service books the return"));
		});
		act("send-out", async () => {
			if (needSerial()) return;
			if (!(await sure(__("Hand the item to {0}? It leaves {1}.", [esc(t.driver_name || t.driver), esc(t.warehouse)])))) return;
			post("send_out", { serial_no: val("serial") }, __("Sent out to the driver"), false);
		});
		const storeMove = async (method, question, msg) => {
			if (t.has_serial && !val("store_serial")) return frappe.msgprint(__("Scan the serial first."));
			if (t.has_serial && t.serial_no && val("store_serial") !== t.serial_no)
				return frappe.msgprint(__("This is not the serial of the ticket ({0}).", [esc(t.serial_no)]));
			if (!(await sure(question))) return;
			post(method, { serial_no: val("store_serial") }, msg, false);
		};
		act("receive-back", () => storeMove("receive_back", __("Receive the item into the store?"), __("Received into the store")));
		act("store-send", () => storeMove("store_send_out", __("Hand the item to {0}?", [esc(t.driver_name || t.driver)]), __("Handed to the driver")));
		act("complete", async () => {
			if (!count("customer") || !count("signature"))
				return frappe.msgprint(__("Upload a photo or video and save the customer's signature first."));
			if (!(await sure(__("The customer has the item back? The ticket closes.")))) return;
			post("complete_return", { note: val("note") }, __("Returned to the customer: ticket closed"), false);
		});
		act("not-home", async () => {
			if (needNote(__("Write your visit note first."))) return;
			if (!(await sure(t.step === "Out for Return"
				? __("The customer is not home? Bring the item back to the Customer Items Store.") : __("The customer is not home?")))) return;
			post("customer_not_home", { note: val("note") }, t.step === "Out for Return"
				? __("Bring the item back to the Customer Items Store: the stock keeper scans it in") : __("Back to customer service to book again"), false);
		});
		act("resolve", async () => {
			const note = can.resolve_note ? val("resolve_note") : val("note");
			if (!note) return frappe.msgprint(__("Write your note first: how it was resolved."));
			if (t.resolve_needs_serial && !val("resolve_serial")) return frappe.msgprint(__("Scan or type the serial first."));
			const leaves = t.warehouse && ["To Workshop", "Under Repair", "Ready for Return", "Return", "Out for Return", "Back to Store"].includes(t.step)
				? " " + __("The item leaves {0}.", [esc(t.warehouse)]) : "";
			if (!(await sure(__("Resolve ticket {0}? It ends now.", [t.name]) + leaves))) return;
			post("resolve_ticket", { view, note, serial_no: val("resolve_serial") }, __("Ticket resolved"), false);
		});

		// Files: uploaded without reloading the ticket, so nothing typed is lost.
		const upload = async (kind, filename, data) => {
			const r = await call("upload_file", { name, kind, filename, filedata: data }, { post: true, freeze: true,
				freeze_message: __("Uploading…") });
			t.files.unshift({ file_name: FILE_LABELS.find(([, k]) => k === kind)[0], file_url: r.file_url });
			const ok = kind === "signature" ? __("Signed") : __("Uploaded");
			$b.find(`[data-state=${kind}]`).removeClass("dd-no").addClass("dd-ok").text(`${ok} (${count(kind)})`);
			frappe.show_alert({ message: __("Uploaded"), indicator: "green" });
			onChange && onChange();
		};
		$b.find("[data-upload]").on("click", (e) => $b.find(`[data-file=${$(e.currentTarget).data("upload")}]`).trigger("click"));
		$b.find("[data-file]").on("change", async (e) => {
			const file = e.target.files[0];
			e.target.value = "";
			if (!file) return;
			try {
				await upload($(e.target).data("file"), file.name, await readFile(file));
			} catch (err) {
				if (typeof err === "string") frappe.msgprint(err);
			}
		});
		if (can.complete) {
			const sign = frappe.ui.form.make_control({ parent: $b.find(".dd-sign"), render_input: true,
				df: { fieldtype: "Signature", fieldname: "signature", label: __("Customer signs here") } });
			act("sign", () => {
				const data = sign.get_value();
				if (!data) return frappe.msgprint(__("Ask the customer to sign first."));
				upload("signature", "signature.png", data);
			});
		}
		$b.find("[data-act=edit-visit]").on("click", (e) => {
			const $v = $(e.currentTarget).closest("[data-row]");
			const row = $v.data("row");
			$v.find("[data-text]").html(`<textarea class="form-control" rows="3">${esc(t.visits.find((v) => v.name === row).report)}</textarea>
				<div class="dd-btn-row"><button class="btn btn-xs btn-primary" data-save>${__("Save Change")}</button></div>`);
			$(e.currentTarget).hide();
			$v.find("[data-save]").on("click", () => post("update_visit_report", { row, report: $v.find("textarea").val() }, __("Report updated")));
		});
	}

	// ---------------------------------------------------------------- the three pages

	const VIEWS = {
		cs: {
			title: __("Support Tickets"), icon: "support", search: __("Search by ticket, subject, customer or phone…"),
			tabs: [{ key: "open", label: __("Open"), icon: "support" }, { key: "ready", label: __("Ready for Return"), icon: "truck" },
				{ key: "closed", label: __("Closed"), icon: "check" }],
		},
		technician: {
			title: __("Technician Issues"), icon: "tool", search: __("Search your visits…"), dateFilter: true,
			tabs: [{ key: "open", label: __("My visits"), icon: "tool" }, { key: "closed", label: __("Done"), icon: "check" }],
		},
		maintenance: {
			title: __("Maintenance Issues"), icon: "setting-gear", search: __("Search workshop tickets…"),
			tabs: [{ key: "open", label: __("In the workshop"), icon: "setting-gear" }, { key: "closed", label: __("Sent out"), icon: "check" }],
		},
	};

	class SupportPage {
		constructor(wrapper, view) {
			this.view = view;
			this.cfg = VIEWS[view];
			this.tab = "open";
			this.page = frappe.ui.make_app_page({ parent: wrapper, title: this.cfg.title, single_column: true });
			this.f = { search: "", trip_date: "" };
			this.$root = $(`<div class="dd-page dm-support">
				${window.dm_drivers.tabs(this.cfg.tabs, this.tab)}
				<div class="dd-panel"><div class="dd-filters">
					<div class="dd-field" style="grid-column: span 2"><label>${__("Search")}</label>
						<input type="search" class="form-control" data-f="search" placeholder="${esc(this.cfg.search)}"></div>
					${this.cfg.dateFilter ? `<div class="dd-field"><label>${__("Visit date")}</label>
						<input type="date" class="form-control" data-f="trip_date"></div>` : ""}
				</div></div>
				<div class="dd-panel"><div class="dd-scroll"><table class="dd-table dd-cards"><thead><tr>
					<th>${__("Ticket")}</th><th>${__("Customer")}</th><th>${__("Item")}</th><th>${__("Step")}</th>
					<th>${__("Trip")}</th><th>${__("Address")}</th>
				</tr></thead><tbody></tbody></table></div>
				<div class="dm-sup-pager" style="display:flex;align-items:center;justify-content:flex-end;gap:6px;padding:8px 12px">
					<span class="dd-muted small" data-page-label style="margin-right:4px"></span>
					<div class="btn-group">
						<button class="btn btn-default btn-xs" data-prev title="${__("Previous")}">${frappe.utils.icon("left", "xs")}</button>
						<button class="btn btn-default btn-xs" data-next title="${__("Next")}">${frappe.utils.icon("right", "xs")}</button>
					</div></div></div>
			</div>`).appendTo(this.page.main);
			this.pageSize = 20;
			this.offset = 0;
			this.$root.find("[data-prev]").on("click", () => { this.offset = Math.max(0, this.offset - this.pageSize); this.load(); });
			this.$root.find("[data-next]").on("click", () => { this.offset += this.pageSize; this.load(); });

			this.page.set_secondary_action(__("Refresh"), () => this.load(true), "refresh");
			if (view === "cs") this.page.set_primary_action(__("New Ticket"), () => this.newTicket(), "add");
			this.page.add_menu_item(__("Issue list"), () => frappe.set_route("List", "Issue", { sm_stage: ["is", "set"] }));

			this.$root.on("click", ".dd-tab-btn", (e) => {
				this.tab = $(e.currentTarget).data("tab");
				this.$root.find(".dd-tab-btn").removeClass("active");
				$(e.currentTarget).addClass("active");
				this.offset = 0;
				this.load();
			});
			let timer;
			this.$root.find("[data-f]").on("input change", (e) => {
				this.f[$(e.currentTarget).data("f")] = $(e.currentTarget).val();
				clearTimeout(timer);
				timer = setTimeout(() => { this.offset = 0; this.load(); }, e.type === "input" ? 300 : 0);
			});
		}

		show() {
			const opts = frappe.route_options || {};
			frappe.route_options = null;
			this.load().then(() => opts.ticket && openTicket(opts.ticket, () => this.load(), this.view));
		}

		async load(feedback) {
			const $tbody = this.$root.find("tbody");
			if (!this.rows) $tbody.html(window.dm_drivers.skeleton(6, 4));
			const seq = (this.seq = (this.seq || 0) + 1);
			const res = await call("get_tickets", { view: this.view, tab: this.tab, ...this.f,
				limit: this.pageSize, offset: this.offset });
			if (seq !== this.seq) return;
			this.rows = res.rows;
			const total = res.total || 0;
			this.$root.find("[data-page-label]").text(total
				? __("{0}-{1} of {2}", [this.offset + 1, Math.min(this.offset + this.pageSize, total), total]) : "");
			this.$root.find("[data-prev]").prop("disabled", this.offset === 0);
			this.$root.find("[data-next]").prop("disabled", this.offset + this.pageSize >= total);
			Object.entries(res.counts || {}).forEach(([k, n]) => this.$root.find(`[data-count="${k}"]`).text(n));
			if (this.view === "cs" && !res.can_create) this.page.clear_primary_action();
			this.render();
			if (feedback) frappe.show_alert({ message: __("Data refreshed"), indicator: "green" });
		}

		render() {
			const $tbody = this.$root.find("tbody");
			if (!this.rows.length) {
				$tbody.html(window.dm_drivers.emptyRow(6, __("No tickets here"), __("Change the tab or the filters.")));
				return;
			}
			$tbody.html(this.rows.map((r) => `
				<tr data-name="${esc(r.name)}" tabindex="0">
					<td data-label="${__("Ticket")}"><b>${esc(r.subject)}</b><span class="dd-sub">${esc(r.name)} · ${day(r.creation)}</span>
						<span class="dd-sub">${priorityPill(r.priority)}</span></td>
					<td data-label="${__("Customer")}"><span>${esc(r.customer_name || "")}</span>
						<span class="dd-sub">${window.dm_drivers.phoneLink(r.customer_phone)}</span></td>
					<td data-label="${__("Item")}">${esc(r.item_code || "")}${r.serial_no ? `<span class="dd-sub">${esc(r.serial_no)}</span>` : ""}
						<span class="dd-sub">${warrantyPill(r)}</span></td>
					<td data-label="${__("Step")}">${stepPill(r.step)}
						${r.not_home_count ? `<span class="dd-sub">${pill(__("Customer was not home ({0}): book a new slot", [r.not_home_count]), "red")}</span>` : ""}
						${r.take_from && ["Ready for Return", "Return"].includes(r.step) ? `<span class="dd-sub">${frappe.utils.icon("map-pin", "xs")} ${__("Item in {0}", [esc(r.take_from)])}</span>` : ""}
						${r.last_note ? `<span class="dd-sub dd-muted">${esc(r.last_note.length > 120 ? r.last_note.slice(0, 120) + "…" : r.last_note)}</span>` : ""}</td>
					<td data-label="${__("Trip")}">${r.trip_date ? `<span class="dd-when">${esc(tripText(r))}</span>
						<span class="dd-sub">${r.driver_name ? esc(r.driver_name) : __("No driver yet")}</span>` : `<span class="dd-muted">—</span>`}</td>
					<td data-label="${__("Address")}" class="dd-addr">${esc(r.address)}</td>
				</tr>`).join(""));
			$tbody.find("tr[data-name]").on("click keydown", (e) => {
				if (e.type === "keydown" && e.key !== "Enter") return;
				openTicket($(e.currentTarget).data("name"), () => this.load(), this.view);
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
							<input class="form-control" data-k="subject" placeholder="${__("e.g. TV black screen")}"></div>
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
							<div class="dd-field"><label>${__("Item Code")} *</label><div data-item></div></div>
							<div class="dd-field"><label>${__("Serial No")}</label>
								<input class="form-control" data-k="serial_no" placeholder="${__("Filled from the order")}"></div>
						</div>
						<div class="dm-sup-row">
							<div class="dd-field"><label>${__("Area")} *</label><select class="form-control" data-k="area"></select></div>
							<div class="dd-field"><label>${__("Block")} *</label><input class="form-control" data-k="block_no"></div>
							<div class="dd-field"><label>${__("Road")} *</label><input class="form-control" data-k="road_no"></div>
							<div class="dd-field"><label>${__("Building")} *</label><input class="form-control" data-k="building_no"></div>
						</div>
						<div class="dd-field"><label>${__("Full Address")} *</label>
							<input class="form-control" data-k="address" placeholder="${__("Flat, floor, landmark...")}"></div>
						<div class="dd-field"><label>${__("Issue Description")} *</label>
							<textarea class="form-control" rows="3" data-k="description" placeholder="${__("Describe the problem in detail...")}"></textarea></div>
						<div class="dm-sup-picked small dd-muted"></div>
					</div>
					<div class="dd-box dm-sup-orders"><h5>${__("Customer Past Orders")}</h5>
						<div class="dd-muted small">${__("Type the customer's phone (8 digits or more) to see the orders. Click an item to fill the ticket.")}</div></div>
				</div>`);
			const val = (k) => ($b.find(`[data-k=${k}]`).val() || "").trim();
			const state = { customer: null, sales_invoice: null, picking: false };
			const item = frappe.ui.form.make_control({ parent: $b.find("[data-item]"), render_input: true,
				df: { fieldtype: "Link", options: "Item", fieldname: "dm_item_code", placeholder: __("Pick it from the orders, or type it"),
					change: () => {
						if (state.picking) return;
						// Typed by hand: the order link and the serial no longer apply.
						state.sales_invoice = null;
						$b.find("[data-k=serial_no]").val("");
						$b.find(".dm-sup-picked").empty();
					} } });
			frappe.db.get_list("Issue Type", { pluck: "name", limit: 200 }).then((types) =>
				$b.find("#dm-issue-types").html(types.map((x) => `<option value="${esc(x)}">`).join("")));
			call("get_areas").then((areas) => $b.find("[data-k=area]").html(`<option value="">${__("-- Area --")}</option>`
				+ areas.map((a) => `<option>${esc(a)}</option>`).join("")));
			const fillAddress = (a) => {
				if (!a) return;
				if (a.area) $b.find("[data-k=area]").val(a.area);
				["block_no", "road_no", "building_no", "address"].forEach((k) => a[k] && $b.find(`[data-k=${k}]`).val(a[k]));
			};

			let timer;
			$b.find("[data-k=customer_phone]").on("input", () => {
				clearTimeout(timer);
				state.customer = null;
				const phone = val("customer_phone");
				if (phone.length < 8) return $b.find(".dm-sup-customer").empty();
				timer = setTimeout(async () => {
					const res = await call("lookup_customer", { phone });
					if (val("customer_phone") !== phone) return;
					state.customer = res.customer;
					fillAddress(res.address);
					$b.find(".dm-sup-customer").html(res.customer
						? `${frappe.utils.icon("users", "xs")} <b>${esc(res.customer_name)}</b> (${esc(res.customer)})`
						: `<span class="text-muted">${__("New phone number: type the address by hand.")}</span>`);
					this.renderOrders($b.find(".dm-sup-orders"), res.orders || [], async (order, it, serial) => {
						state.picking = true;
						await item.set_value(it.item_code);
						state.picking = false;
						$b.find("[data-k=serial_no]").val(serial || "");
						state.sales_invoice = order.name;
						$b.find(".dm-sup-picked").html(__("Linked to invoice {0}", [`<b>${esc(order.name)}</b>`]));
					});
				}, 350);
			});

			d.set_primary_action(__("Create Ticket"), async () => {
				const data = {
					subject: val("subject"), customer_phone: val("customer_phone"), priority: val("priority"),
					customer_email: val("customer_email"), issue_type: val("issue_type"), item_code: item.get_value() || "",
					serial_no: val("serial_no"), area: val("area"), block_no: val("block_no"), road_no: val("road_no"),
					building_no: val("building_no"), address: val("address"), description: val("description"),
					customer: state.customer, sales_invoice: state.sales_invoice,
				};
				const missing = [["subject", __("Subject")], ["customer_phone", __("Customer Phone")], ["item_code", __("Item Code")],
					["area", __("Area")], ["block_no", __("Block")], ["road_no", __("Road")], ["building_no", __("Building")],
					["address", __("Full Address")], ["description", __("Issue Description")]]
					.filter(([k]) => !data[k]).map(([, l]) => l);
				if (missing.length) return frappe.msgprint(__("Please fill: {0}", [missing.join(", ")]));
				const r = await call("create_ticket", { data }, { post: true, freeze: true });
				d.hide();
				frappe.show_alert({ message: __("Support ticket {0} created", [r.name]), indicator: "green" });
				await this.load();
				openTicket(r.name, () => this.load(), this.view);
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
						return serials.map((sn) => `<button class="btn btn-xs btn-default" data-o="${oi}" data-i="${ii}" data-sn="${esc(sn || "")}">
							${it.is_component ? "↳ " : ""}${esc(it.item_code)} · ${__("Qty")}: ${it.qty}${sn ? ` · ${esc(sn)}` : ""}</button>`).join("");
					}).join("") || `<span class="dd-muted small">${__("No items listed")}</span>`}</div>
				</div>`).join(""));
			$box.find("button[data-o]").on("click", (e) => {
				const $t = $(e.currentTarget);
				const order = orders[$t.data("o")];
				onPick(order, order.items[$t.data("i")], $t.data("sn") || null);
			});
		}
	}

	window.dm_support = { SupportPage, openTicket, stepPill, warrantyPill, call };
})();
