# UI revamp Phase 3 evidence

Branch `production-readiness/ui-phase3` in `.worktrees/ui-phase3`, created from `origin/production-readiness/go-live-blockers` @ `d10b314` (Phase 4 merged), with `production-readiness/ui-phase2` @ `d17e695` merged in (`4580641`) so Phase 3 builds on the Phase 2 components (FilterPopover, FilterChips collapse, shared chrome). No Phase 2, portal or auth page was edited. Not pushed, not deployed.

## Iteration 1

Commits: `9eca52a` (3.2), `f80487f` (3.7, 3.9), `a1cedec` (fixtures, guard, System health e2e), `40635c8` (station dialog e2e, baselines, screenshots).

Ticked in `spec/tasks.md`: **3.2, 3.7, 3.9**. Open: 3.1, 3.3, 3.4, 3.5, 3.6, 3.8, 3.10, 3.11.

### What changed

| Task | Result |
|---|---|
| 3.2 Fuel | `FuelStationForm` is an md `FormDialog` (title "Edit fuel station" / "Add fuel station"). Capacity and initial stock are whole-gallon `NumberField`s; threshold is a `%` `NumberField`; Fuel type is `ProductSelect` (cap + name, code secondary). The `CODE (Name)` list is gone. A 20,000 L station shows **5,283 gal** (was `5283,441047162968`); capacity is only sent on save when the user changed it, so a litre-based station isn't silently rewritten with the rounded figure. Stations list is a `DataTable` (ProductChip, stock bar coloured by status with % and gallons as text, `StatusBadge`, row menu "Edit station", `aria-sort` on `th`). `FuelSummaryBar` deleted: totals are title-row counts (stock %, alerts, avg days left), station statuses are FilterChips with counts. One toolbar: location search, chips, Filters popover (product via ProductSelect), last-updated, refresh. Station detail opens in a `Drawer`; `?station=<id>` opens it (Phase 2 follow-up). `FuelEventForm` uses `NumberField`. Consumption chart uses RP 1637 product colours and names. Sourcing: the 9-field inline query form became a `FormDialog` ("Rank terminals", ProductSelect, whole-gallon volume, 4-decimal coordinates); the toolbar shows the current query (product chip, gallons, origin) and result counts instead of a 4-card band. K-factor: summary cards → FilterChips with counts; status → `StatusBadge`; approval confirm → `FormDialog`. `CustomerTankPage` tank create/edit → sectioned md `FormDialog` (whole-gallon NumberFields rounded on load and only sent when changed, ProductSelect over the tenant catalog with free-text fallback, StatusBadge, ProductChip in the list). |
| 3.7 Analytics | Ops Monitoring tab removed for every role. Range selector stays hidden (D13). Pie chart uses `CHART` tokens. Overview's in-page hero header and the "Export Report" button (it had no handler, so no capability is lost) are removed; metric tiles are `aria-pressed` buttons. |
| 3.9 System health | New `admin/SystemHealthPanel` at Settings → Platform → System health (`?tab=system`, alias `system-health`, module `system-health` = platform_admin). Poison-queue depth, oldest age, pending, permanently failed, status badge, 30 s poll, `LoadErrorState` with Try again, stale-data note. Deleted `OpsMonitoringDashboard.tsx` (+test) and the unused `getPrometheusMetrics`, `enable/disable/rollbackOpsFeatureFlag` clients (rg: no caller). Backend untouched. |

Shared component change (additive): `ui/index.ts` now also exports `PageTitle` and `useInPageChrome` (already existed in `PageHeader.tsx`).

Decision notes:
- Sourcing query in a FormDialog: the design §5 table lists "Sourcing order … md; NumberField". The query has 9 fields and ran inline at the top of the page, pushing results below the fold; the dialog keeps every field.
- `chrome.spec` gained `listPage: false` for Analytics (not a list page in design §6): it asserts the content panel starts at ≤ 92 px (directly under the title row) instead of a first data row.
- Phase 3 e2e fixtures live in a new `e2e/ui-revamp/phase3Fake.ts` (22 litre-based stations, summary, poison queue) so Phase 2's `fixtures.ts` is untouched apart from one hook line in `shellFake.ts`.

### Verification (all from `.worktrees/ui-phase3/runsheet`, node_modules symlinked to `.worktrees/merge-board`)

| Check | Result |
|---|---|
| `npx tsc --noEmit` | clean |
| `npm run lint` (biome) | 0 errors, 1 pre-existing warning (`noConfusingVoidType`, `DispatchBoard.tsx`) |
| `npx jest --ci -w 4` | 139 suites, 1805 passed, 1 skipped |
| `format.guard.test.ts` | baseline lowered 200 → **142** (actual count 142) |
| Fresh checkout (`git worktree add --detach` of `40635c8`) | `tsc` clean, `next build` exit 0, focused jest 12 suites / 141 passed; temp worktree removed |
| ui-revamp e2e, full run (`npx playwright test -c playwright.ui-revamp.config.ts --project=chromium`) | **132 passed**, 0 failed (run before the two specs below were added) |
| ui-revamp e2e, new specs (`fuelStation`, Phase 3 `visual` entries, `--update-snapshots`) | 8 passed |
| Dispatch board e2e (`PW_BOARD_PROD=1`) | 17 passed, 2 skipped (iPad); N1 drag p95 17.6 ms (budget 20), hover 17.6, scroll 17.5 |
| Backend gate | not needed (no backend files) |

FormDialog behaviour tests per migrated flow:
- `ops/FuelStationForm.test.tsx` (new, 9): 20,000 L → "5,283", not `type=number`; de-DE display "5.283" and parse "6.000" → 6000; ProductSelect trigger "Regular unleaded", option shows `GASOLINE_REG` as secondary text; threshold-only edit uses the threshold endpoint; unchanged capacity not resent; create with grouped input "12,000"; inline errors keep the dialog open; stock > capacity rejected.
- `ops/CustomerTankPage.test.tsx` (32): create/edit via FormDialog, inline errors, patch-only edit, catalog shown by name in ProductSelect, **5283.44 / 1200.6 gal tank shows "5,283" / "1,201" and an unrelated edit sends only `zip_code`**.
- `ops/SourcingPage.test.tsx` (25): the query runs through the dialog (ProductSelect, NumberFields).
- `compliance/KFactorCalibrationPage.test.tsx` (4): counts in chips; approve only for review-needed.
- `admin/SystemHealthPanel.test.tsx` (new, 4): figures + badge, network failure shows Try again and recovers, 403 staff copy, threshold grading.
- `AnalyticsHub.test.tsx` (rewritten, 7): no Ops Monitoring for 5 role sets incl. platform_admin, exactly 3 tabs, one h1, no range selector.

Chrome height (first data row, px, budget 172):

| Page | 1280×800 | 1440×900 |
|---|---|---|
| Fuel → Stations | 172 | 172 |
| Analytics (not a list page; content top ≤ 92) | pass | pass |

Fuel Stations' `test.fail` is removed in `pages.ts` (chrome and axe); Analytics' chrome mark removed. Others remain owned by their tasks.

Axe (wcag2a/2aa/21aa/22aa): 0 critical/serious on Fuel Stations whole page, Analytics whole page, and the open Edit fuel station dialog at 1280 and 1440 (`fuelStation.spec.ts`).

Contrast and colour vision: `styles/tokens.test.ts` passes (tokens unchanged). New colour use is token-only: stock bars use status dot colours with % text beside them; status always via `StatusBadge` (icon + label); products via cap + symbol + name; consumption/refill rows carry arrow icons and text labels as well as orange/brand tint.

Screenshots (`screenshots/phase3/`, 1280×800 and 1440×900): `fuel-stations`, `fuel-station-dialog`, `analytics`, `settings-system`. Visual baselines committed for the same pages under `e2e/ui-revamp/__screenshots__/*-darwin.png`.

### Redirect map

No routes added or retired in this iteration; the 23 Phase 1 redirects pass in the full e2e run. Function moves this iteration:

| Function | Now lives |
|---|---|
| Analytics → Ops Monitoring (poison queue) | Settings → Platform → System health (`/dashboard/settings?tab=system`, platform_admin) |
| Ops ingestion/indexing cards | removed with their backend routes (task 0.2) |
| Fuel summary bar figures | Fuel title-row counts + status chips |
| Station detail side panel | Drawer from the row; `?station=<id>` deep link |
| Sourcing query form | "Rank terminals" dialog (title row); Re-rank and Reset in the toolbar |

### Not verified / follow-ups

- Linux visual baselines still not recorded (CI skips pixel compare on Linux).
- No staging deploy, no QA accounts, no flags toggled.

### Housekeeping

`.next` and `test-results` removed after runs; scratch under `Runsheet/tmp/ui-phase3` (logs, shot copies) to be deleted at the end of the phase.

## Iteration 2

Base: `origin/production-readiness/go-live-blockers` @ `f6b0740` (Phase 2 merged, live as ui:26) merged into `ui-phase3` at `2a33d67` (a merge, not a rebase: the branch had no pushed history to rewrite, and the only conflicts were Phase 2's own `BoardToolbar.tsx`/`phase2-evidence.md`, taken from go-live-blockers, plus the one-line `phase3Fake` hook in `shellFake.ts`). Commits `f0ce726..168946c`. Not pushed, not deployed.

Ticked in `spec/tasks.md` this iteration: **3.1, 3.3** (with 3.2, 3.7, 3.9 from iteration 1). Open: **3.4, 3.5, 3.6, 3.8, 3.10, 3.11**. Phase 3 is not complete.

### Review findings (iteration 1 review)

| # | Finding | Fix |
|---|---|---|
| 1 | Phase 3 tasks open | 3.1 and 3.3 done this iteration; Fleet and Customers chrome/axe `test.fail` marks removed; guard 142 → 127. Rest open (above). |
| 2 | Bug class live in TruckCompartments / CustomerTankDetail | `TruckCompartmentsPage`: `formatCapacity` → `format.gallons` ("5,283 gal"), timestamps → `format.dateTime`; the configure dialog uses a whole-gallon `NumberField` (was `type=number`), shows a stored 5283.441… as "5,283" and sends the exact stored value back when untouched. `CustomerTankDetailPage`: product as `ProductChip variant="full"` (cap + "Diesel #2 (on-road)" + code), no raw `fuel_type`; gallons, %, dates, coordinates (5 dp) and K-factor (4 dp) via `lib/format`. Tests: `TruckCompartmentsPage.test.tsx` "shows a stored fractional capacity as whole gallons and sends it back unchanged"; `CustomerTankDetailPage.test.tsx` "shows the product by name with its code, and whole gallons". |
| 3 | Unowned FormDialog rows | Owner/orchestrator decision (notification after Phase 2 merged): Phase 3 owns them; recorded under 3.10 in tasks.md. `ops/CreateJobModal.tsx` → md `FormDialog`; its private bottom-right toast is gone, the parts-risk warning goes through the shared toaster (additive `warning` type in `ui/toast/notify.tsx`), so it now outlives the closed dialog (before, it unmounted with the modal). `orders/OrderDetailView.tsx`: Change status, Assign driver, Place on hold and Cancel order are sm `FormDialog`s (validation and API errors inline; success closes and toasts as before). Release hold stays a direct action (no input). Same buttons, labels and endpoints; Phase 2 layout untouched. Tests: `CreateJobModal.test.tsx` (new, 4), `OrderDetailPage.test.tsx` (+4; the existing cancel/hold tests pass unchanged). |
| 4 | 3.11 vs "portal untouched" | Recorded: 3.11 is the sanctioned exception to the portal-file rule, gated on unchanged portal unit and `e2e/portal` baselines. Not started: Phase 3P is still releasing and owns the portal files (its branch is not on go-live-blockers yet). The shared pieces it needs may land first; the portal swap waits for 3P to merge. |
| 5 | Fuel Stations zero headroom | Confirmed the row can't grow: `Toolbar` is a fixed `h-11` row (chips collapse into "More"), and the partial-load notice is a chip inside it. New e2e `fuelStation.spec.ts` "toolbar stays one 44 px row with a load-failure notice at 1280" (summary 500 → notice visible, toolbar 44 px, first row ≤ 172). Every DataTable list page sits at exactly 172 (title 92 + toolbar 44 + 36 px header); shaving the shared header height would move every Phase 2 baseline, so it is left as is. |
| 6 | Ops Monitoring bookmark | `AnalyticsHub`: `?tab=ops-monitoring` → `router.replace("/dashboard/settings?tab=system")` when the System health section is visible to the caller (admin + platform_admin); everyone else stays on Overview. Tests in `AnalyticsHub.test.tsx` (+3). Row added to the redirect map. |
| 7 | Queue age in raw seconds | New `format.duration(seconds)` ("45 s", "12 min", "3 h 20 min", "2 d 4 h"); `SystemHealthPanel` uses it (an empty queue shows "—"). Tests in `format.test.ts` and `SystemHealthPanel.test.tsx`. |

### Owner-reported staging bug: Billing → Margin "could not be loaded"

Confirmed on staging (read-only probe, `api.staging.runsheetops.com`, QA accounts, sessions signed out 200, no data written, no flag touched):

| Role | `GET /api/commerce/margin/records` | `/summary` | `/settings` | control `GET /api/commerce/invoices` |
|---|---|---|---|---|
| `qa-sweep` (admin) | 404 `COMMERCE_DISABLED` "Margin feed is not enabled" | 404 same | 404 same | 200 |
| `qa-driver` (non-admin) | 404 `COMMERCE_DISABLED` | 404 | 404 | 403 `INSUFFICIENT_ROLE` |

No dispatcher QA account exists locally, so the non-admin check used the driver; `require_margin_enabled` runs before the role check (`commerce/api/margin_endpoints.py:127`), so a dispatcher gets the same 404 while the flag is off and 403 `INSUFFICIENT_ROLE` when it's on (margin-feed evidence, "Admin-only access"). Cause: `COMMERCE_MARGIN_FEED_ENABLED` is off on staging by design and the UI treated the 404 as a load error. The flag was not turned on.

Fix (no backend change; no capability flag exists, so the specific code is used, not a blanket 404):
- `marginApi.getMarginAvailability()` probes `GET /settings`: 404 + `COMMERCE_DISABLED` → `disabled`, 403 → `forbidden`, anything else → `unknown` (the pages keep their own errors).
- `CommerceHub`: the Margin tab is offered only to `canSee("margin")` admins and only when the probe isn't `disabled`; the alert-badge poll stops too. `?tab=margin` with the feed off shows "Margin isn't enabled for your account" (module-disabled state, no Try again, link to Invoices); for a non-admin it shows the standard "You don't have access to this margin page … Ask an administrator for the admin role" state instead of silently falling back.
- `MarginRecordsPage` copy for a mid-session flip: `COMMERCE_DISABLED` → "Margin isn't turned on for this account.", 403 → no-access copy; only real errors say "Try again".
- Tests: `services/marginApi.availability.test.ts` (new, 6), `CommerceHub.margin.test.tsx` (10; the old "dispatcher ?tab=margin falls back" case now expects the no-access state), `MarginRecordsPage.test.tsx` (+4: off, 403, 500, unrelated 404). Verified in the release step (post-deploy): Billing on staging shows no Margin tab for `qa-sweep`, and `?tab=margin` shows the off state.

### Owner-added items

| Item | Result |
|---|---|
| 3.x-owner-1 Portal phone row padding | **Deferred, not started.** Phase 3P's head (`72923a4`/`511a0cc`) is not on `origin/production-readiness/go-live-blockers` yet and 3P owns the portal files during its release. Do after 3P lands: rebase, split the tank-row token, 16 px for account rows and invoice lines, 390×844 screenshots + axe. |
| 3.x-owner-2 Jobs chip counts stale | `JobsView`: `refreshCountsSoon()` (1.5 s debounce) after a row-menu/Fail transition, a create, and every `job_created`/`status_changed`/`delay_alert` socket event. Tests: `JobsView.test.tsx` "refresh after a row-menu status change, debounced" and "refresh once for a burst of live socket events" (one round of `size: 1` reads). |
| 3.x-owner-3 Chip from "More" can clip | `FilterChips` now picks the visible set width-aware (`visibleChipIndexes`): the selected chip first, then leading chips while they fit beside it and "More". Tests: `FilterChips.test.tsx` (+2, incl. a 120 px selected label that pushes out two chips). The Fleet screenshot at 1280 shows All · On time · Delayed · More·2 with nothing cut. |
| 3.x-owner-4 Order tray duplicate heading | `OrderTray` no longer renders the "Orders (n)" heading row; Sort (`TraySortSelect`) sits in the tray tab row, outside the tablist, shown on the Orders tab. Presentation only: `useBoardController`, reducer, DnD and API untouched. Board e2e: aria snapshot updated, new test "Sort sits in the tray tab row, no duplicate heading": first tray card at **141 px** at 1280×800 (tab row bottom 128; the removed row was 40 px: a 32 px control + 8 px padding). N1 drag p95 **17.6 ms** (unchanged). |

### What changed (tasks)

| Task | Result |
|---|---|
| 3.1 Fleet | See the 3.1 note in tasks.md. Files: `FleetDashboard`, `FleetTracking` (+test), new `fleet/AddAssetDialog`, `compliance/ExpiryAlertWidget` (now the chip), `ops/TruckCompartmentsPage` (+test), new `ops/compartmentDialogs`, `drivers/DriverQualificationsView` (+test), `drivers/DriverUtilizationView`, `ops/DriverUtilizationList` (+test), `Inventory` (+ new test), `ops/DriverActivitySection` (+test), `services/schedulingApi` (optional `signal`, additive). |
| 3.3 Customers | `commerce/CustomersListPage` (+test), new `commerce/CustomerFormDialog`, new `commerce/customerStatus`, `commerce/CustomerDetailPage`, `ops/CustomerTankDetailPage` (+test), `NotificationHistoryTab` (+ new test). |

Relegated (no capability lost): the Drivers "DQF Dashboard" view (counts → chips, per-driver badges → Alerts column); Customer detail's "Related records" block (repeated the summary's account and invoice counts); the Inventory stats band (→ chips + "Stock value" in the title row); the tracking table's "In transit only" checkbox (→ status chips, default All).

Shared component changes (all additive, backward-compatible): `lib/format` `duration()` and `calendarDate()`; `PageHeader` `back.onClick` (button back for in-place detail views; `href` still works); `FilterChips` width-aware visible set (same props); `ui/toast/notify` `warning` type; `services/schedulingApi.getJobDriverActivity(…, signal?)`; `services/marginApi.getMarginAvailability`/`isMarginDisabledError`. `ARAgingDashboard` now tolerates a payload without `by_account` (it crashed the Billing tab on the e2e fake's generic body; found by the redirects run).

Phase 2 files edited (allowed after Phase 2 merged, per the orchestrator): `ops/CreateJobModal.tsx`, `orders/OrderDetailView.tsx`, `dispatch/JobsView.tsx`, `dispatch-board/trays/{OrderTray,TrayPanel}.tsx`, `e2e/dispatch-board.spec.ts` (aria snapshot + tray test), `e2e/ui-revamp/shellFake.ts` (the Phase 3 fixture hook moved before the generic invoice/customer lists). No portal or auth file was touched.

### Verification (from `.worktrees/ui-phase3/runsheet`, node_modules symlinked to `merge-board`)

| Check | Result |
|---|---|
| `npx tsc --noEmit` | clean |
| `npm run lint` (biome) | 0 errors, 1 pre-existing warning (`DispatchBoard.tsx:327` `noConfusingVoidType`) |
| `npx jest --ci -w 4` | **143 suites, 1862 passed, 1 skipped**, 0 failed (`a5d5f67`+; an earlier run had a `reset-password` timeout under load: auth file, untouched, passes alone) |
| `format.guard.test.ts` | baseline 142 → **127** (actual 127) |
| Fresh checkout (`git worktree add --detach` of `168946c` under `tmp/`) | `tsc` clean, `next build` exit 0; worktree removed |
| ui-revamp e2e, full run (`--update-snapshots=missing`, at `91aa2a3`+fixes) | 141 expected, 16 unexpected: 12 = the new visual baselines being written (expected on first record), 3 = dispatch-board visual (intended tray change), 1 = redirect row 15 (AR aging crash, fixed). Re-runs: visual (dispatch-board, Fleet, Customers) re-recorded and **passing**; `redirects`, `fuelStation`, `systemHealth`, `tabs` **43 passed**; Fleet trucks chrome/axe/visual after the layout change **8 passed** |
| Dispatch board e2e (`PW_BOARD_PROD=1`) | **18 passed**, 2 skipped (iPad); N1 drag p95 17.6 ms (budget 20), hover 17.6, scroll 17.6 |
| Backend gate | not needed (no backend files) |

Chrome height (first data row, px, budget 172; from the run's `firstRowTop` annotations):

| Page | 1280×800 | 1440×900 |
|---|---|---|
| Fleet → Trucks | 172 | 172 |
| Fleet → Drivers (Utilization) | 172 | 172 |
| Fleet → Drivers (Qualifications) | 172 | 172 |
| Fleet → Inventory | 172 | 172 |
| Customers | 172 | 172 |
| Customers → Communications | 172 | 172 |
| Fuel → Stations | 172 | 172 |
| Analytics (content top, ≤ 92) | 92 | 92 |
| Phase 2 pages (unchanged) | Dashboard 145, Board 168 (1024: 168), Jobs/Plans/Orders 172, Live 145 | same |
| Still marked `test.fail` (open tasks) | Compliance 262 (3.5), Billing invoices 197 (3.4), Reconciliation / Settings depots / flags: no row with fixtures (3.4, 3.8) | |

Axe (wcag2a/2aa/21aa/22aa): 0 critical/serious on Fleet Trucks, Drivers (both views), Inventory, Customers, Communications, Fuel Stations (whole page and shell chrome). Communications first failed `scrollable-region-focusable` (rows had only a click handler); fixed with a "View details" row menu. Still marked: settings-flags (3.8).

Contrast and colour vision: `styles/tokens.test.ts` passes (tokens unchanged). Every new status uses `StatusBadge` (icon + label); product grades are caps + names; the utilization bar has its % as text; overload is an "Overloaded" badge instead of row tinting; the new `warning` toast is white on `--color-warning` (amber 700, passes AA in `tokens.test.ts`).

FormDialog behaviour tests per migrated flow (this iteration): Customer create (2), Add asset (2), Configure compartments (4: names not codes, exact capacity round-trip, inline row errors, gallons payload), Cleaning event (3, incl. presigned evidence), Driver add/edit (2, sections), Inventory create/edit/adjust/delete (4), Create job (3), Order status/hold/cancel/assign (4).

Screenshots (`screenshots/phase3/`, 1280×800 and 1440×900): added `fleet-trucks`, `fleet-drivers`, `fleet-drivers-qualifications`, `fleet-inventory`, `customers`, `customers-communications`, and `dispatch-board` (incl. 1024×768) after the tray change.

### Redirect map additions

| Old | Now lives |
|---|---|
| `/dashboard/analytics?tab=ops-monitoring` | `/dashboard/settings?tab=system` (platform_admin); Overview for others |
| Drivers "DQF Dashboard" button | Fleet → Drivers → Qualifications: status chips (counts) and the Alerts column |
| Fleet tracking "In transit only" | Fleet → Trucks status chips |
| Fleet "Compliance Expiry Alerts" card | Fleet title-row chip → `/dashboard/compliance?tab=certifications` |
| Customer tanks slide-over | Customers row menu → "View tanks" (Drawer); detail `/dashboard/customers/tanks/:id` |
| Truck compartments slide-over | Fleet → Trucks row menu → "Compartments" (Drawer) |
| Inventory stock history modal | Inventory row menu → "Stock history" (Drawer) |
| Inventory low-stock alert panel | Toolbar "n low-stock alerts" → Drawer |
| Driver utilization truck link (`/dashboard` + sessionStorage) | `/dashboard/fleet?tab=trucks&asset=:id` |
| Billing → Margin when the feed is off | hidden; `?tab=margin` explains it's off |

### Not verified / follow-ups

- Linux visual baselines still not recorded (CI skips the pixel compare on Linux).
- No staging deploy. The margin off-state is verified in the release step (post-deploy).
- The Margin probe used `qa-driver` as the non-admin; no dispatcher account was created.
- Open for the next iteration: 3.4, 3.5, 3.6, 3.8, 3.10 (guard 127 → 0, dead-code scan, last `test.fail` marks), 3.11 and owner item 1 (both wait for Phase 3P to merge).

### Housekeeping

`.next`, `test-results` and the fresh worktree were removed after each run. Scratch in `Runsheet/tmp/ui-phase3/` (logs, screenshot copies, the read-only margin probe script, no secrets stored) is kept for the next iteration and deleted at the end of the phase.
