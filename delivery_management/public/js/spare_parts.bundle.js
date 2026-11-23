// delivery_management: workshop spare parts (customer_support/spare_parts.py).
// * window.dm_spare.box(t) / bind($b, t, changed): the "Spare parts" card inside the ticket.
// * window.dm_spare.decide(req, done): approve (quantities can be lowered) or reject.
// Used by the ticket dialog (support_pages.bundle.js) and the Spare Parts page.
(function () {
	const API = "delivery_management.api.spare_parts.";
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
	const money = (v) => format_currency(v || 0);
	const num = (v) => (+v || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
	const when = (v) => (v ? moment(v).format("DD/MM/YYYY h:mm A") : "");

	/** Yes / no before an approval or a rejection. */
	const sure = (msg) => new Promise((resolve) => frappe.confirm(msg, () => resolve(true), () => resolve(false)));

	function call(method, args, post) {
		return frappe.call({ method: API + method, args, type: post ? "POST" : "GET", freeze: !!post })
			.then((r) => r.message);
	}

	const STATUS = {
		Pending: ["orange", __("Waiting for approval")],
		Approved: ["blue", __("Approved · take them")],
		Taken: ["green", __("Taken")],
		Returned: ["gray", __("All returned")],
		Rejected: ["red", __("Rejected")],
		Cancelled: ["gray", __("Cancelled")],
	};
	const statusPill = (s) => `<span class="dd-pill dd-pill-${(STATUS[s] || ["gray"])[0]}">${esc((STATUS[s] || [0, s])[1])}</span>`;

	/** One line per part: what was asked, approved, taken, returned. */
	function itemLines(req) {
		return req.items.map((i) => {
			const facts = [`${__("asked")} ${num(i.qty)}`];
			if (req.status !== "Pending" && req.status !== "Rejected") facts.push(`${__("approved")} ${num(i.approved_qty)}`);
			if (+i.taken_qty) facts.push(`${__("taken")} ${num(i.taken_qty)}`);
			if (+i.returned_qty) facts.push(`${__("returned")} ${num(i.returned_qty)}`);
			const serials = (i.serial_no || "").split(/\s+/).filter(Boolean);
			return `<div class="dm-part-line"><span><b>${esc(i.item_name || i.item_code)}</b>
				<span class="dd-muted small">${facts.join(" · ")}</span>
				${serials.length ? `<span class="dm-part-serials">${serials.map(esc).join(", ")}</span>` : ""}</span>
				${+i.amount ? `<span class="dm-part-amt">${money(i.amount)}</span>` : ""}</div>`;
		}).join("");
	}

	function requestCard(req) {
		const c = req.can || {};
		const buttons = [
			c.decide ? `<button class="btn btn-xs btn-primary" data-sp="decide">${__("Approve / Reject")}</button>` : "",
			c.take ? `<button class="btn btn-sm btn-success" data-sp="take">${frappe.utils.icon("check", "xs")} ${__("I took them")}</button>` : "",
			c.return ? `<button class="btn btn-xs btn-default" data-sp="return">${frappe.utils.icon("arrow-down-left", "xs")} ${__("Return unused")}</button>` : "",
			c.cancel ? `<button class="btn btn-xs btn-default dm-part-cancel" data-sp="cancel">${__("Cancel request")}</button>` : "",
		].join("");
		const note = req.status === "Rejected" && req.reject_reason
			? `<div class="dm-part-note dd-no">${__("Rejected")}: ${esc(req.reject_reason)}</div>`
			: req.status === "Approved" ? `<div class="dm-part-note">${__("Held for you in the store. Take them, then confirm here.")}</div>`
			: req.status === "Pending" ? `<div class="dm-part-note dd-muted">${__("A System Manager decides. You get a notification.")}</div>` : "";
		return `<div class="dm-part" data-req="${esc(req.name)}" data-status="${esc(req.status)}">
			<div class="dm-part-head"><b>${esc(req.name)}</b>${statusPill(req.status)}
				<span class="dd-muted small">${esc(req.technician_name || "")} · ${when(req.requested_on)}</span></div>
			${itemLines(req)}${note}
			${buttons ? `<div class="dm-part-actions">${buttons}</div>` : ""}</div>`;
	}

	/** The card inside the ticket: costs, then the requests. */
	function box(t) {
		const p = t.parts;
		if (!p || (!p.requests.length && !p.can_request && !+p.total_cost)) return "";
		const repair = p.can_set_cost
			? `<div class="dm-cost-cell"><span>${__("Repair cost")}</span><div class="dm-cost-edit">
				<input type="number" min="0" step="0.001" class="form-control input-xs" data-sp-cost value="${+p.repair_cost || ""}" placeholder="0">
				<button class="btn btn-xs btn-default" data-sp="cost">${__("Save")}</button></div></div>`
			: `<div class="dm-cost-cell"><span>${__("Repair cost")}</span><strong>${money(p.repair_cost)}</strong></div>`;
		return `<div class="dd-box dm-parts">
			<div class="dm-parts-head"><h5>${frappe.utils.icon("tool", "sm")} ${__("Spare parts & cost")}</h5>
				${p.can_request ? `<button class="btn btn-sm btn-warning" data-sp="request">+ ${__("Request parts")}</button>` : ""}</div>
			<div class="dm-cost">
				<div class="dm-cost-cell"><span>${__("Parts")}</span><strong>${money(p.parts_cost)}</strong></div>
				${repair}
				<div class="dm-cost-cell dm-cost-total"><span>${__("Total")}</span><strong>${money(p.total_cost)}</strong></div>
			</div>
			${p.requests.length ? p.requests.map(requestCard).join("")
				: `<div class="dd-muted small">${__("No spare parts asked for this repair.")}</div>`}
		</div>`;
	}

	function bind($b, t, changed) {
		const p = t.parts;
		if (!p) return;
		const byName = Object.fromEntries(p.requests.map((r) => [r.name, r]));
		$b.find("[data-sp=request]").on("click", () => requestDialog(t.name, () => changed(__("Sent to the System Manager"))));
		$b.find("[data-sp=cost]").on("click", async () => {
			const amount = $b.find("[data-sp-cost]").val();
			if (amount === "" || +amount < 0) return frappe.msgprint(__("Type the repair cost (0 or more)."));
			await call("set_repair_cost", { name: t.name, amount }, true);
			changed(__("Repair cost saved"));
		});
		$b.find(".dm-part [data-sp]").on("click", (e) => {
			const req = byName[$(e.currentTarget).closest(".dm-part").data("req")];
			const act = $(e.currentTarget).data("sp");
			if (act === "take") takeDialog(req, () => changed(__("Parts taken: their cost is on the ticket")));
			if (act === "return") returnDialog(req, () => changed(__("Returned to the store")));
			if (act === "cancel") cancelDialog(req, () => changed(__("Request cancelled")));
			if (act === "decide") decide(req, () => changed(__("Done")));
		});
	}

	/** Search the store (name, code or barcode scan), build a list, say why, send. */
	function requestDialog(ticket, done) {
		const cart = new Map();
		const d = new frappe.ui.Dialog({
			title: __("Request spare parts · {0}", [ticket]), size: "large",
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
			primary_action_label: __("Send to manager"),
			primary_action: async () => {
				if (!cart.size) return frappe.msgprint(__("Add at least one part."));
				const reason = ($b.find("[data-reason]").val() || "").trim();
				if (!reason) return frappe.msgprint(__("Write why you need them."));
				const items = [...cart.values()].map((x) => ({ item_code: x.item_code, qty: x.qty }));
				await call("request_parts", { name: ticket, items, reason }, true);
				d.hide();
				done();
			},
		});
		const $b = d.fields_dict.body.$wrapper.html(`
			<div class="dm-sp-search"><span>${frappe.utils.icon("search", "sm")}</span>
				<input class="form-control" data-q placeholder="${__("Search a part or scan its barcode")}" autocomplete="off"></div>
			<div class="dm-sp-results"></div>
			<h6 class="dm-sp-title">${__("Your list")}</h6>
			<div class="dm-sp-cart"></div>
			<div class="dd-field"><label>${__("Why do you need them?")} *</label>
				<textarea class="form-control" rows="2" data-reason placeholder="${__("e.g. power board burnt")}"></textarea></div>`);
		let results = [];
		const drawCart = () => {
			$b.find(".dm-sp-cart").html(cart.size ? `<table class="dm-sp-table"><thead><tr><th>${__("Part")}</th>
				<th class="text-right">${__("Free")}</th><th class="text-right">${__("Qty")}</th><th></th></tr></thead><tbody>
				${[...cart.values()].map((x) => `<tr data-code="${esc(x.item_code)}"><td><b>${esc(x.item_name)}</b>
					<div class="dd-muted small">${esc(x.item_code)}${x.has_serial_no ? " · " + __("serial") : ""}</div></td>
					<td class="text-right">${num(x.free)}</td>
					<td class="text-right"><input type="number" min="1" max="${x.free}" step="1" class="form-control input-xs dm-sp-qty" value="${x.qty}"></td>
					<td class="text-right"><button class="btn btn-xs btn-link" data-del>${frappe.utils.icon("delete", "xs")}</button></td></tr>`).join("")}
				</tbody></table>` : `<div class="dd-muted small">${__("Nothing yet: search above and tap a part.")}</div>`);
		};
		const add = (x) => {
			if (+x.free <= 0) return frappe.show_alert({ message: __("{0}: nothing free in the store", [x.item_name]), indicator: "orange" });
			const have = cart.get(x.item_code);
			if (have) have.qty = Math.min(have.qty + 1, x.free);
			else cart.set(x.item_code, { ...x, qty: 1 });
			drawCart();
		};
		const drawResults = () => {
			$b.find(".dm-sp-results").html(results.length ? results.map((x, i) => `<button class="dm-sp-hit" data-i="${i}" ${+x.free > 0 ? "" : "disabled"}>
				<span><b>${esc(x.item_name)}</b><span class="dd-muted small"> ${esc(x.item_code)}</span></span>
				<span class="${+x.free > 0 ? "dd-ok" : "dd-no"} small">${+x.free > 0 ? __("{0} free", [num(x.free)]) : __("none free")}
				${+x.held ? `<span class="dd-muted">· ${__("{0} held", [num(x.held)])}</span>` : ""}</span></button>`).join("")
				: `<div class="dd-muted small">${__("No part found in the Spare Parts Store.")}</div>`);
		};
		let seq = 0;
		const search = async () => {
			const my = ++seq;
			const r = await call("search_parts", { txt: $b.find("[data-q]").val() });
			if (my !== seq) return; // an older answer
			results = r;
			drawResults();
		};
		$b.find("[data-q]").on("input", frappe.utils.debounce(search, 250)).on("keydown", async (e) => {
			if (e.key !== "Enter") return;
			e.preventDefault();
			await search();
			const q = ($b.find("[data-q]").val() || "").trim();
			// A barcode scan ends with Enter: one hit is added at once.
			if (results.length === 1 || (results[0] && results[0].item_code === q)) {
				add(results[0]);
				$b.find("[data-q]").val("").trigger("focus");
			}
		});
		$b.on("click", ".dm-sp-hit", (e) => add(results[$(e.currentTarget).data("i")]));
		$b.on("change", ".dm-sp-qty", (e) => {
			const x = cart.get($(e.currentTarget).closest("tr").data("code"));
			const qty = Math.floor(+$(e.currentTarget).val() || 0);
			x.qty = Math.max(1, Math.min(qty, x.free));
			$(e.currentTarget).val(x.qty);
		});
		$b.on("click", "[data-del]", (e) => {
			cart.delete($(e.currentTarget).closest("tr").data("code"));
			drawCart();
		});
		drawCart();
		d.show();
		search();
		setTimeout(() => $b.find("[data-q]").trigger("focus"), 300);
	}

	/** Serial inputs: one per unit, Enter jumps to the next (scanner friendly). */
	function serialInputs(code, count, hint) {
		return `<div class="dm-sp-serials" data-code="${esc(code)}">${Array.from({ length: count }, (_, k) =>
			`<input class="form-control input-sm" data-serial placeholder="${__("Scan serial {0}", [k + 1])}" autocomplete="off">`).join("")}
			${hint ? `<div class="dd-muted small">${hint}</div>` : ""}</div>`;
	}
	function scanFlow($b) {
		$b.on("keydown", "[data-serial]", (e) => {
			if (e.key !== "Enter") return;
			e.preventDefault();
			const all = $b.find("[data-serial]");
			all.eq(all.index(e.currentTarget) + 1).trigger("focus");
		});
	}
	const scanned = ($b, code) => $b.find(`.dm-sp-serials[data-code="${CSS.escape(code)}"] [data-serial]`).map((_, el) => el.value.trim()).get().filter(Boolean);

	function takeDialog(req, done) {
		const items = req.items.filter((i) => +i.approved_qty);
		const d = new frappe.ui.Dialog({
			title: __("Take the parts · {0}", [req.name]),
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
			primary_action_label: __("I took them"),
			primary_action: async () => {
				const serials = {};
				for (const i of items.filter((x) => x.has_serial)) {
					serials[i.item_code] = scanned($b, i.item_code);
					if (serials[i.item_code].length !== +i.approved_qty)
						return frappe.msgprint(__("Scan {0} serials of {1}.", [num(i.approved_qty), esc(i.item_name)]));
				}
				await call("take", { request: req.name, serials }, true);
				d.hide();
				done();
			},
		});
		const $b = d.fields_dict.body.$wrapper.html(`<p class="small dd-muted">${__("Take these from the Spare Parts Store, then confirm. They leave the store and their cost goes on the ticket. From now you answer for them.")}</p>
			${items.map((i) => `<div class="dm-sp-take"><div class="dm-part-line"><b>${esc(i.item_name)}</b><span class="dm-part-amt">× ${num(i.approved_qty)}</span></div>
				${i.has_serial ? serialInputs(i.item_code, +i.approved_qty) : ""}</div>`).join("")}`);
		scanFlow($b);
		d.show();
		setTimeout(() => $b.find("[data-serial]").first().trigger("focus"), 300);
	}

	function returnDialog(req, done) {
		const items = req.items.filter((i) => +i.taken_qty - +i.returned_qty > 0);
		const d = new frappe.ui.Dialog({
			title: __("Return unused parts · {0}", [req.name]),
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
			primary_action_label: __("Return to the store"),
			primary_action: async () => {
				const rows = items.map((i) => {
					const serials = i.has_serial ? scanned($b, i.item_code) : [];
					const qty = i.has_serial ? serials.length : Math.floor(+$b.find(`[data-ret="${CSS.escape(i.item_code)}"]`).val() || 0);
					return { item_code: i.item_code, qty, serials };
				}).filter((r) => r.qty > 0);
				if (!rows.length) return frappe.msgprint(__("Type how many you return, or scan their serials."));
				await call("return_parts", { request: req.name, rows }, true);
				d.hide();
				done();
			},
		});
		const $b = d.fields_dict.body.$wrapper.html(`<p class="small dd-muted">${__("Put the unused parts back in the store. Their cost comes off the ticket.")}</p>
			${items.map((i) => {
				const left = +i.taken_qty - +i.returned_qty;
				const out = (i.serial_no || "").split(/\s+/).filter((s) => s && !(i.returned_serials || "").split(/\s+/).includes(s));
				return `<div class="dm-sp-take"><div class="dm-part-line"><b>${esc(i.item_name)}</b>
					<span class="dd-muted small">${__("{0} with you", [num(left)])}</span></div>
					${i.has_serial ? serialInputs(i.item_code, left, __("Scan only what you return. With you: {0}", [out.map(esc).join(", ")]))
						: `<div class="dd-field"><label>${__("Qty returned")}</label>
							<input type="number" min="0" max="${left}" step="1" class="form-control input-sm" data-ret="${esc(i.item_code)}" value="0"></div>`}</div>`;
			}).join("")}`);
		scanFlow($b);
		d.show();
	}

	function cancelDialog(req, done) {
		const d = new frappe.ui.Dialog({
			title: __("Cancel {0}?", [req.name]),
			fields: [{ fieldname: "info", fieldtype: "HTML", options: `<p>${req.status === "Approved"
					? __("The parts are released for other repairs. Nothing leaves the store.") : __("The request is withdrawn and its parts are free again.")}</p>` },
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Why (optional)") }],
			primary_action_label: __("Cancel request"),
			primary_action: async (v) => {
				await call("cancel", { request: req.name, reason: v.reason || "" }, true);
				d.hide();
				done();
			},
		});
		d.show();
	}

	/** System Manager: approve all or part, or reject with a reason. */
	function decide(req, done) {
		const d = new frappe.ui.Dialog({
			title: __("{0} · {1}", [req.name, req.ticket]), size: "large",
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
			primary_action_label: __("Approve"),
			primary_action: async () => {
				const qtys = {};
				$b.find("[data-ap]").each((_, el) => { qtys[$(el).data("ap")] = Math.floor(+el.value || 0); });
				if (!Object.values(qtys).some((q) => q > 0)) return frappe.msgprint(__("Nothing to approve: reject instead."));
				const list = req.items.filter((i) => qtys[i.item_code] > 0)
					.map((i) => `${esc(i.item_name || i.item_code)} × ${qtys[i.item_code]}`).join("<br>");
				if (!(await sure(__("Approve {0}?<br><br>{1}", [req.name, list])))) return;
				await call("approve", { request: req.name, qtys }, true);
				d.hide();
				done();
			},
			secondary_action_label: __("Reject"),
			secondary_action: () => {
				const r = new frappe.ui.Dialog({
					title: __("Reject {0}", [req.name]),
					fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Why"), reqd: 1 }],
					primary_action_label: __("Reject"),
					primary_action: async (v) => {
						if (!(await sure(__("Reject {0}? Its parts are freed for other repairs.", [req.name])))) return;
						await call("reject", { request: req.name, reason: v.reason }, true);
						r.hide();
						d.hide();
						done();
					},
				});
				r.show();
			},
		});
		const $b = d.fields_dict.body.$wrapper.html(`
			<div class="dm-sp-who"><b>${esc(req.technician_name || req.technician)}</b> · ${when(req.requested_on)}
				${req.reason ? `<div class="dm-sup-text">${esc(req.reason)}</div>` : ""}</div>
			<table class="dm-sp-table"><thead><tr><th>${__("Part")}</th><th class="text-right">${__("Asked")}</th>
				${req.items[0] && "free" in req.items[0] ? `<th class="text-right">${__("Free now")}</th>` : ""}
				<th class="text-right">${__("Approve")}</th></tr></thead><tbody>
				${req.items.map((i) => `<tr><td><b>${esc(i.item_name || i.item_code)}</b><div class="dd-muted small">${esc(i.item_code)}</div></td>
					<td class="text-right">${num(i.qty)}</td>
					${"free" in i ? `<td class="text-right ${+i.free < +i.qty ? "dd-no" : ""}">${num(i.free)}</td>` : ""}
					<td class="text-right"><input type="number" min="0" max="${i.qty}" step="1" class="form-control input-xs dm-sp-qty"
						data-ap="${esc(i.item_code)}" value="${"free" in i ? Math.max(0, Math.min(+i.qty, +i.free)) : i.qty}"></td></tr>`).join("")}
			</tbody></table>
			<p class="small dd-muted">${__("These parts are held for this repair. Lowering a qty or rejecting frees it for others.")}</p>`);
		d.show();
	}

	window.dm_spare = { box, bind, decide, call, sure, statusPill, itemLines, money, num, when, esc };
})();
