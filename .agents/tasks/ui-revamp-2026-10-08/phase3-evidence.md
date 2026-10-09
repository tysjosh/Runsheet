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

## Iteration 3

Commits `713cee9..b70d8d9` (implementation, reviewed in `phase3-review.md`) plus this iteration's fixes `5300be5..eba18d4`; `origin/production-readiness/go-live-blockers` @ `ab5f933` (portal-fixes: portal files, `PortalAccessPanel`/`PortalOrderingSetting`, SendGrid backend) merged at `18b4829` with no conflicts. Not pushed, not deployed.

Ticked in `spec/tasks.md` this iteration: **3.4, 3.5, 3.6, 3.8, 3.10, 3.11** (3.11 re-scoped, see below). With 3.1, 3.2, 3.3, 3.7, 3.9 from earlier iterations, **every Phase 3 task is ticked**. Nothing in Phase 3 is post-deploy-only except the Margin off-state check carried from iteration 2 (verified in release step (post-deploy)).

### Review findings (iteration 3 review)

| # | Finding | Fix |
|---|---|---|
| 1 | 3.4/3.5/3.6/3.8/3.10/3.11 unticked | Ticked with Done notes after the runs below. |
| 2 | No iteration-3 evidence; `test.fail` marks removed without a green run | This section. The full `e2e/ui-revamp` run is green (below). Measuring it exposed that six Billing entries in `pages.ts` (Accounts, Payments, AR aging, Price books, Pricing rules, Contracts) were measuring the **Invoices fallback**: those tabs are Tier 4 and need `platform_admin` (`config/modules.ts`), and the e2e user was admin + dispatcher. `ShellPage.roles` added; chrome, axe, visual and the dialog spec now sign in with `platform_admin` for them. Real measurements then found the Payments axe issue and the Margin layout (both fixed, below). |
| 3 | Uncommitted `FormDialog`/`Modal`/`Table` changes | Committed in `5300be5` with `ui/sharedAdditions.test.tsx` (each prop set and unset; unset asserts today's classes/behaviour). `stickyTop` prop doc now states the overflow trade-off. All additive. |
| 4 | 3.11 scope vs the portal revert | Recorded in `tasks.md`: 3.11 delivers every shared piece; the portal swap is handed to the Phase 3P workflow, gated on unchanged portal unit and `e2e/portal` fixture baselines. Portal and auth files are untouched by this branch's own commits (only the go-live-blockers merge brings portal-fixes in). |
| 5 | Raw `fuel_grade` in Plans clusters | `FuelDistributionPage` cluster member chips: `ProductChip` (cap + name) and `station_name` when present. Test `FuelDistributionPage.test.tsx` "names the product (not the raw code) and the station in member chips". |
| 6 | Cost import not on FormDialog | `CostImportDialog` is an `lg` FormDialog: "Step 1 of 2 · Check file" → "Step 2 of 2 · Import", file required (field error), stays open on the result (`keepOpenOnSuccess`), "Imported" submit is `aria-disabled` so it can't import twice. Deviation recorded: not the `steps` prop, because the step advances on the server's dry-run answer (async) while `steps[].validate` is synchronous. Tests in `MarginPanels.test.tsx` (3). |
| 7 | Inline numeric create forms | Sourcing wait report: "Report wait time" button → md FormDialog (whole-minute NumberField, source, reporter, notes; same payload and validator; toast; summary re-fetch). Cargo manifest edit: lg FormDialog with a row per item (NumberField kg, 2 dp; an untouched weight is sent back exactly; API errors keep the dialog and edits; status changes stay direct actions on the view; Edit is disabled with no items). Tests: `SourcingPage.test.tsx` (wait-report tests now open the dialog, 25 pass), `CargoManifestEditor.test.tsx` (new, 4). Repo scan: the only `type="number"` left outside portal/auth is `dispatch-board/drawer/CompartmentsTab.tsx:143` (Phase 2, logged below). |
| 8 | Copilot confirmation with an empty id | No Approve/Reject/Open in Approvals without an `action_id`; the card says "Decide this in Live → Approvals."; `decide()` also returns early. Test `AIChat.test.tsx` "offers no decision when the action has no id". Backend-gated: a search of `Runsheet-backend` finds no chat SSE emitter of `type: "confirmation"`, so today the card only appears if the backend starts emitting it; Live → Approvals is the working inbox. |
| 9 | Phase 2 / driver-app edits unrecorded | Listed below; `CompartmentsTab` raw `product_code` logged as a follow-up. |

### Found by the real measurements (fixed)

| Page | Problem | Fix |
|---|---|---|
| Billing → Margin | First row at 249 px (sub-tab row plus a separate toolbar row, wrapping cells), then 173 px (an sr-only `<caption>` still takes 1 px in a table). ISO dates, raw stage codes. | `MarginHub` gives the active view a toolbar slot at the end of the sub-tab row (`marginToolbarSlot.ts`); Records portals Filters and Export CSV there (falls back to its own Toolbar outside the hub). Header like DataTable, rows one line, `aria-label` instead of the caption, sale date as `<time dateTime="2026-10-04">Sun 4 Oct 2026</time>`, stage humanised. Now **172 px**. Tests: `MarginPanels.test.tsx` "puts the Records filters and export in the sub-tab row", `MarginRecordsPage.test.tsx` date test updated. |
| Billing → Payments | axe `scrollable-region-focusable` (serious): rows have no controls. | The scroll container is a labelled, focusable `section` ("Payments list"). |
| Settings → Agents, Metrics | axe `select-name`/`label` (critical): type filters and the metrics date/interval controls had no accessible name. | `aria-label` on the two Agent type selects; `htmlFor`/`id` on the metrics controls. |
| Settings → Tax | Effective/Expires dates wrapped to two lines. | `whitespace-nowrap`. |

### Shared component changes (all additive, opt-in, default = today)

`FormDialog` `mobile`, `submitDisabled`, `keepOpenOnSuccess`; `Modal` `mobile="sheet"`; `DataTable` `rowHeight="touch"`, `stickyTop`; `size="touch"` on `Menu`, `FilterChips`, `EmptyState`, `InlineBanner`; `lib/format` `time(…, { hour12 })`, `zoneName()`, `days()`. No existing caller passes them. Tests: `ui/sharedAdditions.test.tsx` (13), `lib/format.test.ts` (+4).

### Files outside Phase 3's own pages (recorded per finding 9)

- Phase 2 files, format-only for guard-to-zero (iteration 3 implementation): `dispatch-board/drawer/CompartmentsTab.tsx`, `dispatch-board/trays/DriverTray.tsx`, `ops/ReplanDiffBody.tsx`, `orders/OrderDetailView.tsx`. This iteration also: `ops/CargoManifestEditor.tsx` (used by the dispatch job detail; review finding 7). Board e2e and N1 unchanged (below).
- `driver-app/lib/tokens.ts` regenerated by `build-tokens.mjs --with-driver` with the two new `open`/`partial` statuses (additive; no driver screen uses them).
- Follow-up (not edited, Phase 2 file): `CompartmentsTab.tsx:138, 359` print raw `product_code`; `:143` is a `type="number"` input.

### Verification (from `.worktrees/ui-phase3/runsheet`, node_modules symlinked to `merge-board`; final code at `eba18d4`)

| Check | Result |
|---|---|
| `npx tsc --noEmit` | clean |
| `npm run lint` (biome) | 0 errors, 2 warnings: pre-existing `DispatchBoard.tsx:327` `noConfusingVoidType`; `portal/PortalTitleRow.tsx:40` unused suppression (portal file from the go-live-blockers merge, not edited here) |
| `npx jest --ci -w 2` | **168 suites, 2351 passed, 1 skipped, 0 failed**. (A `-w 4` run under load average ~10 timed out 10 tests in 8 suites at ~7 s; those 8 suites pass alone, and the `-w 2` run is clean.) |
| `format.guard.test.ts` | baseline **0** (actual 0) |
| Fresh checkout (`git worktree add --detach` of `eba18d4` under `tmp/`) | `tsc` exit 0, `next build` exit 0 (32/32 pages); worktree removed |
| ui-revamp e2e, full run (`npx playwright test -c playwright.ui-revamp.config.ts --project=chromium`, at `e352804`, `--update-snapshots=missing`) | **255 passed, 0 failed except the 2 Margin visual baselines being written** (first record of a missing file). No `test.fail` marks exist. Includes 62 axe, 24 redirects (all 23 rows + shell), 21 Phase 3 dialog/section axe, tabs, systemHealth, fuelStation |
| ui-revamp visual re-run (no recording, all baselines present, at `eba18d4`) | **65 passed** |
| ui-revamp chrome spec (JSON reporter, px below) | **65 passed** |
| Dispatch board e2e (`PW_BOARD_PROD=1 npx playwright test -c playwright.dispatch-board.config.ts`, all projects) | **28 passed, 32 skipped** (project-scoped specs: webkit/iPad variants), 0 failed. N1 (chromium, production bundle): drag p95 **17.5 ms** (budget 20), hover p95 17.4, scroll p95 17.7, 12 rendered lane rows |
| Backend gate | not needed: no backend file changed by this branch's commits (the merged go-live-blockers SendGrid change was reviewed and CI'd there) |

Chrome height, first data row (px, budget 172; `firstRowTop` annotations from `chrome.spec`):

| Page | 1280×800 | 1440×900 |
|---|---|---|
| Compliance → Certifications / Meters / BOLs / IFTA | 172 / 172 / 172 / 172 | 172 / 172 / 172 / 172 |
| Billing → Invoices, Reconciliation | 172, 172 | 172, 172 |
| Billing → Accounts, Payments, AR aging (platform_admin) | 172, 172, 172 | 172, 172, 172 |
| Billing → Price books, Pricing rules, Contracts (platform_admin) | 172, 172, 172 | 172, 172, 172 |
| Billing → Margin (Records) | 172 | 172 |
| Settings → Depots / Flags / Tax / Exemptions | 172 / 172 / 172 / 172 | 172 / 172 / 172 / 172 |
| Fleet (Trucks, Drivers ×2, Inventory), Customers, Communications, Fuel Stations | 172 each | 172 each |
| Analytics (content top, ≤ 92) | 92 | 92 |
| Phase 2 (unchanged) | Dashboard 145, Board 168 (1024×768: 168), Jobs/Plans/Orders 172, Live 145 | same |

Axe (wcag2a/2aa/21aa/22aa), 0 critical/serious:
- Shell chrome and whole page on all 31 `pages.ts` pages at 1440×900 (`axe.spec`).
- Open dialogs at 1280×800, each also checked as a labelled `aria-modal` dialog that Escape closes (`phase3Dialogs.spec.ts`, new): New customer, Add certification, Register meter, Upload terminal BOL, New price book, Add pricing rule, Add contract, Add jurisdiction rate, Add exemption certificate, Add depot, Register channel, Upload road restriction. Plus Edit fuel station (`fuelStation.spec`).
- Whole page on the Settings sections that aren't list pages: Road restrictions, Weather alerts, Notifications, Integrations, Intake channels, Stripe, Agents, Import, Metrics.

Contrast and colour vision: `styles/tokens.test.ts` passes in the jest run, including `open`/`partial` (blue family, ≥ AA text on their fills, distinguished from each other by icon and label, declared CVD pair). Colour is never the only signal on the new pages: statuses are `StatusBadge` (icon + label), products cap + symbol + name, Missing cost is a labelled badge with its reason in words, Variance is a badge instead of a row tint.

FormDialog behaviour tests per migrated flow (this iteration's additions in bold; iteration-3 implementation tests listed as they stand):
- Billing: invoice void and credit override, account detail (`commerce/__tests__`), price book sectioned lg + add-rule sub-dialog (`PriceBookEditor`), cost entry add/supersede/void and margin settings (`MarginForms.test.tsx`), **cost CSV import two-step (3)**.
- Compliance/Settings: Add certification, Register meter, Upload BOL, IFTA mileage adjustment, Add exemption, Add rate (cents ↔ tenths), Add/Edit contract, Add pricing rule (`src/components/compliance`), depots, flag Change state, intake channel, integration connect, road restriction, notification template with live preview (`src/components/admin`, `NotificationSettingsTab.test.tsx`).
- Ops: **Sourcing wait report (5 dialog cases)**, **cargo manifest edit (4)**, Plans Emergency stop / Cost configuration (`FuelDistributionPage.dialogs.test.tsx`).
- Shared: **`sharedAdditions.test.tsx` (13)**; `FormDialog.test.tsx` (15, unchanged).

Screenshots (`screenshots/phase3/`, 1280×800 and 1440×900, from the visual run at `e352804`): every visual page, including the new `compliance-{certifications,meters,bols,ifta}`, `billing-{invoices,accounts,payments,ar-aging,reconciliation,price-books,pricing-rules,contracts,margin}`, `settings` (depots), `settings-{flags,tax,exemptions}`. Darwin baselines committed under `e2e/ui-revamp/__screenshots__/`.

### Redirect map additions (iteration 3)

| Old | Now lives |
|---|---|
| `/ops/command` (agent console) | `/dashboard/control?tab=approvals` (redirect row 5); chat and medium-risk confirmation in Copilot (top bar) |
| Console Approve/Reject | Copilot confirmation card (approvals API) or Live → Approvals |
| `AgentToast` | removed; approvals surface in Live → Approvals and the top-bar alerts count |
| Compliance → Tax jurisdictions, Exemptions | Settings → Company → Tax jurisdictions (`?tab=tax`), Exemptions (`?tab=exemptions`) |
| Billing → Contracts, Pricing rules | Billing tabs (Tier 4, platform_admin), FormDialogs from the title row |
| Margin records filters/export row | the Margin sub-tab row (Filters, Export CSV) |
| Sourcing inline wait-report form | candidate details → "Report wait time" dialog |
| Cargo manifest inline edit mode | Job detail → Cargo → "Edit" dialog |

All 23 Phase 1 redirects plus row 5 pass in the full run.

### Not verified / follow-ups

- Linux visual baselines still not recorded (CI skips the pixel compare on Linux); the Darwin baselines are self-recorded.
- The Copilot confirmation path is backend-gated (no SSE emitter); covered by unit tests only.
- `CompartmentsTab.tsx` raw product codes and `type="number"` (Phase 2 file).
- 3.11's portal swap (handed to the Phase 3P workflow) and owner item 3.x-owner-1 (portal row padding) stay with the portal stream.
- No staging deploy, no QA accounts, no flags touched this iteration.

### Housekeeping

`.next`, `test-results` and the fresh worktree removed. Scratch under `Runsheet/tmp/ui-phase3/` (logs, run scripts, screenshot copies) is deleted at the end of this iteration.

## Iteration 4

Commits `53517ee..a4a702c` on `production-readiness/ui-phase3`. `origin/production-readiness/go-live-blockers` @ `e636358` (F1–F15 staging sweep) merged at `53517ee` with the nine conflicts resolved by hand, then @ `e19fb9b` (Mailtrap SMTP, backend only, no conflicts) merged at `a4a702c`. After the merges, `Runsheet-backend/`, portal and auth files are byte-identical to go-live-blockers (`git diff --stat origin/production-readiness/go-live-blockers HEAD -- Runsheet-backend runsheet/src/components/portal runsheet/src/app/portal runsheet/src/app/auth` is empty). Not pushed, not deployed.

All Phase 3 tasks stay ticked (3.1–3.11). Nothing new is post-deploy-only.

### Review findings (iteration 4 review)

| # | Finding | Resolution |
|---|---|---|
| 1 (HIGH) | Branch stale against go-live-blockers; F-fixes at risk | Merged and ported (table below), then re-verified. |
| 2 (LOW) | `CompartmentsTab` raw codes / `type="number"` (Phase 2 file) | Still a follow-up for the next board change. Not edited. |
| 3 (LOW) | Visual baselines are Darwin-only | Still a follow-up: record Linux baselines before go-live. |

### Conflict resolution: how each F-fix landed in the Phase 3 components

| File | Resolution |
|---|---|
| `commerce/ARAgingDashboard.tsx` (F12) | Phase 3 layout kept. A Current bucket was added first, in the `open` (blue) hue, with the OK, Warning, Overdue and Critical hues following. The toolbar legend now reads "Days past due: Current · 1–30 · 31–60 · 61–90 · 90+", and each label's full `AGING_LABELS` text sits in an sr-only span and a `title`. The title-row counts read "90+ days past due". The accounts table gains a Current column, and "0–30" became "1–30". In the History drawer, Current shows "—" for snapshots from before due-date aging. The upstream test "renders '—' for Current on history snapshots…" now opens the History drawer first (Phase 3 moved history there). |
| `commerce/AccountDetailPage.tsx` (F12) | Phase 3 template kept. The aging facts are six columns: Current plus the four `AGING_LABELS` buckets and Total open. |
| `ops/FuelSummaryBar.tsx` (F5, modify/delete) | It stays deleted, because Phase 3 replaced it with the Fuel title-row counts. The "—" now lives in `FuelDashboardView` as `avgDaysLeftLabel()`: the counts always show "Avg — days left" when `average_days_until_empty` is null or 0. The upstream test moved to `ops/FuelDaysLeft.test.tsx` and now targets the new components: the title-row label, list "—" with no 99999, sort-last in both directions (DataTable header), station detail "—", and `displayDaysUntilEmpty`. |
| `ops/FuelStationList.tsx`, `ops/FuelStationDetail.tsx` (F5) | Phase 3 DataTable and drawer kept. Both use `displayDaysUntilEmpty`, and no-consumption stations sort last. |
| `Analytics.tsx` (F2/F3/F13) | The upstream rewrite was the base: real KPIs only, null-safe, a real 30-day series, error state with Retry, the empty-snapshot state, and weighted route average. The Phase 3 chrome was re-applied on top: no nested header (the hub owns the title), "As of …" published to the Analytics hub title row through `usePageChrome` (inline when rendered standalone), `CHART` pie palette, `number()` and `dateTime()` formatters, and the KPI card and section-heading styles. The range selector stays hidden (D13; the upstream test asserts there's no combobox). |
| `ops/SchedulingMetricsPage.tsx` (F4/F11) | Upstream was the base: UTC bucket labels, completion rate already in 0–100, "Dates are in UTC". The Phase 3 formatters were re-applied on top, and completion now goes through `pct(rate, { decimals: 1 })` without `fraction`. |
| `CommerceHub.tsx` + `marginApi.ts` (Margin availability) | After resolution, both files match Phase 3's version exactly. The availability code was the same on both sides, so one implementation remains: `getMarginAvailability`, `MarginUnavailable` and `isMarginDisabledError`. The conflicts were only in Phase 3's detail-route navigation, and that side was kept. |
| `e2e/ui-revamp/shellFake.ts` | Phase 3 side kept. The upstream `/commerce/ar-aging` and IFTA stubs would have shadowed the richer `phase3Fake` fixtures, which are consulted later. `phase3Fake` AR aging gained `bucket_current_cents` (summary and per-account), and totals were adjusted to match. `phase3Fake` also gained `/analytics/metrics` (with `as_of`), `/analytics/routes` and `/analytics/timeseries` in the new shape. Without them, the Analytics visual guarded an empty page. |

Also fixed: `format.guard` found two new direct `toLocale*` calls from the merge, one in Analytics `formatAsOf` (now `dateTime()`) and one in a SchedulingMetrics comment (reworded). The baseline is back to 0.

### Verification (from `.worktrees/ui-phase3/runsheet`, node_modules symlinked to `merge-board`; code at `4d114d2`, the final merge adds backend files only)

| Check | Result |
|---|---|
| `npx tsc --noEmit -p .` | clean |
| `npm run lint` (biome) | 0 errors, 2 warnings: `DispatchBoard.tsx:327`, which predates this iteration, and the unused suppression in `portal/PortalTitleRow.tsx:40` (portal file, not edited) |
| `npx jest --silent --maxWorkers=50%` | **170 suites; 2373 passed, 1 skipped, 2 failed**. The two failures are 5 s timeouts in files this iteration didn't touch (`dispatch-board/map/map.test.tsx`, `portal/__tests__/RequestDeliveryDialog.test.tsx`) under load average ~12. Re-run alone: 25/25 pass. An earlier full run had 4 different timeouts (`AgentHealth`, `forgot-password`, `cargo/page.real-shape`) plus the real `format.guard` failure. The guard is fixed, and those three pass alone. |
| Targeted suites for merged areas | Analytics, AnalyticsHub, SchedulingMetricsPage, FuelDaysLeft, ARAgingDashboard (11/11), AccountDetailPage, CommerceHub.margin, hubs.urltabs, margin/*, marginApi.availability, format.guard: all pass |
| `npm run build` | exit 0 |
| Fresh checkout (`git worktree add --detach` of `4d114d2` under `tmp/`) | `tsc` OK, `next build` exit 0 (32/32 pages, `BUILD_ID` written); worktree removed |
| ui-revamp e2e, full (`npx playwright test -c playwright.ui-revamp.config.ts --project=chromium`) | Before the fixture update: 255 passed, and the only 2 failures were the Analytics visuals (expected after the F2/F3 rewrite). After the fixture update and re-recording the Analytics baselines (`-g analytics --update-snapshots`, 8 passed), a full re-run gave **257 passed, 0 failed**. That covers chrome, axe (62 + 21 dialogs), 24 redirects, tabs, systemHealth, fuelStation and visual. |
| Dispatch board e2e (`PW_BOARD_PROD=1`, all projects) | **28 passed, 32 skipped**, 0 failed. N1 drag p95 **18.4 ms** (budget 20), hover 18.3, scroll 18.5, 12 lane rows |
| Backend gate | Not needed. This branch changes no backend file, and the backend is identical to go-live-blockers, where it was CI'd. |

Chrome height, first data row, from `firstRowTop` annotations in the full re-run (budget 172, 1280×800 / 1440×900): Fleet trucks, drivers, qualifications and inventory, Customers, Communications, Fuel stations, Compliance certifications, meters, BOLs and IFTA, Billing invoices, accounts, payments, AR aging, reconciliation, price books, pricing rules, contracts and margin, and Settings depots, flags, tax and exemptions: **172 / 172 each**. Analytics content top 92 / 92. Phase 2 is unchanged: Dashboard 145, Board 168 (1024×768: 168), Jobs, Plans and Orders 172, Live 145.

Axe: 0 critical or serious on every page and dialog in the full run, including the merged Fuel, Billing AR aging and Analytics pages.

Contrast and colour vision: `styles/tokens.test.ts` passes. In the new AR Current bucket, colour isn't the only signal: each bucket is labelled in text, and the bar's `aria-label` gives every bucket's share.

Screenshots: `screenshots/phase3/` was refreshed from the full re-run (67 PNGs, 1280×800 and 1440×900), including `analytics-*` (KPIs, as-of in the title row, trend, gauge, coloured route mix) and `billing-ar-aging-*` (Current first, "Days past due" legend).

Redirect map: unchanged since iteration 3. All 24 redirect tests pass in the full re-run.

### Not verified / follow-ups

- The `billing-ar-aging` visual passed against its iteration-3 baseline even though the Current column was added, so that compare is looser than the screenshot suggests. The baseline wasn't re-recorded. The screenshot in `screenshots/phase3/` shows the new render.
- Carried: Linux visual baselines; `CompartmentsTab` codes (Phase 2); Copilot confirmation is backend-gated; Margin off-state is verified in the release step (post-deploy).

### Housekeeping

`.next`, `test-results`, `coverage` and the fresh worktree removed. Scratch under `Runsheet/tmp/ui-phase3/` and `tmp/p3merge/` deleted.
