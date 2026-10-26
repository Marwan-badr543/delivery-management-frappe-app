// delivery_management: "Return / Exchange" in the POS Awesome header.
// 1. Find the sale (invoice / order number, customer name or mobile).
// 2. Tap what comes back: goods the customer brings (serials by tapping them) and, on a
//    delivery order, goods not delivered yet to cancel. Choose the store that receives
//    them, the reason, and how the customer is paid: all in cash, keep as credit
//    (exchange), or part cash.
// 3. Done: the documents are made (api.returns.submit_return, see returns/service.py).
//    "New sale for this customer" starts the exchange; the credit is offered in Payments.
(() => {
	const API = "delivery_management.api.returns.";
	const BTN_ID = "dm-return-btn";
	const REASONS = ["Defective", "Wrong item", "Customer changed mind", "Damaged in delivery", "Exchange", "Other"];
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
	const round = (v) => Math.round((v || 0) * 1000) / 1000;
	const call = (method, args, post) =>
		frappe.call({ method: API + method, args, type: post ? "POST" : "GET", freeze: !!post }).then((r) => r.message);
	const profile = () => document.querySelector('[data-test="pos-navbar"]')?.dataset.posProfile;
	const newId = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

	let root = null;
	let state = null;
	let searchTimer = null;

	// ---------------------------------------------------------------- state

	function money(v) {
		return format_currency(v || 0, state.sale?.currency);
	}

	function picked(line) {
		const p = state.picked[line.si_detail];
		const returned = line.serial_nos.length ? p.serials.size : p.returned;
		return { returned, cancelled: p.cancelled };
	}

	function totals() {
		let value = 0;
		for (const line of state.sale.lines) {
			const { returned, cancelled } = picked(line);
			value += line.unit_value * (returned + cancelled);
		}
		value = round(value);
		const clears = round(Math.min(Math.max(state.sale.outstanding_amount, 0), value));
		const most = round(value - clears);
		let refund = most;
		if (state.payMode === "credit") refund = 0;
		if (state.payMode === "part") refund = Math.min(Math.max(+state.partAmount || 0, 0), most);
		return { value, clears, most, refund: round(refund), credit: round(most - refund) };
	}

	function anything() {
		return state.sale.lines.some((l) => {
			const { returned, cancelled } = picked(l);
			return returned + cancelled > 0;
		});
	}

	// ---------------------------------------------------------------- views

	function shell(title, body, footer) {
		return `<div class="dmr-modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
			<div class="dmr-head"><h3>${esc(title)}</h3>
				<button type="button" class="dmr-x" data-act="close" aria-label="${__("Close")}">&times;</button></div>
			<div class="dmr-body">${body}</div>
			${footer ? `<div class="dmr-foot">${footer}</div>` : ""}</div>`;
	}

	function findView() {
		const rows = (state.results || []).map((r) => {
			const owed = r.outstanding_amount > 0
				? `<span class="dmr-pill dmr-red">${__("Owes")} ${format_currency(r.outstanding_amount, r.currency)}</span>`
				: `<span class="dmr-pill dmr-green">${__("Paid")}</span>`;
			const kind = r.sales_order
				? `<span class="dmr-pill dmr-blue">${__("Delivery")} · ${esc(r.sales_order)}</span>`
				: `<span class="dmr-pill">${__("Branch")}</span>`;
			return `<button type="button" class="dmr-sale" data-act="pick" data-name="${esc(r.name)}">
				<div class="dmr-sale-top"><b>${esc(r.customer_name || r.customer)}</b>
					<span>${format_currency(r.rounded_total || r.grand_total, r.currency)}</span></div>
				<div class="dmr-sale-sub">${esc(r.name)} · ${esc(frappe.datetime.str_to_user(r.posting_date))}
					${r.contact_mobile ? " · " + esc(r.contact_mobile) : ""}</div>
				<div class="dmr-sale-tags">${kind}${owed}</div></button>`;
		}).join("");
		const empty = state.results && !state.results.length
			? `<p class="dmr-muted">${__("No sale found. Try the invoice number, order number, customer name or mobile.")}</p>` : "";
		return shell(__("Return / Exchange"), `
			<label class="dmr-label" for="dmr-q">${__("Find the sale")}</label>
			<input id="dmr-q" class="dmr-input" type="search" autocomplete="off" value="${esc(state.query || "")}"
				placeholder="${__("Invoice / order number, customer name or mobile")}">
			<div class="dmr-list">${rows}${empty}</div>`);
	}

	function stepper(line, field, max, label) {
		const value = state.picked[line.si_detail][field];
		return `<div class="dmr-step"><span class="dmr-step-label">${label}</span>
			<button type="button" data-act="dec" data-row="${esc(line.si_detail)}" data-field="${field}" ${value <= 0 ? "disabled" : ""}>−</button>
			<b>${value}</b>
			<button type="button" data-act="inc" data-row="${esc(line.si_detail)}" data-field="${field}" ${value >= max ? "disabled" : ""}>+</button>
			<span class="dmr-muted">${__("of {0}", [max])}</span></div>`;
	}

	function lineView(line) {
		const p = state.picked[line.si_detail];
		const parts = [];
		if (line.max_return > 0) {
			if (line.serial_nos.length) {
				parts.push(`<div class="dmr-step-label">${__("Tap the serials coming back")}</div><div class="dmr-chips">` +
					line.serial_nos.map((sn) => `<button type="button" class="dmr-chip ${p.serials.has(sn) ? "on" : ""}"
						data-act="serial" data-row="${esc(line.si_detail)}" data-sn="${esc(sn)}">${esc(sn)}</button>`).join("") + "</div>");
			} else {
				parts.push(stepper(line, "returned", line.max_return, __("Brought back")));
			}
		}
		if (line.max_cancel > 0) parts.push(stepper(line, "cancelled", line.max_cancel, __("Cancel (not delivered)")));
		if (!parts.length) {
			parts.push(`<span class="dmr-muted">${line.returned_before >= line.qty ? __("Already returned") : __("Nothing to return yet")}</span>`);
		}
		return `<div class="dmr-line"><div class="dmr-line-top"><b>${esc(line.item_name || line.item_code)}</b>
			<span>${line.qty} × ${money(line.unit_value)}</span></div>${parts.join("")}</div>`;
	}

	function returnView() {
		const s = state.sale;
		const t = totals();
		const stores = s.stores.map((w) => `<option ${w === state.store ? "selected" : ""}>${esc(w)}</option>`).join("");
		const reasons = REASONS.map((r) => `<option value="${esc(r)}" ${r === state.reason ? "selected" : ""}>${__(r)}</option>`).join("");
		const modes = [["all", __("Give back cash")], ["credit", __("Keep as credit (exchange)")], ["part", __("Part cash")]]
			.map(([k, label]) => `<button type="button" class="dmr-mode ${state.payMode === k ? "on" : ""}" data-act="mode" data-mode="${k}">${label}</button>`).join("");
		const ok = anything() && state.reason && (state.payMode !== "part" || t.refund > 0);
		const body = `
			<div class="dmr-sale-head"><div><b>${esc(s.customer_name)}</b><div class="dmr-muted">${esc(s.name)}
				${s.sales_order ? " · " + __("Delivery order") + " " + esc(s.sales_order) : " · " + __("Branch sale")}</div></div>
				<button type="button" class="dmr-link" data-act="back">${__("Change sale")}</button></div>
			<div class="dmr-lines">${s.lines.map(lineView).join("")}</div>
			<div class="dmr-grid">
				<div><label class="dmr-label" for="dmr-store">${__("Received into store")}</label>
					<select id="dmr-store" class="dmr-input">${stores}</select></div>
				<div><label class="dmr-label" for="dmr-reason">${__("Reason")}</label>
					<select id="dmr-reason" class="dmr-input"><option value=""></option>${reasons}</select></div>
			</div>
			<label class="dmr-label" for="dmr-notes">${__("Notes")}</label>
			<textarea id="dmr-notes" class="dmr-input" rows="2">${esc(state.notes || "")}</textarea>
			<div class="dmr-money">
				<div class="dmr-row"><span>${__("Return value")}</span><b>${money(t.value)}</b></div>
				${t.clears > 0 ? `<div class="dmr-row"><span>${__("Clears what the customer still owes")}</span><b>− ${money(t.clears)}</b></div>` : ""}
				<div class="dmr-row dmr-strong"><span>${__("Customer gets")}</span><b>${money(t.most)}</b></div>
				${t.most > 0 ? `<div class="dmr-modes">${modes}</div>` : ""}
				${t.most > 0 && state.payMode === "part" ? `<label class="dmr-label" for="dmr-part">${__("Cash now (the rest stays as credit)")}</label>
					<input id="dmr-part" class="dmr-input" type="number" min="0" step="0.001" max="${t.most}" value="${esc(state.partAmount ?? "")}">` : ""}
				${t.most > 0 ? `<div class="dmr-row"><span>${__("Cash from the drawer")}</span><b>${money(t.refund)}</b></div>
					<div class="dmr-row"><span>${__("Kept as credit")}</span><b>${money(t.credit)}</b></div>` : ""}
			</div>
			${state.error ? `<div class="dmr-error">${state.error}</div>` : ""}`;
		const footer = `<button type="button" class="dmr-btn" data-act="close">${__("Cancel")}</button>
			<button type="button" class="dmr-btn dmr-primary" data-act="submit" ${ok ? "" : "disabled"}>${__("Confirm return")}</button>`;
		return shell(__("Return / Exchange"), body, footer);
	}

	function doneView() {
		const r = state.result;
		const cur = r.currency;
		const body = `<div class="dmr-done">
			<div class="dmr-tick" aria-hidden="true">✓</div>
			<h4>${__("Return saved")} · ${esc(r.name)}</h4>
			${r.refunded_amount > 0 ? `<p class="dmr-big">${__("Give the customer {0} from the drawer", [format_currency(r.refunded_amount, cur)])}</p>` : ""}
			${r.credit_amount > 0 ? `<p>${__("{0} kept as credit: in Payments turn on <b>Customer Credit</b> on the next sale.", [format_currency(r.credit_amount, cur)])}</p>` : ""}
			${r.cleared_amount > 0 ? `<p class="dmr-muted">${__("{0} cleared what the customer owed.", [format_currency(r.cleared_amount, cur)])}</p>` : ""}
			<p class="dmr-muted">${__("Credit Note")} ${esc(r.credit_note)}${r.return_delivery_notes.length ? " · " + __("Return Delivery Note") + " " + r.return_delivery_notes.map(esc).join(", ") : ""}</p>
		</div>`;
		const footer = `<button type="button" class="dmr-btn" data-act="print">${__("Print credit note")}</button>
			<button type="button" class="dmr-btn" data-act="close">${__("Close")}</button>
			<button type="button" class="dmr-btn dmr-primary" data-act="newsale">${__("New sale for this customer")}</button>`;
		return shell(__("Return / Exchange"), body, footer);
	}

	function render() {
		if (!root) {
			root = document.createElement("div");
			root.className = "dmr-overlay";
			root.addEventListener("click", onClick);
			root.addEventListener("input", onInput);
			root.addEventListener("change", onInput);
			root.addEventListener("keydown", (e) => e.key === "Escape" && close());
			document.body.appendChild(root);
		}
		const focus = document.activeElement?.id;
		root.innerHTML = { find: findView, return: returnView, done: doneView }[state.step]();
		const el = focus && root.querySelector("#" + focus);
		if (el) {
			el.focus();
			if (el.setSelectionRange && el.type !== "number") el.setSelectionRange(el.value.length, el.value.length);
		} else if (state.step === "find") root.querySelector("#dmr-q")?.focus();
	}

	// ---------------------------------------------------------------- actions

	function open() {
		if (!profile()) return frappe.show_alert({ message: __("Open the POS first."), indicator: "orange" });
		state = { step: "find", query: "", results: null };
		render();
		search();
	}

	function close() {
		root?.remove();
		root = null;
		state = null;
	}

	function search() {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(async () => {
			const query = state.query;
			const results = await call("search_sales", { pos_profile: profile(), query });
			if (state?.step === "find" && state.query === query) {
				state.results = results;
				render();
			}
		}, 250);
	}

	async function pick(invoice) {
		const sale = await call("get_sale", { pos_profile: profile(), invoice });
		const picked = {};
		for (const line of sale.lines) picked[line.si_detail] = { returned: 0, cancelled: 0, serials: new Set() };
		Object.assign(state, { step: "return", sale, picked, store: sale.default_store, reason: "", notes: "",
			payMode: "all", partAmount: null, error: null, requestId: newId() });
		render();
	}

	async function submit() {
		const t = totals();
		const lines = state.sale.lines.map((line) => {
			const { returned, cancelled } = picked(line);
			return { si_detail: line.si_detail, returned_qty: returned, cancelled_qty: cancelled,
				serial_nos: [...state.picked[line.si_detail].serials] };
		}).filter((l) => l.returned_qty + l.cancelled_qty > 0);
		const data = { sales_invoice: state.sale.name, pos_profile: profile(), lines, warehouse: state.store,
			reason: state.reason, notes: state.notes, request_id: state.requestId };
		if (state.payMode === "all") data.refund_all = 1;
		else data.refund_amount = state.payMode === "credit" ? 0 : t.refund;
		try {
			state.result = await call("submit_return", { data: JSON.stringify(data) }, true);
			state.step = "done";
		} catch (e) {
			state.error = __("The return was not saved. Check the message and try again.");
		}
		render();
	}

	function newSale() {
		const app = (() => {
			let node = document.querySelector('[data-test="pos-navbar"]');
			while (node && !node.__vue_app__) node = node.parentElement;
			return node && node.__vue_app__;
		})();
		const customers = app?.config?.globalProperties?.$pinia?._s.get("customers");
		const { customer, credit_amount: credit, currency } = state.result;
		close();
		customers?.setSelectedCustomer?.(customer);
		if (credit > 0) {
			frappe.show_alert({ message: __("Credit {0} is waiting: in Payments turn on Customer Credit.",
				[format_currency(credit, currency)]), indicator: "green" }, 8);
		}
	}

	function onClick(e) {
		if (e.target === root) return close();
		const btn = e.target.closest("[data-act]");
		if (!btn || btn.disabled) return;
		const act = btn.dataset.act;
		const row = btn.dataset.row && state.picked?.[btn.dataset.row];
		if (act === "close") close();
		else if (act === "pick") pick(btn.dataset.name);
		else if (act === "back") {
			state.step = "find";
			render();
			search();
		}
		else if (act === "inc" || act === "dec") {
			row[btn.dataset.field] += act === "inc" ? 1 : -1;
			render();
		} else if (act === "serial") {
			row.serials.has(btn.dataset.sn) ? row.serials.delete(btn.dataset.sn) : row.serials.add(btn.dataset.sn);
			render();
		} else if (act === "mode") {
			state.payMode = btn.dataset.mode;
			render();
		} else if (act === "submit") submit();
		else if (act === "print") {
			window.open(`/printview?doctype=Sales%20Invoice&name=${encodeURIComponent(state.result.credit_note)}&trigger_print=1`);
		} else if (act === "newsale") newSale();
	}

	function onInput(e) {
		const id = e.target.id;
		if (id === "dmr-q") {
			state.query = e.target.value;
			search();
		} else if (id === "dmr-store") state.store = e.target.value;
		else if (id === "dmr-reason") {
			state.reason = e.target.value;
			render();
		} else if (id === "dmr-notes") state.notes = e.target.value;
		else if (id === "dmr-part") {
			state.partAmount = e.target.value;
			if (e.type === "change") render();
		}
	}

	// ---------------------------------------------------------------- header button

	function inject() {
		const nav = document.querySelector('[data-test="pos-navbar"]');
		if (!nav || nav.querySelector("#" + BTN_ID)) return;
		const anchor = nav.querySelector(".status-entry-surface");
		if (!anchor) return;
		const btn = document.createElement("button");
		btn.id = BTN_ID;
		btn.type = "button";
		btn.className = "dmr-open";
		btn.title = __("Return or exchange goods of a sale");
		btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2"
			stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-4"/></svg>
			<span>${__("Return / Exchange")}</span>`;
		btn.addEventListener("click", open);
		anchor.parentElement.insertBefore(btn, anchor);
	}

	new MutationObserver(() => {
		if (location.pathname.includes("posapp")) inject();
	}).observe(document.documentElement, { childList: true, subtree: true });
})();
