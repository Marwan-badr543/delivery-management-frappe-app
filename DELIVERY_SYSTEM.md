# Delivery Management: what was built and how it works

App: `apps/delivery_management` (**Frappe v16 / ERPNext v16**, bench `~/frappe/frappe-bench-16`, site `erp16.localhost`), plus changes to `apps/posawesome` ([POS-Awesome-V15](https://github.com/defendicon/POS-Awesome-V15), branch `develop`).

> **Ported from v14 (2026-10-08).** The business logic is the same as the v14 build; section *0.1 What changed for v16* lists every difference.

## 0.1 What changed for v16

| Area | v14 | v16 |
|---|---|---|
| POS submit | `posawesome...posapp.submit_invoice(..., force_sync=True)` (we patched POS Awesome) | `posawesome...invoice_processing.creation.submit_invoice(..., submit_in_background=False)`: no POS Awesome Python patch needed |
| Sellable serial | `status = Active` and no `delivery_document_no` / `sales_invoice` | v16 Serial No has neither field. Sellable = `status = Active` and still in a warehouse (sold or delivered serials leave the warehouse). Oldest first by `posting_date` (v16 has no `purchase_date`) |
| Serial settings | n/a | Stock Settings must have **Activate Serial / Batch No for Item** and **Use Serial / Batch Fields** on. The app writes serials into the plain `serial_no` fields and ERPNext builds the bundles from them |
| Server scripts | `server_script_enabled` in the site config | **Not needed any more (§12.2).** The app creates the Delivery Note and reserves serials itself; the three production scripts are disabled by a patch on migrate |
| Salesman (Sales Statistics) | invoice `owner` | V15 lets several cashiers share one login and switch with a PIN; the verified cashier is stored in `posa_cashier`. Salesman = `posa_cashier`, else `owner` |
| POS cashier PIN / shift | none | V15 refuses any sale without an open **POS Opening Shift** and a terminal unlocked by a cashier PIN. Our flow goes through V15's own submit, so this applies to Orders too |
| Retries | n/a | V15 resends a submit with the same `posa_client_request_id` after a network drop. `api.pos.submit` returns the first result instead of creating a second Sales Order (`service.already_submitted`, test `TestRetriedSubmit`) |
| Order drafts | POS drafted a Sales Invoice | Stock V15 drafts a *Sales Order* in Order mode. With this app installed (`frappe.boot.delivery_management.enabled`), the POS keeps a Sales Invoice draft and submits through `delivery_management.api.pos.submit`, which creates the Sales Order itself |
| Request-local cache | `get_value(expires=True)` | v16 also keeps a request-local copy on `set_value`; the stats handoff reads with `use_local_cache=False` |


Scope of this phase: the delivery time-slot engine, AC installation capacity, the
admin page, and POS ordering (Invoice vs Order, automatic serials, stock per store,
delivery address). Nothing else from the old system was moved yet.

---

## 0. Decisions you need to confirm

These are business rules I had to choose. Each one is a setting or a small change.

| # | Decision | What I did | Where to change it |
|---|----------|-----------|--------------------|
| 1 | Which items count as an **AC installation** | Item Group is `Air Conditioner` **or any group under it** (`Magic Air Conditioner`, the COTTING items that sit directly on `Air Conditioner`, ...). | Delivery Settings → AC Item Groups (several groups allowed) |
| 2 | **Accessories** must not use AC capacity | Excluded by default: group `Air Conditioner Accessory` (Copper Pipe, Electric Wire) and item `GAS R32 FILLING`. The gas item is in the same group as the real ACs, so it needs an item-level exclusion. | Delivery Settings → Excluded Item Groups / Excluded Items |
| 3 | Do accessories need a delivery car? | With an AC unit in the order, they travel with the installation and need no extra slot. Without one (accessory bought alone), they need a normal delivery slot. | `slots/cart.py` |
| 4 | Is `COTTING A/C x TON` an installation? | It sits directly in `Air Conditioner`, so it **counts** as an AC unit. If it is a service (removal/cutting), add those items to Excluded Items. | Delivery Settings |
| 5 | Non-stock lines (services, charges) | Never need a slot. | `slots/cart.py` |
| 6 | Lead-time boundary | **Inclusive.** Lead 2h at 15:00 blocks 16:00 **and 17:00**; 18:00 is the first open slot (your example). The old frontend used `<` and kept 17:00 open. It applies across midnight too: at 23:00, a 00:30 slot tomorrow is blocked. | `slots/timeutils.is_too_soon` |
| 7 | Same-day AC installation | Allowed if the day has capacity (as in the old system). Only past days are blocked. | `engine.get_ac_day_status` |
| 8 | Admin "Open" on a **full** slot / AC day | Does **not** override capacity (the old system did, until the next recompute). To allow more, raise cars / day capacity, or use "Allow over capacity" on a manual booking. | engine |
| 9 | When is capacity given back? | When the Sales Order is **cancelled**: directly, or by your `order_cancellation` app when a return invoice is submitted (it cancels the SO, which triggers our hook). Closing an SO does **not** free capacity. | `orders/events.py` |
| 10 | Address fields on Invoice (branch pickup) | Shown and optional. If filled, the address is saved on the customer too. Required only for Orders. | `DeliverySchedule.vue` |

---

## 1. The engine (the core)

`delivery_management/slots/`. **One place** books and frees capacity, usable from anywhere:

```python
from delivery_management.slots import reserve, release, reschedule, profile_cart

profile = profile_cart(so.items)            # ac_units, has_ac, has_non_ac
reserve("Sales Order", so.name,
        area="Manama", delivery_date="2026-10-08", delivery_time="16:00",   # normal goods
        ac_date="2026-10-09", ac_units=profile.ac_units)                    # AC installation
release("Sales Order", so.name)             # idempotent; safe to call twice
reschedule("Sales Order", so.name, ...)     # release + reserve, atomic
move_reservation(reservation_name, ac_date=...)  # move only one part of a mixed order
```

### 1.1 Two kinds of capacity

| | Delivery slot (non-AC goods) | AC day |
|---|---|---|
| Key | (area, date, time) | date |
| Unit | 1 per order | **AC units** (3 ACs in one order = 3) |
| Capacity | `Delivery Area.cars_number` | date override, else `Global AC Daily Capacity` |
| Valid when | area enabled, a work hour exists for that weekday, not too soon, not closed by admin, `booked + 1 ≤ cars` | not past, not closed by admin, `booked + units ≤ capacity` |

A mixed order books **both** in the same transaction. If either fails, neither is kept.

### 1.2 Data model (old model → new doctype)

| Old (SQLAlchemy) | New doctype | Role |
|---|---|---|
| `Areas` | **Delivery Area** | name, `cars_number`, `enabled` |
| `WorkHours` | **Delivery Work Hour** | area + start time + 7 weekday checkboxes (unique area+time) |
| `ManageOrdersNumber` | **Delivery Slot Counter** | running count per (area, date, time); name `area\|date\|HH:MM` |
| `DisabledTime` | **Delivery Disabled Slot** | blocked slots, with a **source**: `Capacity` (engine-owned) or `Manual` (admin-owned) |
| `ManageAcOrderedNumber` + `MaxAcQuantity` | **AC Day Capacity** | per date: reserved units, optional date override, manual close, `is_full` |
| (none) | **Delivery Slot Reservation** | **the ledger**: one row per booking (owner document, slot, qty, Reserved/Released, forced) |
| `settings` table | **Delivery Settings** (Single) | global AC capacity, lead hours, booking window, AC group + exclusions, main store |

### 1.3 Why it is robust (and what changed from the old logic)

1. **The ledger is the source of truth.** Release reads what was actually booked.
   The old `_decrement_ac_order` recomputed AC qty from the current product data, so
   it drifted when products changed. Counters are only a cache of the ledger, and
   `rebuild_from_ledger()` can regenerate them at any time (repair tool).
2. **Check before you book, under a lock.** The old code incremented first and
   disabled the slot afterwards, so two orders could overbook. Now the counter row
   is locked (`SELECT … FOR UPDATE`), the rule is checked, then it is incremented.
   Proven with two real DB connections racing for the last car and for the last
   AC units: exactly one wins, and the other gets "slot is full". See `tests/race_check.py`.
3. **No duplicate rows, no deadlocks.** Rows have deterministic names. A missing row
   is inserted first (inside a savepoint; a concurrent duplicate is absorbed) and
   only then locked. This avoids the gap-lock deadlock that `SELECT … FOR UPDATE`
   on a missing row causes. I hit that deadlock in the race test and fixed it.
4. **Manual blocks are never wiped.** In the old `_check_time_slots`, a
   cancellation deleted *all* DisabledTime rows of the slot, including the admin's
   manual disable. Now the engine only adds or removes `Capacity` rows.
5. **AC check is exact.** `booked + units > capacity` is rejected as a whole
   (18/20 booked + an order of 3 is refused; an order of 2 fills the day).
6. **Capacity changes resync.** Saving an area's cars/enabled, or the global AC
   capacity, recomputes "full" for all future slots/days.
7. **Guards.** A booked document cannot book the same kind twice (use reschedule).
   A ledger row cannot be deleted while Reserved. A work hour, or one of its
   weekdays, cannot be removed while future bookings exist on it.
8. **No commits inside the engine.** The caller's transaction decides, so a POS
   order and its booking succeed or fail together.
9. **Testable time.** Every check takes an optional `now`.

### 1.4 Availability for the UI

`get_delivery_slots(area, date)` returns every working slot with `available`,
`reserved`, `capacity`, `remaining` and machine reasons (`too_soon`, `full`,
`manually_disabled`, `area_disabled`). `get_ac_calendar(start, days, units)` does
the same per day. `reserve()` re-checks the same rules under the lock, so the
listing is advisory and the booking is authoritative.

---

## 2. POS order flow

`delivery_management/orders/service.py`, called by one endpoint,
`delivery_management.api.pos.submit` (POST only). It replaces POS Awesome's
`submit_invoice` call and then **delegates back to it**, so payments, credit,
change and printing behave exactly as before.

### Type = Invoice (customer takes goods from this branch)
1. Every line uses the POS profile's **branch warehouse**, `update_stock = 1`.
2. Stock is checked on the server (never trust the UI).
3. **Serials are picked automatically**, oldest first, from the branch: Active,
   not reserved (Inactive), not invoiced/delivered, rows locked `FOR UPDATE`.
4. The address is optional; if filled, it is saved on the customer.
5. POS Awesome submits the Sales Invoice. No slot is booked.
6. Returns skip all of this and keep POS Awesome's own return logic.

### Type = Order (delivery)
1. Classify the cart: AC units / accessories / other goods (§0).
2. Validate the schedule against the cart: other goods need area+date+time, AC
   needs an AC day, both need both. The address (area, block, road, building) is required.
3. Check stock in the **main store** (`Stores - SHD`), minus open SO reservations.
4. Address: reuse the customer's address when **all** fields match (Address title,
   area, block, road, building), or **add a new one** if anything changed (old
   addresses are kept). Block / Road / Building must be digits. A text-only address
   from the old site (`"MALKIYA, block 1034, road 3479, building 3105"`) is parsed:
   it pre-fills the POS, and when reused its structured fields are filled in.
5. Build the **Sales Order** from the POS lines (same rates, discounts, taxes;
   pricing rules not re-applied), warehouse = main store. **Pick serials** from the
   main store into `Sales Order Item.dm_serial_nos` and mark them **Inactive**
   (= reserved, the convention your `stock_solution` app and server scripts use).
6. Put the schedule on the SO fields (area, slot date/time, AC day).
7. Submit the SO. **Capacity is booked by the Sales Order itself** (`before_submit`
   hook, §2.1), the same as for an order typed in the Desk. Your production server script *"Delivery Note From Sales Order"*
   creates the draft DN, as today. Our `Delivery Note.before_insert` hook copies the
   same serials onto it. It also corrects the DN header from the SO (see §6.1).
8. Link every invoice line to its SO line (`sales_order`, `so_detail`), main-store
   warehouse, the same serials, `update_stock = 0` (stock moves with the DN), then
   POS Awesome submits the **Sales Invoice**.
9. All of this is one transaction. If the slot was just taken, a serial is gone, or
   the payment fails, nothing persists, and the POS keeps the screen so the cashier
   can choose another slot.

Verified on the replicated environment, using the real production server script:
SO → DN (server script) → the warehouse submits the DN with the reserved serials →
serials become Delivered, and the SO is 100% delivered and 100% billed.

### Save/New = order without payment (the usual case)
With Type = Order, **Save/New** does the whole order (steps 1-9: SO submitted,
slot booked, DN created by the server script, Sales Invoice **submitted**) but
takes no payment: the invoice is **Unpaid** with the full outstanding amount, for
the customer to pay later. **PAY** does the same and records the payment. With
Type = Invoice (or a return), Save/New keeps POS Awesome's old behaviour (a draft).

### 2.1 Schedule fields on any Sales Order (POS or Desk)
`orders/sales_order.py`, hooked on Sales Order:
- The fields **Delivery Area, Delivery Slot Date, Delivery Slot Time, AC
  Installation Date** are optional. If you fill them on an order made by hand,
  **submitting it books that slot / AC day**; cancelling releases it.
- `validate` refuses: a date without area/time, a time that is not a working hour
  of the area, a slot that is **full, closed, too soon or in the past**, an AC day
  without enough units left, an AC day on an order with no AC items, and
  non-digit Block/Road/Building. AC Units is computed from the items.
- `before_submit` books under the engine's lock, so a slot taken between save and
  submit is refused, and the DN server script never runs for it.
- The section **Delivery Schedule** is on the **Details** tab, above the items
  (it used to be on the Terms tab, read-only). Area, Slot Date, Block/Road/Building
  are editable; **Slot Time** and **AC Installation Date** are read-only (hidden until set). The
  **Choose Delivery Slot** button opens a picker that lists **only free slots** (cars
  left) for the chosen area/date, and the AC days with enough capacity for the
  order's AC units.

### Cancellation
`Sales Order.on_cancel` releases all bookings (ledger) and reactivates its reserved
serials, unless a non-cancelled DN still lists them. In that case the existing
"active serial after delete delivery note" script reactivates them when the DN is deleted.

---

## 3. POS Awesome V15 changes (`apps/posawesome`, branch `develop`)

Everything is switched on only when this app is installed (`frappe.boot.delivery_management.enabled`, set by
`api.pos.extend_bootinfo`) **and** the POS Profile has *Allow Sales Order*. Without the app, POS Awesome V15 behaves exactly as stock.
Rebuild after changes: `cd apps/posawesome/frontend && yarn build` (or `bench build --app posawesome`).

| File (under `frontend/src/posapp/`) | Change |
|---|---|
| `utils/deliveryManagement.ts` (**new**) | Detection, `defaultInvoiceType` (Order), `isDmOrder`, `serialsPickedByServer`, Arabic-digit `digitsOnly`, `dmCall`. |
| `stores/deliveryStore.ts` (**new**) | The chosen delivery (address + schedule + `valid` + `missing`) and the sellable stock per type (branch / main store). |
| `components/pos/delivery/DeliveryPanel.vue` (**new**) | The **Home Delivery** card under the cart (Order only): 1 address (Area, Block, Road, Building digits-only, Address name; prefilled from the customer's last order), 2 delivery day strip + time-slot cards (cars left, full/closed/too-soon shown and disabled), 3 AC installation day strip (units free). A status chip says what is still needed. |
| `composables/pos/delivery/useDeliveryStock.ts` (**new**) | Stock shown per type in the item list and cards; out-of-stock items greyed out. |
| `utils/posDocumentMode.ts` | An Order stays a **Sales Invoice** draft (stock V15 would draft a Sales Order). |
| `services/invoiceService.ts` | Pay submits through `delivery_management.api.pos.submit` (order type + delivery), never in the background, 120 s timeout. |
| `composables/pos/payments/usePaymentSubmission.ts` | Orders need a valid delivery and a connection; dm sales skip the offline **outbox** (replaying them through V15's own submit would skip the Sales Order, serials and slot); success message names the Sales Order; slots refresh after a failed submit. |
| `components/pos/Payments.vue` | Submit locked for an Order until the delivery is complete, with a notice saying what is missing; after a sale the type goes back to Order. Sales Person field hidden (salesman = cashier). |
| `components/pos/invoice_utils/actions.ts` | **Save & Clear** on an Order = place it **unpaid** (`unpaid=1`), see §2. |
| `components/pos/invoice_utils/validation.ts` | No "serial required" for sales (the server picks serials); stock check per type (branch / main store). |
| `composables/pos/items/useItemAddition.ts`, `addition/useItemCreation.ts`, `invoice/CartItemRow.vue`, `invoice/ItemsTableExpandedRow.vue` | No browser-side serial pick or serial chip on sales (kept on returns); no per-item delivery date. |
| `components/pos/items/ItemsSelector.vue`, `ItemCard.vue`, `ItemsSelectorTable.vue` | Loads the per-type stock; blocks out-of-stock items with a clear message. |
| `components/pos/payments/PaymentAdditionalInfo.vue`, `PaymentSelectionFields.vue` | Stock V15's order delivery date / address and Sales Person hidden. |
| `components/pos/Invoice.vue` | Mounts the panel; default type Order. |

No Python file of POS Awesome is changed in v16.

> **Server side for V15** (`orders/service.py`): serials sent by the browser are ignored (V15 pre-fills them and
> does not know about reserved serials); a retried request returns the first result (`already_submitted`);
> the "Delivery Note ... has been created" pop-up of the production server script is dropped during the POS
> submit (in V15 that desk dialog fought the POS for focus and froze the browser).

### 3.1 Desk pages (v16 redesign)
* **Sales Statistics**: filter card with quick periods, KPI cards (total sales, commission, invoices, orders, AC / other units), a "What is counted?" note, the salesman table with share bars and per-row commission %, and the invoice list with search and paging. Same API, same numbers, same commission formula and saved preferences.
* **Delivery Slots**: segmented tabs, colour-coded slot cards (free / partly booked / full / closed / too soon) with a legend and capacity bars, a sticky bookings panel with address and phone, confirmations before closing or releasing. AC tab: 14-day strip with meters, day panel with capacity editor. The Delivery tab shows **every area as a section** (one date for all), each with its summary, slots and bookings panel. **Move** (page and Delivery Note) is one dialog, `public/js/slot_move.bundle.js`, with the area as a dropdown of all areas.
* **Delivery Note → Move Delivery Slot / Move Installation Day** (managers, until Delivered): moves the booking its Sales Order holds for the note's type (`api.admin.get_delivery_note_booking` + `move_reservation`). The new slot is copied onto the order, its invoices and its Delivery Notes not yet delivered (draft or On the way).
* **Sales Order → Choose Delivery Slot**: one picker with a day strip, free-time cards and AC days; choose both, then **Apply**.

## 4. Admin page: `/desk/delivery-slots` (roles: System Manager, Delivery Manager)

**Tab "Delivery Times"** (old *Delivery Times* page):
area + date (prev/today/next); a card per slot with booked/cars, capacity bar and
status. **Close / Open slot** (with reason). Click a slot to list its bookings:
SO link, customer, phone, block/road/building. **Add booking** (manual: reference/
invoice no, phone, customer, optional "allow over capacity"). **Move** a booking to
another area/date/slot; the SO and its invoice fields update. **Release** manual bookings.

**Tab "AC Management"** (old *AC management* page):
a 14-day strip (booked/capacity per day), then for the chosen day: reserved /
capacity (override or global) / remaining, **Close / Open day**, **Set capacity
for this date** / **Use global**, **Reserve units** (manual), and the list of AC
orders with their AC items × qty, **Move** (to another day) and **Release** (manual only).

Bookings owned by an order cannot be released from the page. Cancel the order
instead, so stock, money and capacity stay consistent.

Every endpoint checks the role server-side (`frappe.only_for`). Writes are POST-only.

---

## 4.1 Sales Statistics page: `/desk/sales-statistics`

Roles: System Manager, Sales Manager, Accounts Manager see **every salesman**;
Sales User sees **only their own** numbers (as in the old admin site).

Rules (each one has a test in `tests/test_sales_stats.py`):
- **Salesman** = the cashier who made the sale: `posa_cashier` (POS Awesome V15 records the PIN-verified cashier; several cashiers can share one login), else the user who created the invoice.
- **Period = the day the invoice was created**, not its Posting Date. The POS
  profiles allow the cashier to change the Posting Date (`Allow change posting
  date` is on in production too); ACC-SINV-2026-00035 was created on 07-10 with
  Posting Date 12-10 and was missed by the first version. Whole days, both ends
  included.
- **Only paid invoices count**: submitted, not a return, nothing outstanding
  (status Paid, or Credit Note Issued). Drafts, cancelled, unpaid and partly paid
  invoices are left out; an unpaid order (Save/New) counts once it is fully paid.
- **Returns** (credit notes against a paid invoice) are subtracted from money and units.
- **Total** = what the customer pays (rounded total, with VAT); **Net Total** = before
  VAT; company currency.
- Units: AC = items that use AC installation capacity (AC Item Groups minus the
  exclusions); Non-AC = every other stock item. Services and charges are not units.
- The per-salesman numbers are summed from the same invoice list shown on the page,
  so the table and the list always agree (tested).

Page:
- Filters: **From / To date** (default: first day of this month → today; presets
  Today, Last 7 Days, This Month, Last Month), **Salesman**, **Company**.
- Per salesman: Invoices, Delivery Orders, Returns, Total, Net Total, AC Units,
  Non-AC Units, Commission %, **Commission**; totals row and summary cards.
- **Invoices counted**: every invoice number with created time, posting date,
  salesman, customer, type, status, total, units (100 per page). Click a salesman
  to see only theirs.
- **Commission %**: one general rate, plus an optional rate per salesman typed in
  the row; **Commission On** Total (incl. VAT) or Net Total. Remembered in the
  browser only.
- Menu → **Export CSV** (summary + invoice list).

Background: every **Show** queues a **fresh** calculation on the `long` queue (no
caching, no reuse of an earlier result). The page fills in when the job finishes
(realtime event, polling as a fallback). It reads with SQL only (no documents
loaded). `after_migrate` adds an index on `tabSales Invoice.creation` so long
periods stay fast. Requires the bench worker (running under `bench start` / supervisor).

---

## 5. Settings (`Delivery Settings`, single doc)

- **Global AC Daily Capacity**: default AC units per day when the date has no override.
- **Slot Lead Time Hours**: blocks same-day slots starting within this many hours (inclusive).
- **Booking Window (Days)**: how far ahead the POS offers dates.
- **AC Item Groups** (several allowed), **Excluded Item Groups**, **Excluded Items**: see §0.
  An excluded group must sit under one of the AC groups. On migrate, the old single
  value is carried over into the list.
- **Main Store (Delivery Orders)**: `Stores - SHD`.

Custom fields added (prefix `dm_`):
- **Address**: Delivery Area, Block No, Road No, Building No (the POS "Address"
  field is the standard Address Title).
- **Sales Order / Sales Invoice**: section "Delivery Schedule" with POS Order Type,
  Delivery Area, Slot Date, Slot Time, AC Installation Date, AC Units, Block/Road/Building.
- **Sales Order Item**: Allocated Serial Nos.

New role: **Delivery Manager**.

---

## 6. Findings in the client's production ERP (read-only, nothing was changed)

I only used HTTP **GET**. Production structure: company `showsatellite` (SHD, BHD,
Asia/Bahrain); main store `Stores - SHD`; POS profiles BaniJamra / JIDHAFS /
Muharraq / Nuwaidrat → their branch warehouses; AC items under `Air Conditioner`
(mostly `Magic Air Conditioner`); 3 active server scripts on SO/DN.

### 6.1 Latent bug in the production server script (handled in our app)
*"Delivery Note From Sales Order"* sets `customer = doc.customer_name` (the
**name**, not the ID). Production names customers by **Customer Name**, and POS
allows duplicate names (IDs like `Ali`, `Ali-1`). For such a customer the DN goes
to the wrong customer or fails, and with our flow the whole order would roll back.
Our `Delivery Note.before_insert` hook now copies customer, currency and price list
from the SO, **only for DNs of orders placed through this app**. I recommend also
fixing the script itself to `customer = doc.customer`.

---

## 7. Local development environment (`erp16.localhost`, shared with the `marwan` company of another project: the seed leaves its timezone and customer naming alone)

`bench --site erp16.localhost execute delivery_management.dev.setup_showsatellite.run`
(idempotent, refuses to run unless `allow_tests` is on, never wired to install).
Source: `dev/showsatellite_snapshot.json` (read-only snapshot of production). It created:

- Company **showsatellite** (SHD, BHD) with the exact production warehouse names:
  `Stores - SHD`, `JIDHAFS - SHD`, `Banijamra - SHD`, `Muharraq - SHD`,
  `Nuwaidrat - SHD`, HamzaCenter, Maasum, MaamerStore, Workshop tree.
- Branches, the full production **item-group tree** (125), the AC items + 10 real
  non-AC items with production prices (price list `Standard Selling BHD`).
- The 4 **POS profiles** (Allow Sales Order on), Cash account, sample customers
  *Ahmed Ali*, *Fatima Hasan*.
- Opening stock with serials: 12 per item in `Stores - SHD`, 2 per branch. AC units
  are only in JIDHAFS and BaniJamra branches, so you can see Invoice vs Order stock differ.
- The 3 production **server scripts** (server scripts enabled in site config).
- 8 **sample** delivery areas × 6 slots (10:00–20:00; Friday afternoons only). The
  real areas/hours live in the old system's DB; enter them in the page/doctypes.
- Delivery Settings as in §5. Site timezone set to **Asia/Bahrain**, customer naming
  by Customer Name, like production. A dev-only EGP→BHD exchange rate was added,
  because your dev site's default price list is EGP.

Your existing dev company `marwan` was not modified.

---

### 7.1 Using the POS on the dev site
- Open the POS on one of **BaniJamra / JIDHAFS / Muharraq / Nuwaidrat** (company
  showsatellite). If an old shift is still open on another profile (e.g. `m`,
  company marwan, EGP), POS Awesome resumes it automatically. **Close that shift
  first**, otherwise items show rate 0 (their prices are in `Standard Selling BHD`)
  and Orders are refused ("belongs to company …").
- The dev profiles copy the production POS flags. Rate and item-discount editing
  are **off in production too**, so those fields are greyed out by design.
- With "Use Browser Local Storage" on (as in production), the item list first shows
  the browser's cached copy; it refreshes from the server a moment later.

## 8. Tests

```
bench --site erp16.localhost run-tests --module delivery_management.tests.test_slot_engine --skip-before-tests
bench --site erp16.localhost run-tests --module delivery_management.tests.test_orders --skip-before-tests
bench --site erp16.localhost run-tests --module delivery_management.tests.test_sales_stats --skip-before-tests
```
(`--app delivery_management` does not work here: the runner first builds ERPNext test
records for linked doctypes and crashes on a Cost Center record.)
**126 tests on v16 (erp16.localhost), all passing** (47 slot engine, 60 orders with 1 skip when order_cancellation is absent, 19 sales statistics). Run with `--skip-before-tests`.
`tests/test_sales_stats.py` (18): exact money per salesman (hand-computed), invoice
numbers listed, rows = sum of their invoices, creation date vs posting date, day
boundaries (00:00 / 23:59:59.999999), long periods, unpaid / partly paid excluded
until paid, a real Payment Entry makes it count, drafts and cancelled excluded,
returns subtracted, returns against unpaid invoices ignored, rounded total, company
currency, user/company/date filters, every run is fresh, a Sales User sees only
their own numbers and cannot read another user's run, bad dates, expired run.

> ⚠ Always pass `--skip-before-tests`. Without it ERPNext's `before_tests` hook runs
> `delete from tabItem Price` and **commits**, wiping every price on the site, and resets
> Selling Settings' default customer group / territory. If that happens on the dev
> site, re-run `bench --site erp16.localhost execute delivery_management.dev.setup_showsatellite.run`
> to restore the showsatellite prices. Each test rolls back everything it creates (checked:
no `_DM` rows left).

- `tests/test_slot_engine.py` (47): time normalisation; lead time (your exact
  example, midnight, zero lead); book/full/release/idempotent/never negative;
  double-booking guard; reschedule (including into your own full slot); move one part of a
  mixed order; past dates; weekday without hours; unknown hour/area; disabled area;
  0 cars; cars change resync (up and down, bookings above new capacity);
  manual close (rejects booking, survives release + rebuild, reopen keeps "full");
  force/over-capacity; AC units vs orders, whole-order rejection, override vs global,
  capacity 0, manual close, global change resync, past day, calendar; mixed order
  all-or-nothing; rebuild from ledger; simulated concurrent first insert; ledger delete guard.
- `tests/test_orders.py` (56): (new) Save/New unpaid order; Sales Order schedule
  (no schedule books nothing, submit books / cancel releases, taken slot refused,
  not-a-working-hour, half-filled refused, AC units from items, AC day without AC
  items, full AC day, slot taken between save and submit, digits only, picker lists
  only free slots); address title in the match, default title, digits, old-site text
  address parsed and reused, Arabic digits match ASCII, prefill prefers the last order's address; several AC
  groups. Earlier: cart classification (UOM, accessories, excluded
  item, services, settings validation); sellable qty per store and type; serial picking
  (oldest first, skip reserved, shortage, fractional qty); address reuse vs new;
  Invoice flow (branch serials, branch stock limit, optional address); Order flow
  end-to-end (SO/DN/SI carry identical serials, Inactive status, capacity, address,
  100% billed with discount); AC-only and non-AC-only orders; missing schedule/address;
  main store vs branch stock; different serials for consecutive orders; full slot
  leaves nothing behind; cancel frees capacity and serials; draft DN keeps serials
  reserved; **POS return → `order_cancellation` cancels the SO → capacity freed** (the
  real production cancel path); background-submission profile still submits
  immediately; returns refused as Orders; Orders from another company's POS profile refused; salesman = logged-in user's Sales Person; work-hour removal guard.
- `tests/race_check.py`: a manual concurrency proof with two real DB connections
  (`slot` and `ac` modes). It cleans up after itself.

---

### UI check
**Round 3 (Save/New, address, Desk picker), headless Chrome:** the POS opens on Type
**Order**; selecting *Ahmed Ali* pre-filled Address/Block/Road/Building; typing
`3a0x5` in Block left `305`; **Save/New** created SAL-ORD-2026-00016 (submitted,
slot + AC day booked), ACC-SINV-2026-00037 (**submitted, Unpaid**, outstanding =
total) and the draft DN (server script) with serials; the new Address "Home" was saved.
On a new Sales Order in the Desk, *Choose Delivery Slot* listed only free slots and
filled area/date/time. The test order was then cancelled (bookings released).

The full Order flow was clicked through in a headless Chrome on the dev site: customer → Type
Order → AC + TV → address → day/slot + AC day → PAY → **Submit** → Sales Order + paid
Invoice + Delivery Note (server script), screen reset. It found and fixed a bug where PAY
wiped the chosen slot (Submit stayed disabled). Earlier note, kept for reference: before
that, the UI had not been clicked through. What
*was* verified: the Vue build compiles, the page JS parses, every POS/admin endpoint
answers correctly over real HTTP (and `submit` refuses GET), the desk page loads,
and the whole backend flow ran on the replicated environment with the production
server script. Please check by hand on `/app/posawesome` and `/app/delivery-slots`:

1. Switching **Invoice ↔ Order** changes the stock shown (branch vs `Stores - SHD`).
   An AC unit with no branch stock is "Out of stock" in Invoice and sellable in Order
   (try profile Muharraq or Nuwaidrat).
2. Cart with TV only: only the slot picker shows. AC only: only the AC-day chips.
   Both: both. AC + Copper Pipe: AC day only.
3. Selecting a customer pre-fills area/address/block/road/building. Changing one and
   submitting adds a new Address on the customer; submitting the same values does not.
4. Today's slots inside the lead time are disabled ("too soon"). A full slot is disabled ("full").
5. A failed submit (e.g. close the chosen slot from the admin page first) shows the
   error and **keeps the screen**.
6. Admin page: Close/Open slot, Add booking, **Move** (the slot list must fill when
   area/date change in the dialog), AC capacity override / Use global, Reserve units.

## 9. Deploying to production (when you're ready)

1. `bench get-app` / copy `delivery_management`, then `bench --site <site> install-app delivery_management`
   (it only adds the role, custom fields and default settings).
2. Deploy the modified `posawesome` and run `bench build --app posawesome`.
3. In **Delivery Settings**: confirm the main store, the AC group and the exclusions (§0).
4. Create **Delivery Areas** (cars) and **Work Hours**, and give staff the
   *Delivery Manager* role.
5. POS profiles: **Allow Create Sales Order** is already on in production. Background
   submission can stay as it is (see §3).
6. ~~Optionally fix the server script~~: replaced by the app (§12.2); the migrate patch disables the scripts.
7. Phase 3/4 steps: see §12.6.

**Not done in this phase (as agreed):** migrating the old system's existing
bookings, driver assignment, notifications, etc.

---

## 10. File map

```
delivery_management/
  slots/        engine.py (booking engine) · cart.py (AC classification) ·
                timeutils.py · settings.py · __init__.py (public API)
  orders/       service.py (Invoice/Order flow) · stock.py (availability, serials) ·
                address.py · events.py (SO cancel, DN serial copy) · schedule.py ·
                sales_order.py (SO validate/before_submit booking, Desk slot picker API)
  public/js/    sales_order.js (Desk "Choose Delivery Slot" picker)
  api/          pos.py (POS endpoints) · admin.py (admin page endpoints, role-checked) ·
                sales_stats.py (Sales Statistics endpoints + background job)
  stats/        sales.py (per-salesman aggregation of paid Sales Invoices)
  delivery_management/doctype/   8 doctypes + 3 child tables
  delivery_management/page/delivery_slots/   admin page (js/css/json)
  delivery_management/page/sales_statistics/ Sales Statistics page (js/css/json)
  install.py    role, custom fields, default settings (after_install/after_migrate)
  dev/          setup_showsatellite.py + showsatellite_snapshot.json
  drivers/      assignment.py (availability, assign) · delivery_note.py (DN hooks, permissions) · stats.py
  api/drivers.py  Driver Assignment / Delivery Schedule / Installation Statistics endpoints
  delivery_management/page/{driver_assignment,delivery_schedule,installation_statistics}/
  delivery_management/print_format/pos_invoice_salesman/
  public/js/    drivers_common.bundle.js · delivery_note.js · header_search.bundle.js
  fixtures/role.json   AC Tech, TV Driver, Technician, Maintenance Technician
  orders/       delivery_note.py (DN from SO) · serials.py (serial reservations and guard)
  customer_support/  tickets.py (support rules) · permissions.py · doctype/support_technician_visit ·
                page/{support_tickets,technician_issues,maintenance_issues}
  api/support.py   Support pages endpoints
  public/js/    support_pages.bundle.js · issue.js
  patches/      ac_tech_day_limits_into_settings.py · disable_replaced_server_scripts.py
  tests/        test_slot_engine.py · test_orders.py · test_sales_stats.py · test_drivers.py · test_serials.py ·
                test_support.py · fixtures.py · utils.py · race_check.py
```

---

## 11. Phase 3 (2026-10-10): drivers, delivery charge, bundles

### 11.0 Request checklist (all done and tested)

- [x] 1. Item check box **Has Delivery Charge** + Delivery Settings **Delivery Charge Item**. In a POS Order the charge item is added once; never a second row or qty 2. The server refuses duplicates.
- [x] 2. Product Bundle created and tested from the POS (Order and branch Invoice, TV bundle and AC bundle).
- [x] 3. Roles **AC Tech** and **TV Driver** as fixtures (`fixtures/role.json`).
- [x] 4. Delivery Note: **Delivery Schedule** section (copied from the Sales Order, still kept on SO/SI), **Order Type** (AC / Non AC, from the item groups like the POS), **Driver / AC Tech** (User, set only from the page). Shown in the list view and the standard filters.
- [x] 5. Doctype **Driver Schedule** (one row per assigned DN, the old `driver_schedule`), doctype **AC Tech Day Limit** (the old per-day override), Delivery Settings **AC Tech Max Orders per Day**.
- [x] 6. Page **Driver Assignment** (`/desk/driver-assignment`), tabs TV / AC.
- [x] 7. Page **Delivery Schedule** (`/desk/delivery-schedule`), tabs TV / AC, for drivers.
- [x] 8. POS guard: AC and non-AC items cannot be in one POS Order. A manual mixed order gets no Order Type, and the DN asks for one.
- [x] 9. Page **Installation Statistics** (`/desk/installation-statistics`).
- [x] 10. Print format **POS Invoice Salesman** (copy of production `POS_INVO_C1` + "Sales Man").
- [x] 11. Only `delivery_management` and the POS Awesome front end changed; nothing in frappe / erpnext.

### 11.1 Order Type of a Delivery Note

Set on insert from the items, with the same classification as the POS (`slots.cart`): Delivery Settings AC item groups, bundles counted by their components.

| Items | Order Type |
|---|---|
| AC units (+ AC accessories, services, delivery charge) | **AC** |
| TVs / other stock goods (+ accessories alone, services, delivery charge) | **Non AC** |
| AC units **and** TVs / other goods (only possible in a manual order) | *empty*: the form shows a warning and the field becomes required; the DN cannot be saved again or submitted until someone chooses |

The DN is still created as before by the production server script on SO submit; an empty type never blocks that insert. Old DNs were filled by the patch `patches/backfill_delivery_note_schedule.py`. Changing the type of an assigned DN removes a driver of the wrong role.

### 11.2 Who can be assigned (port of `driver_schedule.py`)

| | TV / Non AC orders | AC orders |
|---|---|---|
| Role | **TV Driver** | **AC Tech** |
| Busy when | another of his orders the same day is within **0.5 h** (person assigning is a System Manager) or **1 h** (anyone else) of this time; a missing time conflicts | he already has his daily maximum of AC orders that day |
| Daily maximum | none | Delivery Settings *AC Tech Max Orders per Day* (0 = no limit), or the per-day *AC Tech Day Limit* (0 = **day off**); a per-day limit always wins |

Unavailable people are listed but greyed with the reason ("unavailable", "3/3 full", "day off"); the server checks again on assign (`drivers/assignment.check_assignable`) under a row lock on the driver, so two people can't both take his last free place. Counts are always read from **Driver Schedule** rows, which are kept in step with the DN (assign, unassign, reschedule from the Delivery Slots page, cancel, delete).

### 11.3 Pages

**System Manager = admin in the old system.** Delivery Manager has the same rights on these pages.

* **Driver Assignment** (System Manager, Delivery Manager): draft DNs waiting for delivery, earliest first. Filters: search (DN, customer, phone), assigned / not assigned, driver, area, date; page size and paging. Columns: DN, customer + phone, address, delivery day/time (AC: installation day + units), type, payment, driver. "Assign driver" opens the list of drivers of the right role with availability. Clicking a row opens the Delivery Note. AC tab + a date: each AC Tech's load that day, click to set his limit for that day (0 = day off, empty = global). A banner lists DNs that still need an Order Type.
* **Delivery Schedule** (+ TV Driver, AC Tech): assigned DNs in a date range (default today to +1 month, "Today" button). A driver sees only his own; managers see all and can filter by driver. Status filter On the way / Delivered (workflow state, see §13). Order details: customer, phone (tap to call), address + Google Maps, items (bundle components and serials), schedule, payment (total / paid / outstanding) with **Record payment** (Payment Entry against the order's invoice, never above the outstanding, credited to the driver), **Open Delivery Note**, **Download DN PDF**, **Upload photo / video** (attached to the customer, as before), **customer signature** pad (attached to the DN). **No Ship, Return or Submit** (the office submits the DN). Phone layout: rows become cards.
* **Installation Statistics** (+ TV Driver, AC Tech): from **submitted** DNs with a driver. Presets Today / Last 7 days / This month, start/end date, technician (managers only), unit type (all / AC / TV & non-AC). Cards: AC units, TV & non-AC units, total. Table per technician with bars, orders, totals; click a row to list his DNs. A driver sees only himself. AC units = qty of AC items; non-AC units = qty of other stock items (accessories only when the order has no AC unit; services and the delivery charge are not units). No caching.

Drivers get read + print on Delivery Note, limited to their own DNs by `drivers/delivery_note.has_permission` / `get_permission_query_conditions`. Users who also have a stock/sales/accounts role keep their normal access.

### 11.4 POS (Order mode only)

* **AC / non-AC guard** (`useDeliveryRules.ts`, server `api.pos.check_pos_order_rules`): adding an AC unit to a cart with a TV (or the other way) is refused with "AC items and TV / non-AC items must be in separate orders…". AC accessories, services and the delivery charge fit both. Branch Invoices are not limited (no delivery); manual orders in ERPNext are not limited.
* **Delivery charge**: adding an item with *Has Delivery Charge* adds the Delivery Charge Item (qty 1) if it is not in the cart yet. Adding it again by hand is refused; two rows or qty 2 are refused before payment and by the server. Switching the sale to Invoice removes a charge the POS added by itself. The item must be non-stock, in the POS Profile's item groups and have a price.
* POS Awesome's own "Delivery Charges" box (top right) is a different, older feature; it was left as it is.

### 11.5 Product Bundles

Tested in the browser: *TV 43 + HDMI CABLE BUNDLE* (Order and branch Invoice) and *AC 1.5 TON + COPPER PIPE BUNDLE* (Order). What the app does for bundles:

* Stock: checked per component (POS list and server); a bundle is sellable as many times as its scarcest component allows.
* Booking: the components decide (TV bundle books a delivery slot, AC bundle books AC units).
* Serials: Order: picked from the main store for the serialised components on the Sales Order packing list, reserved (Inactive) and copied to the Delivery Note; cancelling the order frees them. Branch Invoice: picked from the branch.
* Order Type / Installation Statistics count the components.
* Known POS Awesome issue (upstream, not changed): the browser console shows `expand_bundle … reading 'push'` when a bundle is added; the cart shows the bundle line only. The sale is not affected (ERPNext builds the packing list on the server).

### 11.6 Sales Invoice print

Print format **POS Invoice Salesman** (module Delivery Management): the production POS format `POS_INVO_C1` (read with GET from production) with a **Sales Man** line under Posting Time: the cashier who made the sale (`posa_cashier`, PIN login), else the user who created the invoice (same rule as Sales Statistics). The dev seed sets it on the four Showsatellite POS Profiles. **On production after the upgrade:** POS Profile → Print Format → *POS Invoice Salesman*.

### 11.7 Tests

`tests/test_drivers.py` (15): order type incl. bundles and mixed, schedule copy, TV window, AC limit / override / day off, role check, unassign/cancel, type change, driver access, installation stats, POS order rules, item rules. `tests/test_orders.py` adds bundle Order / Invoice / stock / cancel and the driver payment. Dev users (seed): `tv.driver1@`, `tv.driver2@`, `ac.tech1@`, `ac.tech2@showsatellite.test`, password in `dev/setup_showsatellite.py` (`DEV_DRIVER_PASSWORD`).


---

## 12. Phase 4 (2026-10-11): settings, Delivery Note, serials, support

### 12.0 Request checklist (all done and tested)

- [x] 1. Per-day AC Tech limits moved into **Delivery Settings** (table *AC Tech Day Limits*). All driver settings are there now, including the TV driver gaps (were hard-coded 1 h / 0.5 h) and the support visit gap.
- [x] 2. Server script **"Delivery Note From Sales Order"** replaced by the app; deleted on dev. **"invalidate serial after insert delivery note"** and **"active serial after delete delivery note"** replaced and disabled.
- [x] 3. Serial number report and double-use tests (§12.3).
- [x] 4. Support management (§12.4), from the old system's logic on ERPNext Issue / Maintenance Visit / Warranty Claim.
- [x] 5. Coverage check of the old system (§12.5).

### 12.1 Delivery Settings (new fields)

| Field | Default | Used for |
|---|---|---|
| TV Driver Gap (Hours) | 1 | Driver Assignment, assigner without System Manager |
| TV Driver Gap for System Manager (Hours) | 0.5 | Driver Assignment, System Manager (old admin window) |
| AC Tech Day Limits (table) | – | Per technician and day: max orders (0 = day off). Also edited from the AC tab of Driver Assignment, which writes only that row (under a lock) |
| Create Delivery Note when a Sales Order is submitted | on | §12.2 |
| Only for Company | empty = all | dev: `showsatellite` (the site also hosts the `marwan` company) |
| Technician Visit Gap (Minutes) | 60 | Support: a technician is busy ± this many minutes around a visit |
| Default Service Person | – | Maintenance Visit `service_person` when the technician has no Sales Person (prod used "Sales Team") |

The old standalone *AC Tech Day Limit* doctype became that child table; patch `ac_tech_day_limits_into_settings` moves existing rows.

### 12.2 Delivery Note from Sales Order (`orders/delivery_note.py`)

Sales Order `on_submit` → ERPNext's own `make_delivery_note` mapper → draft Delivery Note (ignore permissions, like the script). Compared with the script: customer **ID** (the script used the name), price list, currency, taxes, addresses and the `against_sales_order`/`so_detail` links; made once (skipped when the order already has a DN, draft or submitted, or nothing is left to deliver, or *Skip Delivery Note* is ticked). The serials reserved by the POS order, the schedule and the Order Type are copied as before. The desk shows a small green alert; the POS shows nothing.

Patch `disable_replaced_server_scripts` disables the three scripts by name on every site that has them (production too, on its first migrate) and switches the setting on. Disabled, not deleted, so they can be looked at. Running both would make two Delivery Notes per order.

### 12.3 Serial numbers: how they work now

**Serial No status in ERPNext v16** (field `status`; there is **no Workflow** on Serial No, on dev or on production):

| Status | Meaning | Set by |
|---|---|---|
| Active | In a warehouse | ERPNext, on every inward movement (purchase, receipt, transfer, return, DN cancel) |
| Inactive | Business meaning: **reserved for an open order**, still in the warehouse | This app (ERPNext also uses it for a few internal cases, e.g. a cancelled purchase receipt) |
| Delivered | Left the store to a customer (DN, or invoice with update stock); warehouse emptied, customer and warranty expiry set | ERPNext |
| Consumed | Used in manufacture / repack / material issue | ERPNext |
| Expired | Warranty/expiry passed | ERPNext |

Life of a serial in a POS **Order**: Active (in Stores) → picked by the POS server and **Inactive** (Sales Order `dm_serial_nos`) → same serial on the auto Delivery Note → DN submitted → **Delivered**. Cancelling the order (or closing it) makes it Active again unless another document holds it. A branch **Invoice** takes an Active serial from the branch and it becomes Delivered at once.

**Can two orders get the same serial?** No. What prevents it:

1. **The POS never takes a serial from the browser.** Serials POS Awesome pre-fills are dropped; the server picks them (`orders/stock.pick_serials`).
2. **Picking skips reserved serials twice over**: status must be Active *and* no live document may hold it (open Sales Order or draft Delivery Note). The second check matters because ERPNext sets a moved serial back to Active.
3. **Row locks**: the pick is `SELECT … FOR UPDATE`. Two cashiers selling the last TV at the same moment: the second waits, then sees it taken and gets "Not enough stock". Proved with two real DB connections (`serial_race.py` run on dev: one GOT, one REFUSED).
4. **One guard on every outward movement** (`Serial and Batch Bundle` validate/before_submit → `orders/serials.guard_outward_bundle`). In v16 every outward serial goes through a bundle, so it covers Delivery Notes, Sales Invoices with update stock, Stock Entries (issue, transfer) and the POS, whatever screen or API made them. A serial held by another open order is refused with "… is reserved for SO-…, DN-…".
5. **Desk documents**: a Delivery Note, stock Sales Invoice or outward Stock Entry row with an empty serial gets a free serial from the app *before* ERPNext's FIFO auto-pick. That FIFO ignores Inactive and takes the oldest serial, usually the reserved one (Stock Settings *Auto Create Serial and Batch Bundle For Outward* is on); test `test_material_issue_without_serial_takes_a_free_one` fails without this step.
   Note for the warehouse: a Sales Order typed in the desk now gets its draft Delivery Note **with serials already filled** (oldest free) and reserved; the old script left them empty. Change a serial on the draft if needed; the old one is freed. A typed serial that another order holds is refused on save.
6. **The flag follows the documents** (replaces the two server scripts): a draft Delivery Note reserves its serials (Inactive), changing a serial frees the old one, deleting the draft frees them, a cancelled DN of an open order stays reserved, closing/cancelling the Sales Order frees them, and any other stock entry that moves a serial puts the flag back after ERPNext resets it.
7. **A reserved serial cannot be moved by a Stock Entry** (transfer or issue) while its order is open: the guard refuses it. Free it first (cancel/close the order or change the serial on its Delivery Note).

What it does not cover: a serial typed on a document of an app that writes to the database directly (bypassing documents), and production data before the upgrade (v14 rules). Tests: `tests/test_serials.py` (17) and `test_orders.py`.

### 12.4 Support management (`customer_support/`)

Port of the old system's support part (old pages `/support`, `/support/technician`, `/support/maint-tech`; services `issue_tickets.py`, `technicians.py`, `maint_tech.py`). The Frappe 15 `support_management` app was read for ideas (it used Issue + workflow + separate tech doctypes); the old system's logic was followed.

* **Ticket = ERPNext Issue** (as the old system: it created an Issue for every ticket, 488 on production). Support fields `sm_*` on Issue: stage, customer phone, sales invoice, delivery note, item, serial, address, visit time, CS user, technician, maintenance technician, CS report, technician visits (child table *Support Technician Visit*), maintenance report, pickup slot, resolved by. Issues without a stage (e.g. the WhatsApp ones) are not part of the flow.
* **Stages**: Customer Service → Technician → Maintenance Technician; resolve at any stage once that stage's report exists. Resolve = Issue *Resolved* + its Warranty Claims *Closed*.
* **Customer service** (page **Support Tickets**, role *Support Team*, sees the tickets they opened; System Manager sees all): New Ticket with customer found by phone (customer ID, mobile or contact phone), past orders shown with items and **delivered serials**; one click fills item, serial, invoice and address. CS report required; visit time cannot be in the past. Update schedule/address, update the CS report (only in this stage), choose a technician: every user with role *Technician*, with his visits that day; busy within ± gap minutes = greyed. Checked again on the server under a lock (the old system checked only in the browser).
* **Technician** (page **Technician Issues**, role *Technician*, only his tickets, date filter): log a visit → draft **Maintenance Visit** (Breakdown, completion status, item, serial, service person) and, when the serial's warranty has not expired, a **Warranty Claim** (Work In Progress; reused on later visits). Edit his visit reports. Escalate to a *Maintenance Technician*, optionally booking a **pickup delivery slot** (same capacity as delivery orders; booked in the same transaction, the old system leaked the booking when the move failed).
* **Maintenance technician** (page **Maintenance Issues**, role *Maintenance Technician*): write/edit the maintenance report, resolve.
* Technicians see only their own Issues in the desk too (`customer_support/permissions.py`).
* Differences from the old system (decisions): technician lists come from roles (the old page listed every employee); the delivery note/serial is found from the invoice in ERPNext v16 (the old lookup by `delivery_document_no` does not exist in v16); old "MAINT-TICKET-n" numbers are not needed (every ticket is an Issue).
* Tests: `tests/test_support.py` (14). Browser: full flow as `cs.agent1@`, `tech1@`, `maint1@showsatellite.test` (dev password as the drivers).

### 12.5 Old system parts still not in ERPNext (logs and dashboard ignored)

1. **Assign a delivery slot to an already submitted order** (old Orders page "Assign delivery"). Today the slot is chosen before the Sales Order is submitted; a manual booking on Delivery Slots is not linked to the order or its Delivery Note.
2. **WhatsApp bot reschedule API** (old `/admin/whatsapp-orders/reschedule-delivery`, by Sales Order).
3. **Website shop** (catalog, options/add-ons, OpenCart webhook, customer OTP accounts, per-order quantity limits `max_quantity` / AC per order, Google Sheet log): only if the website stays live.
4. Ship / Return flags and DN submit by drivers: left out on purpose (phase 3).
5. Departments and per-user page permissions: replaced by roles / Role Profiles.

### 12.6 Production steps (after the v16 upgrade)

1. `bench migrate` (adds fields/roles, moves day limits, **disables the 3 server scripts**), then `bench clear-cache`.
2. Delivery Settings: check *Create Delivery Note…* (on), set *Only for Company* if the site has other companies, *Default Service Person* (e.g. "Sales Team"), gaps.
3. Roles: *Support Team* (customer service), *Technician*, *Maintenance Technician*.
4. Items: set **Warranty Period (days)** on items, so delivered serials get a warranty expiry and Warranty Claims are opened.
5. Tickets still open in the old system's own database do not move by themselves: finish them there or re-open them on the Support Tickets page.

## 13. Mixed AC / non-AC orders, driver submit (2026-10-12)

* The POS no longer blocks AC units and TV / other goods in one Order (the item guard,
  the submit check and `api.pos.check_pos_order_rules` mixing rule were removed; the
  delivery-charge-once rule stays). The cashier books both the delivery slot and the
  AC installation day in the Delivery panel.
* `orders.delivery_note.split_by_order_type`: a mixed Sales Order gets **two** draft
  Delivery Notes — `AC` (AC units, mixed bundles, AC accessories; installation day and
  AC units only) and `Non AC` (other goods, delivery charge, services; delivery date and
  time only). Admin slot moves (`orders.schedule.stamp_schedule`) update each note with
  its own fields only.
* Delivery Note workflow `delivery note on the way` (fixtures: Workflow, Workflow State,
  Workflow Action Master): Draft → **On the way** (submitted by the stock keeper in the
  desk, after scanning the serials) → **Delivered**; Cancel from On the way / Delivered.
* Delivery Schedule page lists only On the way / Delivered notes (status filter). The
  driver's **Mark as delivered** (`api.drivers.mark_delivered`) needs a photo or video and
  the customer's signature attached first. That check is on the page only: the
  Delivered action in the Delivery Note form has no such guard. Installation Statistics
  do not count notes still On the way.

## 14. AC Site Check (2026-10-13)

DocType **AC Site Check** (`ACSC-.YYYY.-#####`): an AC Tech visits the customer's house
before a sale to recommend the AC type / capacity. Fields: customer name, phone, AC Tech
(only users with the AC Tech role), check date, status, area / block / road / building,
notes, and the **Rooms** table (AC Site Check Room: room, size, recommendations).
On save the customer is found by phone (`customer_support.tickets.find_customer`); if none,
a new Customer is created from the name and phone. The address is saved on the customer
like the POS does. Tests: `tests/test_ac_site_check.py`.
