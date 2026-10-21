// delivery_management: a search field in the desk header, like v14/v15.
// v16 moved the search to a sidebar button that opens a modal; this field just
// opens that same modal, so Frappe's own search is reused. Desk only (POS Awesome
// does not load desk assets).
(function () {
	const BTN = "#navbar-modal-search, [id='navbar-modal-search']";

	function open_search() {
		const $btn = $(BTN).first();
		if ($btn.length) return $btn.trigger("click");
		// fallback: Ctrl+K handler
		document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }));
	}

	function mount() {
		// every page (list, form, report...) has its own .page-head, so add one field to each.
		const hint = /Mac/i.test(navigator.platform) ? "⌘K" : "Ctrl+K";
		$(".page-head-content").each(function () {
			const $host = $(this);
			if ($host.find(".dm-header-search").length) return;
			const $el = $(`<div class="dm-header-search" role="search">
				<span class="dm-hs-icon">${frappe.utils.icon("search", "sm")}</span>
				<input type="text" readonly class="form-control" placeholder="${__("Search or type a command")}" aria-label="${__("Search")}">
				<kbd>${hint}</kbd></div>`);
			$el.on("click focusin", "input", (e) => { e.preventDefault(); $(e.target).blur(); open_search(); });
			$host.find(".standard-items-section").first().before($el);
		});
	}

	$(document).on("page-change", () => setTimeout(mount, 50));
	$(document).on("toolbar_setup", () => setTimeout(mount, 300));
	setInterval(mount, 1500); // page-head is re-rendered per route; mount() is a no-op if present
})();
