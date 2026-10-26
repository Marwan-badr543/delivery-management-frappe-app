// delivery_management: helpers shared by the Driver Assignment, Delivery Schedule and
// Installation Statistics pages (window.dm_drivers).
(function () {
	const API = "delivery_management.api.drivers.";
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));

	function call(method, args, opts = {}) {
		return frappe
			.call({ method: API + method, args, type: opts.post ? "POST" : "GET", freeze: !!opts.freeze })
			.then((r) => r.message);
	}

	function date(d) {
		return d ? frappe.datetime.str_to_user(d) : "";
	}

	function time(t) {
		return t ? moment(String(t).slice(0, 8), "HH:mm:ss").format("h:mm A") : "";
	}

	function relativeDay(d) {
		if (!d) return "";
		const diff = frappe.datetime.get_day_diff(d, frappe.datetime.get_today());
		if (diff === 0) return __("Today");
		if (diff === 1) return __("Tomorrow");
		if (diff === -1) return __("Yesterday");
		return moment(d).format("ddd");
	}

	function money(v, currency) {
		return format_currency(v || 0, currency || frappe.defaults.get_default("currency"));
	}

	function paymentPill(p) {
		if (!p || p.status === "No Invoice") return `<span class="dd-pill dd-pill-gray">${__("No invoice")}</span>`;
		const cls = { Paid: "green", "Partly Paid": "orange", Unpaid: "red" }[p.status] || "gray";
		const extra = p.outstanding > 0 ? `<span class="dd-sub">${__("Due")}: ${money(p.outstanding, p.currency)}</span>` : "";
		return `<span class="dd-pill dd-pill-${cls}">${__(p.status)}</span>${extra}`;
	}

	function statusPill(row) {
		const ac = row.order_type === "AC";
		if (row.dm_delivery_status === "On the way")
			return `<span class="dd-pill dd-pill-blue">${frappe.utils.icon("truck", "xs")} ${__("On the way")}</span>`;
		if (row.docstatus === 1)
			return `<span class="dd-pill dd-pill-green">${frappe.utils.icon("check", "xs")} ${ac ? __("Installed") : __("Delivered")}</span>`;
		return `<span class="dd-pill dd-pill-orange">${ac ? __("Pending install") : __("Pending delivery")}</span>`;
	}

	function typePill(type) {
		if (type === "AC") return `<span class="dd-pill dd-pill-blue">${__("AC")}</span>`;
		if (type === "Non AC") return `<span class="dd-pill dd-pill-gray">${__("Non AC")}</span>`;
		return `<span class="dd-pill dd-pill-red">${__("Choose type")}</span>`;
	}

	function phoneLink(phone) {
		return phone ? `<a href="tel:${esc(phone)}" onclick="event.stopPropagation()">${esc(phone)}</a>` : "";
	}

	function whenCell(row) {
		const d = row.work_date;
		if (!d) return `<span class="dd-muted">${__("Not scheduled")}</span>`;
		const t = row.order_type === "AC" ? __("Installation day") : time(row.delivery_time);
		return `<span class="dd-when">${relativeDay(d)} · ${date(d)}</span><span class="dd-sub">${esc(t)}</span>`;
	}

	function emptyRow(cols, title, text) {
		return `<tr><td colspan="${cols}"><div class="dd-empty">${frappe.utils.icon("es-line-filter", "lg")}
			<b>${esc(title)}</b><span>${esc(text)}</span></div></td></tr>`;
	}

	function skeleton(cols, n = 5) {
		return Array.from({ length: n }, () =>
			`<tr class="dd-skeleton">${"<td><div></div></td>".repeat(cols)}</tr>`).join("");
	}

	function tabs(items, active) {
		return `<div class="dd-tabs" role="tablist">${items
			.map((t) => `<button class="dd-tab-btn ${t.key === active ? "active" : ""}" role="tab" data-tab="${t.key}">
				${frappe.utils.icon(t.icon, "sm")}<span>${esc(t.label)}</span><span class="dd-badge" data-count="${t.key}">–</span></button>`)
			.join("")}</div>`;
	}

	function select(options, value) {
		return options
			.map((o) => `<option value="${esc(o.value)}" ${String(o.value) === String(value ?? "") ? "selected" : ""}>${esc(o.label)}</option>`)
			.join("");
	}

	// Remember filters per page (per browser) so drivers don't re-enter them.
	function prefs(key, value) {
		try {
			if (value === undefined) return JSON.parse(localStorage.getItem("dm_" + key) || "{}");
			localStorage.setItem("dm_" + key, JSON.stringify(value));
		} catch (e) {
			return {};
		}
	}

	function areas() {
		if (!areas._p)
			areas._p = call("get_areas").catch(() => []);
		return areas._p;
	}

	/** Initials of the person's full name (frappe.avatar only knows users already in the boot info). */
	function avatar(name) {
		return `<span class="avatar avatar-small"><span class="avatar-frame standard-image">${esc(frappe.get_abbr(name || "?"))}</span></span>`;
	}

	window.dm_drivers = {
		avatar,
		call, esc, date, time, relativeDay, money, paymentPill, statusPill, typePill, phoneLink, whenCell,
		emptyRow, skeleton, tabs, select, prefs, areas,
	};
})();
