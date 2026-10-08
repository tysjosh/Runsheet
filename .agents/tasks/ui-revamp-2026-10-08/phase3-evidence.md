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
