# UI revamp: tasks

Read `requirements.md` and `design.md` first. Branch per phase off `production-readiness/go-live-blockers` (`ui-revamp/p0-backend`, `ui-revamp/p1-foundation`, `ui-revamp/p2-dispatch`, `ui-revamp/p3-modules`, `ui-revamp/p3p-portal`, `ui-revamp/p4-driver`), merged in order, except that Phase 3P may merge before or after Phase 2 and 3. Every task leaves the build green.

Standard verification commands (from `.github/workflows/ci.yml`):

- Web: `cd runsheet && npx tsc --noEmit && npm run lint && npm test -- --ci && npm run build`
- Web e2e (new): `cd runsheet && npx playwright test e2e/ui-revamp --project=chromium`
- Board e2e (must stay green): `cd runsheet && npx playwright test -c playwright.dispatch-board.config.ts --project=chromium`
- Backend: `cd Runsheet-backend && python -m pytest <paths> -q`, then `python scripts/generate_endpoint_registry.py` and commit `docs/endpoint-registry.md` when routes change
- Driver: `cd driver-app && npm run check-types && npm run lint && npm test -- --ci`

Pre-condition for Phase 2 and 3 (met: merged at `ea3e9e2`): `production-readiness/customer-portal` is merged into go-live-blockers (it edits `OrdersPage.tsx`, `OrderDetailView.tsx`, `CustomerDetailPage.tsx`, `config/modules.ts`, `app/layout.tsx`, `signin/page.tsx`). Phase 1 tasks avoid those files except where noted, and rebase after the merge.

Sizes: S ≤ 0.5 day, M ≈ 1 day, L ≈ 2 days.

## Phase 0: ship ahead of the redesign (backend, small)

- [ ] 0.1 (S) Return CORS headers on unexpected 500s.
  Add `Runsheet-backend/middleware/unhandled_errors.py` (pure ASGI, catches `Exception`, logs with request id, returns the JSON envelope `{"error_code":"INTERNAL_ERROR","message":…,"details":{},"request_id":…}` with status 500). Register it in `main.py` immediately before the `app.add_middleware(CORSMiddleware, …)` call (~line 175) so it is inside CORS. Leave `errors/handlers.py:366` in place.
  Accept: a route raising `RuntimeError` returns 500 with `access-control-allow-origin` for an allowed origin and the envelope body. `AppException` and 422 handling are unchanged.
  Verify: new `tests/unit/test_unhandled_errors_cors.py` (FastAPI `TestClient`, `Origin: https://app.staging.runsheetops.com`); `python -m pytest tests/unit/test_unhandled_errors_cors.py tests/unit -q -x`.

- [ ] 0.2 (S) Scope and prune ops monitoring routes.
  In `ops/api/endpoints.py:1347-1600`: add `tenant: TenantContext = Depends(get_tenant_context)` and `require_role(tenant, "platform_admin")` to `/monitoring/poison-queue`. Delete `/monitoring/ingestion` and `/monitoring/indexing` (Elasticsearch-only queries over indices dropped by migration 0007; the ingestion route 500s with `UnsupportedAggregationError` on Postgres). Leave `/metrics/prometheus` and the feature-flag admin routes unchanged.
  Accept: dispatcher and admin get 403 on poison-queue, platform_admin 200; ingestion and indexing are 404.
  Verify: add the cases to the existing ops endpoint tests (`rg -l "monitoring/poison-queue" Runsheet-backend/tests`); run them; regenerate the endpoint registry.

## Phase 1: tokens, design system, shell and navigation (≈ 8–10 days)

- [x] 1.1 (M) Token source and generator.
  Create `design/tokens.json` exactly as `design.md` §2.1 and `scripts/build-tokens.mjs` per §2.2 (Node, no deps, `--check`). Generate `runsheet/src/styles/tokens.css` and `tokens.ts`, `driver-app/lib/tokens.ts`, and the marked block in `driver-app/global.css` (add `/* tokens:start */` and `/* tokens:end */` around the `@layer base` variables).
  Accept: running the script twice yields no diff; `--check` exits 1 after a manual edit to a generated file.
  Verify: `node scripts/build-tokens.mjs && node scripts/build-tokens.mjs --check`; add a CI step `node scripts/build-tokens.mjs --check` to the web job in `.github/workflows/ci.yml`.
  Done (Phase 1, branch `production-readiness/ui-phase1`). Deviation: the driver outputs (`driver-app/lib/tokens.ts`, the `tokens:start/end` block in `driver-app/global.css`) are generated only with `--with-driver` and are not written yet, because Phase 1 must not change the driver app. Moved to task 4.1.

- [x] 1.2 (S) Token tests.
  `runsheet/src/styles/tokens.test.ts`: contrast for every status `fg/bg`, `dot/white`, product `fg/bg`, semantic text pairs, identity white-on-colour (≥ 4.5:1 text, ≥ 3:1 UI; cyan identity allowed ≥ 4.5 using `cyan.700`); CVD ΔE ≥ 4 for co-occurring status pairs under deuteranopia and protanopia (matrices in `design.md` §10 / `palette.html`); every product code in `fuel_product_catalog.py` has a token (hard-code the 9 codes with a comment pointing at the file).
  Verify: `npx jest src/styles/tokens.test.ts`.

- [x] 1.3 (M) Wire tokens into the web app.
  Replace the colour variables at the top of `src/app/globals.css` with `@import "../styles/tokens.css";`, keeping legacy aliases (`primary`, `primary-hover`, `success`, `error`, `warning`, `info`, `surface-*`) as defined in §2.2. Point `components/Analytics.tsx` and `components/ReportViewer.tsx` at `styles/tokens.ts`; delete `styles/design-tokens.ts` and `styles/css-vars.ts`.
  Accept: no visual change except the alias colours that now pass AA (warning → amber.700, info → blue.700).
  Verify: web standard commands; open `/dashboard` locally and compare with the staging screenshot `screenshots/dispatcher-1440-today.png`.

- [x] 1.4 (M) `lib/format.ts` and the guard test.
  Implement §3.1 with tests (locales `en-US`, `de-DE`; time zone `America/Chicago`). Add `TenantSettingsContext` in `app/dashboard/layout.tsx` (time zone from `apiService.getAccountProfile()`, falling back to the browser). Add `src/lib/format.guard.test.ts` with the current baseline count of `toFixed(` + `toLocale` in `src/components` (record the number in the test).
  Verify: `npx jest src/lib`.

- [x] 1.5 (L) Core components.
  In `components/ui/`: update `Button` (drop `success`/`warning`; migrate their call sites: `rg -n 'variant="(success|warning)"' src`), add `IconButton`, `StatusBadge`, `ProductChip`, `ProductSelect`, `IdentityAvatar` (+ `lib/identity.ts`), `Tabs`, `Toolbar`, `FilterChips`, `Field`, `NumberField`, `Select`, `Drawer`, `Menu`, `Tooltip`, `InlineBanner`, `Skeleton`; extend `Table` into `DataTable` (keep `Table` export as an alias); extend `LoadErrorState` with `failure.kind` and the OI-48 copy fix. Export all from `ui/index.ts`.
  Accept: unit tests per `design.md` §10 "Components" row; `StatusBadge` renders an icon and a label for each of the 13 statuses; `aria-sort` only on `th`.
  Verify: `npx jest src/components/ui`.

- [x] 1.6 (M) `FormDialog`.
  Implement per `design.md` §5 on top of `ui/Modal.tsx`. Tests: focus trap and return, Esc/× close, dirty confirm, Enter submits (not in textarea), envelope field errors (both shapes), form-level error banner, spinner, success toast, steps and sections.
  Verify: `npx jest src/components/ui/FormDialog.test.tsx`.

- [x] 1.7 (M) Compact `PageHeader` and page-chrome context.
  Rewrite `ui/PageHeader.tsx` per §3 (44 px, single `<h1>`, inline `tabs`, `context`, `counts`, `actions`, `help`, `back`); add `PageChromeProvider`/`usePageChrome()` so embedded tab content contributes actions instead of rendering its own header. The `subtitle` and `icon` props remain accepted but are not rendered (`subtitle` feeds `help` when `help` is absent), so untouched pages compile and immediately get compact headers.
  Accept: every page shows one `<h1>`; existing `PageHeader` call sites compile unchanged.
  Verify: `npx jest src/components/ui/PageHeader.test.tsx`; web standard commands.

- [x] 1.8 (L) Shell: top bar, sidebar, user menu.
  Move `Header.tsx` and `Sidebar.tsx` to `components/shell/`, with `NAV_SECTIONS` in `config/nav.ts` per `requirements.md` R2.2. The top bar is 48 px: drop the "Runsheet" `<h1>`, `GlobalSearch` gets the ARIA combobox pattern (`role="combobox"`, `aria-expanded`, `aria-controls`, `aria-activedescendant`) and a `/` shortcut, the Copilot send button gets `aria-label` (`AIChat.tsx:691`), and the user menu has Profile and Sign out. The sidebar has groups, the collapsible Back office (localStorage key `rs.nav.backoffice`), and count badges for Orders and Live. Register `orders`, `live`, `settings`, `system-health` in `config/modules.ts` and update `modules.test.ts` (this touches `modules.ts`: do it after the portal merge, or rebase it carefully).
  Accept: R2.1–R2.6; axe on `/dashboard` has no `aria-allowed-attr` or `button-name` violations.
  Verify: `npx jest src/components/shell src/config`; web standard commands.

- [x] 1.9 (M) URL-synced tabs in every hub.
  Replace `useState` tab state with `Tabs`/`useSearchParams` in `AdminHub`, `AnalyticsHub`, `CommerceHub`, `ComplianceHub`, `DriversHub`, `FleetDashboard`, `FuelOpsPage`, `NotificationsHub`, `SetupHub`, `DispatchPage` (keep its `scheduling`/`distribution` values working as aliases of `jobs`/`plans`).
  Accept: R2.4; refreshing or sharing a URL keeps the tab.
  Verify: `npx jest` for each hub's tests; a Playwright test opening each `?tab=` URL.

- [x] 1.10 (L) Route consolidation and redirects.
  Create `config/redirects.ts` with the 23 rows of `design.md` §4 plus `/ops` → `/dashboard`, and map it in `next.config.ts` `redirects()`. Create the new `/dashboard/...` route files listed in §4 (each delegating to the existing component, with a `layout.tsx` title). Create `/dashboard/settings` hosting the Setup and Admin tabs (§4 Settings tabs) with role gating. Update every in-app link to old paths and `DASHBOARD_ITEM_PATH`. Delete the old `app/ops/**`, `app/commerce/**`, `app/orders/**`, `app/compliance/**`, `app/admin/**` route files, except `app/ops/command`, which stays until task 3.6. Move `admin/integrations/page.tsx` and `admin/weather-alerts/page.tsx` content (and their `page.test.tsx`) into `components/admin/` first, since `AdminHub` lazy-imports them; likewise `app/ops/scheduling/page.tsx` (imported by `DispatchPage`) and `app/ops/(overview)/page.tsx` and `app/ops/control/page.tsx` (imported by Fleet and Control routes).
  Accept: R3.1–R3.4; `rg -n '"/ops/|"/commerce|"/orders/|/compliance/terminals|"/admin/' runsheet/src` returns only `redirects.ts` and tests.
  Verify: `e2e/ui-revamp/redirects.spec.ts` (one test per row: old URL → final URL and `<h1>`); web standard commands.

- [x] 1.11 (M) E2E harness for the revamp.
  Add `e2e/ui-revamp/` with fixtures (`page.route` serving ≥ 20 rows per list endpoint), the `firstRowTop` helper, `chrome.spec.ts` (expected to fail until each page migrates: mark with `test.fail()` per page, flipped as pages land), `axe.spec.ts` (`@axe-core/playwright`, tags `wcag2a`, `wcag2aa`, `wcag21aa`, `wcag22aa`, assert 0 critical/serious, per page), and `visual.spec.ts` (`toHaveScreenshot` at 1280×800 and 1440×900). Add a CI job mirroring `dispatch-board-e2e` for `e2e/ui-revamp`.
  Verify: `npx playwright test e2e/ui-revamp --project=chromium` passes (with the expected-fail marks).
  Done. Run with `npx playwright test -c playwright.ui-revamp.config.ts --project=chromium` (the spec's own config, fake backend). Visual baselines are per platform (`*-darwin.png`); Linux CI skips the pixel compare until Linux baselines are recorded. CI job `ui-revamp-e2e` added.

## Phase 2: Dashboard and Dispatch Board (≈ 8–10 days)

- [x] 2.1 (L) Dashboard.
  Build `components/dashboard/Dashboard.tsx` per `design.md` §7.1 and `../mockup-dashboard.html`, reusing `OrderAttentionRow` and the feed severity logic from `DispatchCockpit.tsx`. `app/dashboard/(today)/page.tsx` renders it. Widgets use `Promise.allSettled` and their own `LoadErrorState`. Inline approve uses the same `agentApi` call as `ApprovalQueuePanel`.
  Accept: R7.1–R7.6; each widget header and row navigates per the §7.1 table; first row ≤ 172 px at 1280×800 and 1440×900.
  Verify: `npx jest src/components/dashboard` (port `DispatchCockpit.test.tsx` cases); `chrome.spec.ts` and `axe.spec.ts` for `/dashboard` pass without `test.fail`.
  Done (Phase 2, branch `production-readiness/ui-phase2`, see `../phase2-evidence.md`). h1 is "Dashboard" (mockup), with a Today/Tomorrow toggle. Deviation: the board socket is not joined from the Dashboard (it would show dashboard viewers as board presence); orders and scheduling sockets plus a 120 s poll refresh it. Low-tank "Create order" prefills product and refill gallons. Review fix (iteration 2): deviation from the design §7.1 table, the "delayed" count opens Jobs `?status=delayed` in both modes, because the count comes from `/scheduling/jobs/delayed` and the board has no `delayed` status to filter on. The "exceptions" count opens today's board filtered to the counted stops' statuses (`status=failed,rejected`), or the stop itself when there is one, or Jobs → Failed when the board has none. The board Filters panel shows such deep-linked statuses as pressed chips so they can be cleared.

- [x] 2.2 (L) Dispatch title row and board toolbar.
  Per §7.2: `DispatchPage` with `PageHeader` tabs Board · Jobs · Plans. Split `dispatch-board/BoardToolbar.tsx` into `BoardTitleControls` and `BoardToolbarRow` registered via `usePageChrome()`. Add the View menu (Timeline/Sequence, Comfortable/Compact, Map) keeping `viewState.ts` keys. Move Suggestions list, shortcuts and history into `⋯`. `PlaceModeBanner` renders in the toolbar slot.
  Accept: R8.1–R8.3, R8.5; first lane ≤ 172 px at 1024×768, 1280×800, 1440×900; board e2e suite and N1 budgets green.
  Verify: `npx jest src/components/dispatch-board src/components/DispatchPage.test.tsx`; board e2e command; `chrome.spec.ts` board cases.
  Done. Map stays a separate toggle chip beside the View menu (as in the mockup); "Truck history" in `⋯` opens the selected (or first) lane's History tab, since the board has no board-wide history. The read-only (shadow / past day) notice is a chip in the toolbar row.

- [x] 2.3 (M) Board colour: identity, product, status.
  Per §7.2 bullets for `LaneHeader`, `LoadBlock`, `StopCard`, `GhostLoad`, `OrderCard`, `CompartmentGauge`, `CheckChip`. Use `assignLaneIdentities`. Lane accessible names keep their current text plus the truck type (as today).
  Accept: R8.4; colour never alone (every coloured element has an icon or text); axe 0 critical/serious on the board.
  Verify: `npx jest src/components/dispatch-board`; board e2e; update its screenshot baselines if any.
  Done. Stop and order card faces are memoised so the N1 drag p95 stays at baseline (17.7 ms vs 17.6 ms). Accessible names of stops, order cards and gauge segments are unchanged (they still carry the product code the board e2e and screen-reader tests rely on); visible text uses product names and caps.

- [x] 2.4 (M) Jobs view.
  Move `app/ops/scheduling/page.tsx` content into `components/dispatch/JobsView.tsx` (no header), with `FilterChips` counts replacing `JobSummaryBar`, `DataTable` replacing `JobBoard` table rendering (keep `JobBoard`'s sort and column logic), a row menu from `JobActionButtons`, no row tinting, and Cargo search in the overflow. Job detail and cargo routes from task 1.10. Fix `[id]/page.tsx`'s swallowed `transitionStatus` error (currently `console.error` only, `app/ops/scheduling/[id]/page.tsx:15-25`) by surfacing a toast.
  Accept: R8.6; first row ≤ 172 px.
  Verify: `npx jest src/components/ops/JobBoard.test.tsx src/components/dispatch`; e2e chrome/axe for `?tab=jobs`.
  Done. Rows open `/dashboard/dispatch/jobs/:id`; `?status=` selects the chip (Dashboard "delayed" link). The job route no longer re-calls the transition API (the detail view already does); errors toast from the detail view. Review fix (iteration 2): the status chip is a server filter again. A status chip is sent as `status` (also to Export CSV), Delayed reads `/scheduling/jobs/delayed` (the toolbar filters apply to that set client-side, and Export is hidden on Delayed because the export route can't filter it), the list pages on the server at 50 per page, and chip counts are tenant totals from one `size: 1` read per status. Search narrows the page on screen ("Search this page"). Chips that don't fit collapse into "More · n" (R4.3).

- [x] 2.5 (M) Plans view.
  `FuelDistributionPage.tsx`: remove its `PageHeader` (`:3653`) and `TabNavigation` (`:3658`); sub-views via a toolbar segmented control `?sub=`. Reject and replan dialogs move to `FormDialog`.
  Accept: R8.7; no nested header.
  Verify: `npx jest src/components/ops/FuelDistributionPage*.test.tsx`; e2e chrome/axe for `?tab=plans`.
  Done. Each sub-view's single toolbar row holds the `?sub=` switch plus that view's controls (Plans: status, refresh, cost settings, Generate Plan); the plan list is a `DataTable`.

- [x] 2.6 (M) Live.
  Build `components/live/LiveView.tsx` per §7.3; `/dashboard/control` renders it; Approvals tab supports `?id=`; remove the lat/lon grid; autonomy chip; `OperationsMap` identity colours.
  Accept: R9.1–R9.3.
  Verify: `npx jest src/components/live src/components/ops/ApprovalQueue.test.tsx`; e2e chrome/axe for `/dashboard/control`.
  Done. Default-depot and inventory warnings are title-row chips (0 px otherwise). Without a Google Maps key the map shows an empty state and the truck list carries the data. Review fix (iteration 2): deviation from design §6, the chrome budget measures Live's first data row, which is the "Exceptions and delays" list (`[data-feed-row]`), not the jobs table. Live's layout puts the map on the left and two stacked panels on the right, with exceptions above active jobs, so the exceptions row is the first data row on the page. The autonomy chip is a link only for admins (Settings → Agents is admin-gated); for others it is text. Truck avatars use the distinguishing number (`QA-TRK-104` → "04").

- [x] 2.7 (M) Orders list and bulk actions.
  `OrdersPage.tsx`: one toolbar (search, `Filters · n` popover holding call type, channel, customer, driver, product, date presets), status `FilterChips` with counts, `DataTable` with selection, `ProductChip`, `format` dates, and bulk Confirm and Hold calling the existing per-order status endpoint sequentially with a per-row result toast. Remove the page's duplicate search field in favour of the toolbar search. Drop the page's "Create Order" action (the top bar's "New order" is the one create action; Phase 1 review finding 8). `CreateOrderModal` moves to `FormDialog`.
  Accept: R10.2; R5.4 (no raw codes); first row ≤ 172 px.
  Verify: `npx jest src/components/ops/OrdersPage.test.tsx src/components/ops/OrderDetailPage.test.tsx`; e2e.
  Done. Chip counts use one `size: 1` list read per status (no aggregate endpoint). Bulk Hold asks one reason in a small `FormDialog`. Order detail not-found renders an `<h1>` (Phase 1 review issue 3). Review fix (iteration 2): status chips that don't fit collapse into "More · n" (selected chip always shown) instead of clipping (R4.3).

## Phase 3: remaining staff modules (≈ 12–15 days)

Each task: hub uses the compact `PageHeader` with inline tabs; inner pages drop their own headers; summary bars become chip counts; every create/edit flow in the module moves to `FormDialog` per the `design.md` §5 table; numbers and dates go through `format`/`NumberField`; the guard baseline in `format.guard.test.ts` is lowered; chrome and axe specs for the module flip from `test.fail` to passing.

- [x] 3.1 (L) Fleet: Trucks, Drivers, Inventory. Merge `DriversHub` into Fleet (`?tab=drivers`); remove the Orders tab from `FleetDashboard.tsx:40`; `ExpiryAlertWidget` becomes a header count chip linking to Compliance; tracking table default filter "All"; compartments and driver forms → `FormDialog` (fix `TruckCompartmentsPage.tsx:1100` float gallons). Fix OI-29 in `DriverActivitySection.tsx` while there (AbortController; cap pages at 1000/PAGE_SIZE).
  Verify: `npx jest src/components/FleetTracking.test.tsx src/components/ops/TruckCompartmentsPage.test.tsx src/components/drivers src/components/ops/DriverActivitySection.test.tsx`; e2e.
  Done (Phase 3 iteration 2, see `../phase3-evidence.md`). Trucks: DataTable with status chips (default All) and an asset-type Filters popover, StatusBadge for status and compliance, Compartments in a Drawer from the row menu, "Add asset" moved onto a FormDialog; below 1536 px the table is full width with the map underneath. The expiry card is one title-row chip linking to Compliance → Certifications (hidden when everything is current). Compartments: configure (lg FormDialog with rows, whole-gallon NumberField, product chips instead of comma codes, untouched capacities sent back exact), cleaning event (md FormDialog) and the eligibility check (Modal with ProductSelect). Drivers: utilization on DataTable with header sort and duty chips (all drivers read once, chips filter on the client so counts stay right); qualifications on DataTable + Drawer + sectioned lg FormDialog. The separate "DQF Dashboard" view is relegated: its counts are the chips and its badges are the Alerts column. Inventory: create, edit and adjust on FormDialog, delete on a confirm Modal, history in a Drawer. OI-29: the superseded driver-activity read is aborted (page cap was already in place). Expiry dates use the new `format.calendarDate` (date-only strings no longer shift a day west of UTC).

- [x] 3.2 (L) Fuel and the station dialog bugs. `FuelOpsPage`, `FuelDashboardView`, `SourcingPage`, `KFactorCalibrationPage`. `FuelStationForm` → `FormDialog` with `NumberField` (capacity, initial stock: whole gallons; threshold: %) and `ProductSelect`; delete the `CODE (Name)` labels (`FuelStationForm.tsx:41-49`). The same fix applies to `CustomerTankPage.tsx:1032` and `SourcingPage.tsx:283`.
  Accept: editing a station with capacity 20,000 L shows `5,283 gal`; Fuel Type shows the chip and "Regular unleaded" with "GASOLINE_REG" as secondary text and no truncation.
  Verify: `npx jest src/components/ops/FuelStationForm.test.tsx src/components/ops/SourcingPage.test.tsx src/components/ops/CustomerTankPage.test.tsx` (`FuelStationForm.test.tsx` is new: cover the 20,000 L → 5,283 gal display, locale parsing and the product labels); e2e.
  Done (Phase 3 iteration 1, branch `production-readiness/ui-phase3`, see `../phase3-evidence.md`). `FuelSummaryBar` is deleted: network stock, alerts and average days left are title-row counts, and station status counts are the filter chips. Station detail opens in a `Drawer` and the list honours `?station=` (Phase 2 follow-up). The Sourcing query form became a `FormDialog` opened from "Rank terminals"; the toolbar shows the current query and result summary. K-factor approval is a `FormDialog`; its summary cards became chips. Editing keeps stored fractional volumes unless the user changes them (station capacity and tank capacity/level are only sent when changed).

- [x] 3.3 (M) Customers: list, detail, tanks, Communications. Add a `?tab=communications` tab hosting `NotificationHistoryTab`; tank detail under `/dashboard/customers/tanks/[id]`; customer create and tank → `FormDialog`; `CustomerTankDetailPage.tsx:176` shows the product name.
  Verify: `npx jest src/components/commerce src/components/ops/CustomerTankPage.test.tsx`; e2e.
  Done (iteration 2). Customers list: DataTable, status chips with counts, "New customer" FormDialog (the list had no create flow before; `POST /commerce/customers` already existed), tanks in a Drawer from the row menu. Customer and tank detail: compact title row with back, StatusBadge, `lib/format` money/dates/gallons; the tank shows the product chip with name and code (no raw `fuel_type`). Communications: one toolbar (status chips with the summary counts, type/channel popover), DataTable, detail Drawer with Retry.

- [ ] 3.4 (L) Billing. `CommerceHub` with inline tabs (no nested `PageHeader` in `InvoicesListPage`, `AccountsListPage`, `PaymentsListPage`, `ARAgingDashboard`, `ReconciliationPage`, `PriceBookEditor`, margin pages); detail routes from 1.10; price book → `lg` sectioned `FormDialog`; margin and cost-entry codes → `ProductChip` (`CostEntriesPage.tsx:164`, `MarginRecordsPage.tsx:316`).
  Verify: `npx jest src/components/commerce src/components/ops/ReconciliationPage.test.tsx`; e2e.

- [ ] 3.5 (L) Compliance. Tabs Certifications, Meters, BOLs, IFTA, plus terminals detail; inline forms in `AssetCertificationsPage`, `ExemptionsPage` (moves to Settings → Company), `PriceProtectionContractsPage`, `PricingRulesPage`, `TaxJurisdictionsPage` (moves to Settings → Company) → `FormDialog`.
  Verify: `npx jest src/components/compliance`; e2e.

- [ ] 3.6 (M) Copilot confirmation parity, then retire `/ops/command`. Port the console's inline medium-risk confirmation (`app/ops/command/(console)/page.tsx`) into `AIChat.tsx`, with tests. Then delete `app/ops/command/**`, add redirect row 5, and remove `AgentToast`/`ReportViewer` if they have no other importer.
  Accept: R3.5.
  Verify: `npx jest src/components/AIChat.test.tsx` (new test file: confirmation shown for a medium-risk action, confirm and cancel paths); redirects e2e.

- [x] 3.7 (M) Analytics. Remove the Ops Monitoring tab; hide the range selector (OI-50, `Analytics.tsx:111-139, 275`); charts use `CHART` tokens; compact header.
  Verify: `npx jest src/components/Analytics.test.tsx src/components/AnalyticsHub.test.tsx` (`AnalyticsHub.test.tsx` is new: no Ops Monitoring tab, no range selector); e2e.
  Done (iteration 1). Ops Monitoring is gone for every role, platform_admin included. The overview's own hero header and the non-functional "Export Report" button are removed (no handler existed). Analytics is not a list page (not in design.md §6), so chrome.spec checks its content starts right under the title row.

- [ ] 3.8 (L) Settings. Finish the `/dashboard/settings` sections (task 1.10 shell): Company (Depots with detail route, Road restrictions, Weather alerts, Tax jurisdictions, Exemptions), Notifications (rules, templates → `FormDialog`), Integrations (marketplace, intake channels, Stripe), Agents (settings, monitoring), Import, Flags, Metrics. Depot and flag forms → `FormDialog`.
  Verify: `npx jest src/components/admin src/app/dashboard/settings` (add `NotificationSettingsTab.test.tsx`, new, for the template FormDialog); e2e.

- [x] 3.9 (S) System health (platform_admin). After 0.2: `SystemHealthPanel` in Settings → System health showing poison-queue metrics with `LoadErrorState` and retry; delete `OpsMonitoringDashboard.tsx` and the ingestion and indexing client functions in `services/opsApi.ts`; remove `getPrometheusMetrics` and the feature-flag client functions if still unused (`rg`).
  Verify: `npx jest src/components/admin/SystemHealthPanel.test.tsx`; e2e: platform-admin-only visibility (fixture roles).
  Done (iteration 1). Section `?tab=system` (alias `system-health`), group "Platform". `OpsMonitoringDashboard.tsx` and its test are deleted; `getPrometheusMetrics` and the three ops feature-flag client functions had no caller and are deleted (backend routes unchanged). New `e2e/ui-revamp/systemHealth.spec.ts`.

- [ ] 3.10 (S) Dead code and guard to zero. Also owns the two design §5 rows Phase 2 left (review finding 3; Phase 2 merged, so the orchestrator lifted the Phase 2 file restriction for them): `ops/CreateJobModal.tsx` and `orders/OrderDetailView.tsx` status/hold/cancel/assign. Both migrated in iteration 2. Delete unimported files found by a fresh import scan (`AdminHub.tsx:141` dead case; `WebSocketStatus.tsx` if unimported); lower `format.guard.test.ts` baseline to 0; remove all `test.fail` marks in `e2e/ui-revamp`.
  Verify: web standard commands; full `e2e/ui-revamp` run green; board e2e green.

- [ ] 3.11 (M) Fold the portal's local substitutes into the shared components (deferred from 3P.1, 3P.2, 3P.6, 3P.8; Phase 3P wasn't allowed to edit shared components, see `phase3p-evidence.md` "Deviations"). Each item removes a portal-local copy once the shared piece exists:
  - `open`/`partial` into `design/tokens.json` + `tokens.test.ts`; `icon` prop (`FileText`, `CircleDollarSign`) on `StatusBadge`; then drop `PORTAL_EXTRA_STATUS` and `PortalBadge` from `components/portal/portalStatusMap.ts`/`PortalStatus.tsx`, and merge `components/portal/__tests__/portalTokens.test.ts` into `tokens.test.ts` (3P.1).
  - `hour12`, `zoneName`, `days()` in `lib/format.ts` with no-change tests; delete `components/portal/portalFormat.ts` (3P.2).
  - `FormDialog`/`Modal` `mobile="sheet"`, an `aria-disabled` submit and keep-open-on-success; move `RequestDeliveryDialog` onto `FormDialog` (3P.6).
  - `DataTable` `stickyTop` (sticky header without the `overflow-x-auto` wrapper breaking it) and 48 px rows; replace `PortalTable` (3P.7, 3P.8, 3P.10).
  - 44 px phone-size variants of `Menu`, `FilterChips`, `EmptyState`, `InlineBanner` (R14.19); replace the portal menu, chips, `PortalEmpty`, `PortalBanner` (3P.3, 3P.8).
  Verify: web standard commands; portal unit suite and `e2e/portal` fixture suite green with unchanged baselines.

## Phase 3P: customer portal (≈ 11.5 days)

Runs after Phase 1 and alongside Phase 2, on its own branch `ui-revamp/p3p-portal` off go-live-blockers. It touches only the files in `design.md` §11.10: portal files plus the shared tokens, `StatusBadge`, `FormDialog`/`Modal`, `lib/format`, sign-in and the `/auth/*` password pages. No backend change and no change to portal behaviour or security (R14 constraints); escalations PE1–PE7 (`audit.md` §j-e) are not in this phase.

Portal verification commands, in addition to the web standard commands:

- Unit: `cd runsheet && npx jest src/components/portal src/app/portal src/app/signin src/app/auth src/components/ui src/lib src/styles`
- E2E with fixtures (no backend): `cd runsheet && npx playwright test e2e/portal/visual.spec.ts e2e/portal/chrome.spec.ts --project=chromium`
- E2E on staging (read-only, QA portal user per `audit.md` §j-f, deleted afterwards): `PLAYWRIGHT_BASE_URL=https://app.staging.runsheetops.com npx playwright test e2e/portal --project=chromium`

Each task: the T-UI-SHELL test passes; existing portal behaviour tests keep their assertions (only selectors may change); axe 0 critical/serious on the pages it touches at 390×844 and 1280×800.

- [x] 3P.1 (S) Status tokens and the portal status map.
  Add `open` and `partial` to `design/tokens.json` (`design.md` §11.4), regenerate, add `FileText` and `CircleDollarSign` to `StatusBadge`'s icon map, extend `tokens.test.ts` with the portal co-occurrence set and the two declared pairs. Add `components/portal/portalStatus.ts` and `PortalStatus.tsx`; replace every `StatusText` use; delete `StatusText.tsx`.
  Accept: R14.1, R14.2; requirements AC 1 and AC 2 (status part).
  Verify: `npx jest src/styles/tokens.test.ts src/components/ui/components.test.tsx src/components/portal`; `node scripts/build-tokens.mjs --check`.

- [x] 3P.2 (S) Formatting.
  `lib/format.ts`: `hour12`, `zoneName`, `days()` (§11.3), with tests that existing outputs don't change. `PortalGate` configures format (§11.7). Replace every `components/portal/format.ts` import with `lib/format`; delete the file. Add the portal grep test (no `toFixed(`/`toLocale`, no rendered `status_code`/`product_code`) under `src/components/portal/__tests__/guard.test.ts`.
  Accept: R14.4; AC 2.
  Verify: `npx jest src/lib src/components/portal`.

- [x] 3P.3 (M) Shell: top bar, bottom tab bar, account menu, title row.
  Rewrite `PortalShell`; add `PortalTopBar`, `PortalTabBar`, `PortalAccountMenu`, `PortalTitleRow` (§11.1). Every portal page uses `PortalTitleRow` and marks `data-portal-first`. Extend T-UI-SHELL's forbidden list with `components/shell/*`.
  Accept: R14.5, R14.6, R14.19; AC 4, AC 5, AC 16.
  Verify: `npx jest src/app/portal/layout.test.tsx src/components/portal`; new `e2e/portal/chrome.spec.ts` (first content ≤ 120 px at 390×844, ≤ 136 px at 1280×800; tab bar present and items ≥ 44 px at 390, absent at 1280).

- [x] 3P.5 (L) Home.
  `app/portal/page.tsx` per §11.2: `TanksPanel` (`TankRow`, `LevelBar`, `tankLevel.ts`, `tankTitle.ts`, sort order), `BalancePanel` (same data calls as today, "At least" wording kept until PE4), `ActiveOrdersPanel`; capability messages inside their panels; `Skeleton`, `EmptyState` and `PortalSectionError` per panel.
  Accept: R14.7, R14.8, R14.15, R14.16; AC 6, AC 12; first content ≤ 120/136 px.
  Verify: `npx jest src/app/portal/page.test.tsx` (new: ranking, 12 % tank "Order now", meter `aria-valuetext`, one customer-name occurrence, each capability message in its panel, per-panel error with the others rendered) and `src/components/portal/__tests__/tankLevel.test.ts`, `tankTitle.test.ts`; visual and axe for Home in every §11.8 state.

- [x] 3P.6 (L) Request delivery dialog.
  `FormDialog`/`Modal` `mobile="sheet"` (shared, with tests). `RequestDeliveryDialog`, `TankPicker`, `useOrderRequest.ts` (logic moved out of `OrderRequestForm.tsx`, which is deleted after its tests move). Triggers on Home, Orders and `TankRow`; `/portal/orders/new?tank=` opens the dialog over Orders (§11.6). Unavailable state per R14.12.
  Accept: R14.11, R14.12; AC 7, AC 8 (order tests), AC 9.
  Verify: `npx jest src/components/portal/__tests__/RequestDeliveryDialog.test.tsx src/components/ui/FormDialog.test.tsx src/components/ui/Modal.test.tsx` (port every case of `OrderRequestForm.test.tsx`; add deep link, preselect, focus return, sheet at 390 via `matchMedia` mock); `e2e/portal/keyboard-flow.spec.ts` (updated selectors) on staging with the flag procedure from the customer-portal runbook, or with fixtures when the flag can't be flipped.

- [x] 3P.7 (M) Orders list.
  `OrderRow` and `PortalTable` (§11.3); Cancel request keeps its handler, focus return and live region, with the reference text from §11.7 in the accessible name and the announcement (`messages.ts` signature change). The `usePagedList.refresh()` stuck-`loadingMore` LOW from `customer-portal/review.md` is fixed here (`setLoadingMore(false)` in `refresh()`, with a Jest case), because the list is rebuilt in this task.
  Accept: R14.9, R14.15; AC 11; ≤ 72 px rows at 390; first content ≤ 120/136 px.
  Verify: `npx jest src/app/portal/orders/page.test.tsx src/components/portal/__tests__/usePagedList.test.ts`; visual and axe for Orders.

- [x] 3P.8 (M) Invoices list and detail.
  `InvoiceRow`, `PortalTable`, `FilterChips` for status, a Dates popover (sheet below 640), CSV in the title row (desktop) or `⋯` (phone). Detail: `BalanceCard`, `PayBar`, Pay hidden while an attempt is in flight, line items with `ProductChip`, PDF as secondary.
  Accept: R14.9, R14.10, R14.13; AC 10.
  Verify: `npx jest src/components/portal/__tests__/InvoiceTable.test.tsx src/app/portal/invoices` (update existing; add the pending-attempt and sticky-bar cases); visual and axe for Invoices and invoice detail in every §11.8 state.

- [x] 3P.9 (S) Pay page restyle.
  Tokens, `PortalTitleRow`, the amount as `NumberField` (unit `$`, decimals 2, min 1.00, max remaining). `PaymentForm` logic, Stripe loading, idempotency and polling unchanged.
  Accept: R14.14.
  Verify: `npx jest src/components/portal/__tests__/PaymentForm.test.tsx` passes unchanged except selectors; visual and axe for the pay page (render only, Stripe script blocked in the fixture run).

- [x] 3P.10 (M) Tanks list and detail.
  Tanks list uses `TankRow` (2-up from 640 px); detail uses `TankSummary` + delivery history (`OrderRow`-style rows / `PortalTable`) with `ProductChip`; fix the "Tank Tank" heading; Request delivery preselects the tank.
  Accept: R14.3, R14.8, R14.15; AC 3, AC 12.
  Verify: `npx jest src/components/portal/__tests__/TankCard.test.tsx` (renamed `TankRow.test.tsx`) and `src/app/portal/tanks`; visual and axe for Tanks and tank detail.

Phase 3P size: 11 tasks, 3 S (1.5 days) + 6 M (6 days) + 2 L (4 days) ≈ 11.5 dev-days. It can start as soon as Phase 1 is on go-live-blockers (it is) and doesn't wait for Phase 2.

### Phase 3P release gates (post-deploy, run by the release step)

Moved by orchestrator decision 2026-10-08: these can only run after deploy, which happens after approval. The release step runs them on staging and rolls back if they fail.

- [ ] 3P.4 (M) Sign-in and set-password, including V1. **Verified in release step (post-deploy)** by orchestrator decision: code, unit and fixture keyboard tests are done; only the staging run of the three `e2e/portal` specs remains.
  `components/SignIn.tsx`: real `<form onSubmit>`, "Forgot password?" below the password field, audience-neutral copy, token colours with ≥ 4.5:1 text, ≥ 24 px targets (D33). `app/auth/reset-password` and `forgot-password`: tokens, "Set your password", rules shown up front, `aria-describedby` on errors; SuperTokens calls unchanged. Fix `e2e/portal/portal-env.ts` `signInWithKeyboard` to Tab until `#password` has focus (max 5 presses, then fail with the focused element's name).
  Accept: R14.17, R14.18; AC 13, AC 14 (`/signin`), AC 17.
  Verify: `npx jest src/app/signin src/app/auth src/components/SignIn`; a Playwright keyboard test (Email + one Tab → Password focused); axe on `/signin` and `/auth/reset-password?token=x` at both widths; the three existing `e2e/portal` specs pass on staging without the temporary extra-Tab copy used in the portal verify step.

- [ ] 3P.11 (M) Portal visual, axe and contrast pass. **Verified in release step (post-deploy)** by orchestrator decision: fixture visual/axe/chrome suite and baselines are done; only the staging QA-customer walk remains.
  `e2e/portal/visual.spec.ts` and `fixtures/` (§11.8 matrix, GET-only routing, 418 for any write); `toHaveScreenshot` baselines at 390×844 and 1280×800 committed under `e2e/portal/__screenshots__/` (per-platform, as in Phase 1); axe in the same loop; contrast check of the three level-bar fills on slate-200 in `tokens.test.ts`; reflow at 320 (existing spec). Then the staging read-only walk with a QA customer created and deleted per `audit.md` §j-f, and an evidence file `portal-phase3p-evidence.md` with the before/after chrome table.
  Accept: R14.20; AC 14, AC 15; every §11.8 state has a baseline; no axe critical/serious; chrome budget met.
  Verify: `npx playwright test e2e/portal --project=chromium` (fixtures) green; staging run green; QA fixtures deleted and verified (dry run → delete → verify).

## Phase 4: driver app (≈ 6–8 days)

- [x] 4.1 (S) Tokens and brand. First run `node scripts/build-tokens.mjs --with-driver` and add the `/* tokens:start */`/`/* tokens:end */` markers (deferred from 1.1). Use the generated `global.css` block and `lib/tokens.ts`; the tab bar tint comes from tokens; delete `constants/Colors.ts` after updating `app/(tabs)/_layout.tsx`; night theme via `.dark:root`.
  Verify: driver standard commands; Expo web screenshot of Work in light and dark (`page.emulateMedia({colorScheme:"dark"})`).

- [x] 4.2 (M) Single headers and sizes. Remove duplicate in-content titles (`order/[orderId].tsx`, `pod.tsx:260`, `exception.tsx:101`, `inspection/new.tsx:236`); `ui/input.tsx` height 48; back button hit area 44; radios get `accessibilityState.checked` (`profile.tsx:302-316`, `inspection/new.tsx:252-268`, `exception.tsx`); warning text uses an exception badge and `red.800`; dates via a shared `lib/format.ts` port (same rules as web).
  Verify: RNTL tests for each screen asserting one title and `checked` state; Expo web tap-target check (all interactive ≥ 44×44).

- [x] 4.3 (L) Work screen redesign. Duty chip (move from Profile, same mutations), next-stop card with `ProductChip` caps, Navigate (`Linking.openURL` with `maps:`/`geo:`/Google Maps URL per platform), Call (`tel:`), Arrive (existing check-in), compact remaining stops; DVIR prompt on going on duty. Messages tab label "Dispatch". Profile loses duty/HOS.
  Accept: R13.3, R13.4, R13.8; journey tap counts per `audit.md` §(i-e).
  Verify: RNTL tests for Work (next stop, duty chip, Navigate URL per platform with `Platform.OS` mocked); driver standard commands.

- [x] 4.4 (M) POD and Route. POD gallons prefilled with planned gallons (editable, whole numbers, `decimal-pad`), capture buttons in two columns; Route stop chips ≥ 44 pt and product caps.
  Verify: RNTL tests for POD prefill and Route chips.

- [x] 4.5 (S) Template clean-up. Delete `components/HelloWave.tsx`, `ParallaxScrollView.tsx`, `Collapsible.tsx`, `ExternalLink.tsx`, `DispatchOrderCard.tsx` after `rg` confirms no importer (`.ios.tsx` platform files are not dead).
  Verify: driver standard commands.

- [x] 4.6 (M) Driver visual and a11y pass (Expo web). Playwright script against `EXPO_PUBLIC_DEMO_MODE=true npx expo start --web --port 8099` (stop the server afterwards): 9 screens at 390×844, light and dark, axe 0 critical/serious (excluding `document-title`, which is web-only), tap-target check. (The device pass moved to 4.7 by owner decision, 2026-10-08.)
  Verify: script output and screenshots committed under `driver-app/e2e-web/__screenshots__/`.
  Done (Phase 4, branch `production-readiness/ui-phase4`, commits `7d223a1`, `75ed8f7`, re-run on `d10b314`): 10 screens (the 9 plus the duty sheet) × light/dark, 0 critical/serious, 0 undersized targets, 0 inputs under 48 px. Deviations for 4.1–4.5 are listed in `../phase4-evidence.md` (night destructive token, device-local pre-trip marker, Arrive → Route check-in, post-trip offered not forced).

- [ ] 4.7 (S, owner) Manual device pass on iOS and Android with a staging driver account (owner). Owner-gated by owner decision (2026-10-08): it does not block Phase 4 review approval, and agents leave it unticked.
  Build: an EAS build from `production-readiness/ui-phase4` (or its merge), `eas build --profile development` (dev client, `expo-dev-client`) or `--profile preview` (internal distribution), one each for iOS and Android. Expo Go does not work: `react-native-mmkv`, the background location task (`expo-task-manager`) and the signature WebView need native modules. Set `EXPO_PUBLIC_API_BASE_URL` to the staging API, leave `EXPO_PUBLIC_DEMO_MODE` unset, and sign in with a staging driver account in `demo-tenant` that has `QA-` assigned orders. iOS needs a registered device (ad hoc) or TestFlight.
  Check, and record each as pass or fail with device and OS:
  - Navigate opens Apple Maps (`maps:`) on iOS and the maps app (`geo:`) on Android with the stop address. Call opens the dialler (`tel:`) with the customer number.
  - Camera, signature and photo capture: POD delivery photo and meter ticket open the camera and save. The signature pad draws and clears, and its strokes are kept on submit. DVIR photos attach.
  - Safe areas and notch: the Work "Today" header, the tab bar and the bottom actions clear the notch, Dynamic Island and home indicator, in portrait on both platforms.
  - Night theme and `.dark:root`: switching the system appearance re-themes every screen live (brand green, no white flashes). Text stays legible, and the contamination warning shows red.300 at night.
  - Hermes `Intl`: times are 24 h with no seconds ("08:30"), dates read like "Thu 8 Oct", windows read "08:30–10:30", and no screen shows "Invalid Date".
  - Gloves: 48 pt inputs and every button, radio and chip (≥ 44 pt) can be hit first time with work gloves, including the duty chip and the Navigate/Call/Arrive buttons.
  - Sunlight: at full brightness outdoors, Work, delivery and POD are readable in the day theme, and the product caps can be told apart.
  - Offline queue and chip: in airplane mode, a check-in or POD queues and the queue chip shows the outstanding count. Back online, it drains and the chip clears.
  Record results in `../phase4-evidence.md` under "Device pass".

## Definition of done (whole revamp)

- Every page in `design.md` §6 meets the 172 px budget at 1280×800 and 1440×900 (board also at 1024×768).
- `e2e/ui-revamp` axe: 0 critical, 0 serious on every shell page.
- Token contrast and CVD tests pass; colour is never the only signal.
- Every create/edit flow in `design.md` §5 uses `FormDialog`.
- All 23 redirects pass; no capability is unreachable (checked against the "Function now lives" column).
- The board e2e suite and N1 budgets are green; the driver app passes its tests and the Expo web pass.
- Customer portal: R14 acceptance criteria 1–17 pass; `e2e/portal` (fixtures and staging) green; the portal meets its own chrome budget (≤ 120 px at 390×844, ≤ 136 px at 1280×800).
