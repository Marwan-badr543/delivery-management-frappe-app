// delivery_management: "Return / Exchange" in the POS Awesome header.
// 1. Find the sale (invoice / order number, customer name or mobile).
// 2. Tap what comes back: goods the customer brings (serials by tapping them) and, on a
//    delivery order, goods not delivered yet to cancel. Choose the store that receives
//    them, the reason, and how the customer is paid: all in cash, keep as credit
//    (exchange), or part cash.
// 3. Done: the documents are made (api.returns.submit_return, see returns/service.py).
//    "New sale for this customer" starts the exchange; the credit is offered in Payments.
// An unpaid delivery order (draft invoice) has nothing to give back: its bill goes down.
//
// "Customer money" tab: find the customer, then take a payment on what they owe (e.g. an
// unpaid order) or pay out their credit in cash, with the reason. Both go in the
// cashier's shift (orders/payments.py).
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
		return format_currency(v || 0, state.sale?.currency || state.cm?.currency);
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
		if (state.sale.is_draft) return { value, clears: value, most: 0, refund: 0, credit: 0 };
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

	function tabs() {
		return `<div class="dmr-tabs" role="tablist">${[["sale", __("Return / Exchange")], ["money", __("Customer money")]]
			.map(([k, label]) => `<button type="button" role="tab" aria-selected="${state.tab === k}"
				class="dmr-tab ${state.tab === k ? "on" : ""}" data-act="tab" data-tab="${k}">${label}</button>`).join("")}</div>`;
	}

	function findView() {
		if (state.tab === "money") return customersView();
		const rows = (state.results || []).map((r) => {
			const owed = !r.docstatus
				? `<span class="dmr-pill dmr-red">${__("Not paid yet")} ${format_currency(r.outstanding_amount, r.currency)}</span>`
				: r.outstanding_amount > 0
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
		return shell(__("Return / Exchange"), `${tabs()}
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
				${s.is_draft ? `<p class="dmr-muted">${__("Not paid yet: nothing is given back, the order's bill goes down by {0}.", [money(t.value)])}</p>` : ""}
				${t.clears > 0 && !s.is_draft ? `<div class="dmr-row"><span>${__("Clears what the customer still owes")}</span><b>− ${money(t.clears)}</b></div>` : ""}
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
			${r.cleared_amount > 0 ? `<p class="dmr-muted">${__("{0} off what the customer owes.", [format_currency(r.cleared_amount, cur)])}</p>` : ""}
			${r.tv_accounts.length ? `<p class="dmr-muted">${__("TV accounts deleted: {0}", [r.tv_accounts.map(esc).join(", ")])}</p>` : ""}
			<p class="dmr-muted">${[r.credit_note && __("Credit Note") + " " + esc(r.credit_note),
				r.return_delivery_notes.length && __("Return Delivery Note") + " " + r.return_delivery_notes.map(esc).join(", ")]
				.filter(Boolean).join(" · ")}</p>
		</div>`;
		const footer = `${r.credit_note ? `<button type="button" class="dmr-btn" data-act="print">${__("Print credit note")}</button>` : ""}
			<button type="button" class="dmr-btn" data-act="close">${__("Close")}</button>
			<button type="button" class="dmr-btn dmr-primary" data-act="newsale">${__("New sale for this customer")}</button>`;
		return shell(__("Return / Exchange"), body, footer);
	}

	function customersView() {
		const rows = (state.customers || []).map((c) => `<button type="button" class="dmr-sale" data-act="customer" data-name="${esc(c.name)}">
			<div class="dmr-sale-top"><b>${esc(c.customer_name || c.name)}</b><span class="dmr-muted">${esc(c.mobile_no || "")}</span></div>
			<div class="dmr-sale-tags">${c.credit > 0 ? `<span class="dmr-pill dmr-green">${__("Credit")} ${money(c.credit)}</span>` : ""}
				${c.due > 0 ? `<span class="dmr-pill dmr-red">${__("Owes")} ${money(c.due)}</span>` : ""}</div></button>`).join("");
		const empty = state.customers && !state.customers.length ? `<p class="dmr-muted">${__("No customer found.")}</p>` : "";
		return shell(__("Return / Exchange"), `${tabs()}
			<label class="dmr-label" for="dmr-cq">${__("Find the customer")}</label>
			<input id="dmr-cq" class="dmr-input" type="search" autocomplete="off" value="${esc(state.cquery || "")}"
				placeholder="${__("Customer name or mobile")}">
			<div class="dmr-list">${rows}${empty}</div>`);
	}

	function modeSelect(id, value) {
		return `<select id="${id}" class="dmr-input">${state.cm.modes.map((m) =>
			`<option ${m === value ? "selected" : ""}>${esc(m)}</option>`).join("")}</select>`;
	}

	function moneyView() {
		const c = state.cm;
		const due = c.due.map((d) => `<label class="dmr-due ${state.dueInvoice === d.name ? "on" : ""}">
			<input type="radio" name="dmr-due" value="${esc(d.name)}" ${state.dueInvoice === d.name ? "checked" : ""}>
			<span><b>${esc(d.sales_order || d.name)}</b> · ${esc(frappe.datetime.str_to_user(d.posting_date))}
				${d.docstatus ? "" : `<span class="dmr-pill dmr-red">${__("Not paid yet")}</span>`}</span>
			<b>${money(d.outstanding_amount)}</b></label>`).join("");
		const body = `<div class="dmr-sale-head"><div><b>${esc(c.customer_name)}</b></div>
				<button type="button" class="dmr-link" data-act="back">${__("Change customer")}</button></div>
			<section class="dmr-money">
				<div class="dmr-row dmr-strong"><span>${__("Owes")}</span><b>${money(c.due.reduce((a, d) => a + d.outstanding_amount, 0))}</b></div>
				${c.due.length ? `<div class="dmr-dues">${due}</div>
					<div class="dmr-grid"><div><label class="dmr-label" for="dmr-pay">${__("Amount received")}</label>
						<input id="dmr-pay" class="dmr-input" type="number" min="0" step="0.001" value="${esc(state.payAmount ?? "")}"></div>
					<div><label class="dmr-label" for="dmr-paymode">${__("Paid by")}</label>${modeSelect("dmr-paymode", state.payWith)}</div></div>
					<button type="button" class="dmr-btn dmr-primary dmr-wide" data-act="collect" ${+state.payAmount > 0 ? "" : "disabled"}>${__("Take payment")}</button>`
				: `<p class="dmr-muted">${__("Nothing to pay.")}</p>`}
			</section>
			<section class="dmr-money">
				<div class="dmr-row dmr-strong"><span>${__("Credit (from returns)")}</span><b>${money(c.credit)}</b></div>
				${c.credit > 0 ? `<div class="dmr-grid"><div><label class="dmr-label" for="dmr-out">${__("Pay out from the drawer")}</label>
						<input id="dmr-out" class="dmr-input" type="number" min="0" step="0.001" max="${c.credit}" value="${esc(state.outAmount ?? "")}"></div>
					<div><label class="dmr-label" for="dmr-outmode">${__("Paid by")}</label>${modeSelect("dmr-outmode", state.outWith)}</div></div>
					<label class="dmr-label" for="dmr-why">${__("Reason")}</label>
					<input id="dmr-why" class="dmr-input" type="text" value="${esc(state.outReason || "")}"
						placeholder="${__("e.g. returned yesterday, came back for the cash")}">
					<button type="button" class="dmr-btn dmr-primary dmr-wide" data-act="payout"
						${+state.outAmount > 0 && +state.outAmount <= c.credit && (state.outReason || "").trim() ? "" : "disabled"}>${__("Pay out credit")}</button>`
				: `<p class="dmr-muted">${__("No credit to pay out.")}</p>`}
			</section>
			${state.error ? `<div class="dmr-error">${state.error}</div>` : ""}`;
		return shell(__("Customer money"), body);
	}

	function moneyDoneView() {
		const r = state.result;
		const text = r.payment_type === "Pay" ? __("Give the customer {0} from the drawer", [money(r.amount)])
			: __("Put {0} into the drawer", [money(r.amount)]);
		const body = `<div class="dmr-done"><div class="dmr-tick" aria-hidden="true">✓</div>
			<h4>${__("Saved")} · ${esc(r.payment_entry)}</h4>
			<p class="dmr-big">${r.mode_of_payment === state.cm.modes[0] ? text : `${text} (${esc(r.mode_of_payment)})`}</p></div>`;
		const footer = `<button type="button" class="dmr-btn" data-act="close">${__("Close")}</button>
			<button type="button" class="dmr-btn dmr-primary" data-act="customer" data-name="${esc(r.party)}">${__("Back to the customer")}</button>`;
		return shell(__("Customer money"), body, footer);
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
		root.innerHTML = { find: findView, return: returnView, done: doneView, money: moneyView,
			moneyDone: moneyDoneView }[state.step]();
		const el = focus && root.querySelector("#" + focus);
		if (el) {
			el.focus();
			if (el.setSelectionRange && el.type !== "number") el.setSelectionRange(el.value.length, el.value.length);
		} else if (state.step === "find") root.querySelector(state.tab === "money" ? "#dmr-cq" : "#dmr-q")?.focus();
	}

	// ---------------------------------------------------------------- actions

	function open() {
		if (!profile()) return frappe.show_alert({ message: __("Open the POS first."), indicator: "orange" });
		state = { step: "find", tab: "sale", query: "", results: null, cquery: "", customers: null };
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
			const money = state.tab === "money";
			const query = money ? state.cquery : state.query;
			const results = await call(money ? "search_customers" : "search_sales", { pos_profile: profile(), query });
			if (state?.step === "find" && (money ? state.cquery : state.query) === query) {
				state[money ? "customers" : "results"] = results;
				render();
			}
		}, 250);
	}

	async function openCustomer(customer) {
		const cm = await call("get_customer_money", { pos_profile: profile(), customer });
		const first = cm.due[0];
		Object.assign(state, { step: "money", cm, error: null, dueInvoice: first?.name, payAmount: first?.outstanding_amount,
			payWith: cm.modes[0], outAmount: null, outWith: cm.modes[0], outReason: "", requestId: newId() });
		render();
	}

	async function moneyAction(method, data) {
		try {
			state.result = await call(method, { data: JSON.stringify({ pos_profile: profile(), request_id: state.requestId, ...data }) }, true);
			state.step = "moneyDone";
		} catch (e) {
			state.error = __("Not saved. Check the message and try again.");
		}
		render();
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
		if (state.payMode === "all" && !state.sale.is_draft) data.refund_all = 1;
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
		} else if (act === "tab") {
			state.tab = btn.dataset.tab;
			render();
			search();
		} else if (act === "customer") openCustomer(btn.dataset.name);
		else if (act === "collect") {
			moneyAction("collect_payment", { invoice: state.dueInvoice, amount: +state.payAmount, mode_of_payment: state.payWith });
		} else if (act === "payout") {
			moneyAction("pay_out_credit", { customer: state.cm.customer, amount: +state.outAmount, mode_of_payment: state.outWith,
				reason: state.outReason });
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
		} else if (id === "dmr-cq") {
			state.cquery = e.target.value;
			search();
		} else if (e.target.name === "dmr-due") {
			state.dueInvoice = e.target.value;
			state.payAmount = state.cm.due.find((d) => d.name === e.target.value)?.outstanding_amount;
			render();
		} else if (["dmr-pay", "dmr-out", "dmr-why"].includes(id)) {
			state[{ "dmr-pay": "payAmount", "dmr-out": "outAmount", "dmr-why": "outReason" }[id]] = e.target.value;
			const btn = root.querySelector(id === "dmr-pay" ? '[data-act="collect"]' : '[data-act="payout"]');
			if (btn) btn.disabled = id === "dmr-pay" ? !(+state.payAmount > 0)
				: !(+state.outAmount > 0 && +state.outAmount <= state.cm.credit && (state.outReason || "").trim());
		} else if (id === "dmr-paymode") state.payWith = e.target.value;
		else if (id === "dmr-outmode") state.outWith = e.target.value;
		else if (id === "dmr-store") state.store = e.target.value;
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
		btn.title = __("Return or exchange goods of a sale, or take / pay out a customer's money");
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
