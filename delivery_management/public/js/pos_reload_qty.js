// delivery_management: "Reload qty" button in the POS Awesome header.
// Reloads the sellable qty of the items on screen and in the cart from the store of the
// current type: Invoice = this branch's warehouse, Order = the main store.
(() => {
        const BTN_ID = "dm-reload-qty";

        function posApp() {
                let node = document.querySelector('[data-test="pos-navbar"]');
                while (node && !node.__vue_app__) node = node.parentElement;
                return node && node.__vue_app__;
        }

        function stores() {
                const pinia = posApp()?.config?.globalProperties?.$pinia;
                return pinia && { delivery: pinia._s.get("delivery"), invoice: pinia._s.get("invoice") };
        }

        async function reload(btn) {
                const s = stores();
                const profile = document.querySelector('[data-test="pos-navbar"]')?.dataset.posProfile;
                if (!s?.delivery || !profile) return;
                const isOrder = s.invoice?.invoiceType === "Order";
                btn.disabled = true;
                btn.classList.add("dm-spin");
                try {
                        await s.delivery.stockChanged((s.invoice?.items || []).map((r) => r.item_code), profile);
                        frappe.show_alert({ message: isOrder ? __("Main store qty reloaded") : __("Branch qty reloaded"), indicator: "green" }, 3);
                } finally {
                        btn.disabled = false;
                        btn.classList.remove("dm-spin");
                }
        }

        function inject() {
                const nav = document.querySelector('[data-test="pos-navbar"]');
                if (!nav || nav.querySelector("#" + BTN_ID)) return;
                const anchor = nav.querySelector(".status-entry-surface");
                if (!anchor) return;
                const btn = document.createElement("button");
                btn.id = BTN_ID;
                btn.type = "button";
                btn.className = "dm-reload-qty";
                btn.title = __("Reload available qty from the store of the current type (Invoice: branch, Order: main store)");
                btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2"
                        stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>
                        <span>${__("Reload Qty")}</span>`;
                btn.addEventListener("click", () => reload(btn));
                anchor.parentElement.insertBefore(btn, anchor);
        }

        const style = document.createElement("style");
        style.textContent = `
                .dm-reload-qty { display:inline-flex; align-items:center; gap:6px; height:32px; padding:0 12px; margin:0 6px;
                        border-radius:16px; border:1px solid rgba(31,111,235,.35); background:rgba(31,111,235,.08); color:#1F6FEB;
                        font-weight:600; font-size:13px; cursor:pointer; white-space:nowrap; }
                .dm-reload-qty:hover { background:rgba(31,111,235,.16); }
                .dm-reload-qty:disabled { opacity:.6; cursor:wait; }
                .dm-reload-qty.dm-spin svg { animation: dm-spin .8s linear infinite; }
                @keyframes dm-spin { to { transform: rotate(360deg); } }
                @media (max-width: 768px) { .dm-reload-qty span { display:none; } .dm-reload-qty { padding:0 8px; } }`;
        document.head.appendChild(style);

        new MutationObserver(() => {
                if (location.pathname.includes("posapp")) inject();
        }).observe(document.documentElement, { childList: true, subtree: true });
})();
