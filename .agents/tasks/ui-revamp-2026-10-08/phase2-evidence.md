# UI revamp Phase 2 evidence

Branch `production-readiness/ui-phase2` in `.worktrees/ui-phase2`, created from `origin/production-readiness/go-live-blockers` @ `3c5dc4a` (Phase 1, live as ui:25). Code commit `d48decb`. Not pushed, not merged, not deployed (none of that was in scope for this step).

Iteration 1 (no `phase2-review.json` yet). Tasks 2.1–2.7 are ticked in `spec/tasks.md`, each with a short "Done" note and its deviations. Only Phase 2 checkboxes and notes were changed. The Phase 3P sections were not touched.

## What changed, by task

| Task | Result |
|---|---|
| 2.1 Dashboard | New `components/dashboard/` (`Dashboard.tsx`, `dashboardModel.ts`, `OrderAttentionRow.tsx`). The title row has h1 "Dashboard", a Today/Tomorrow toggle, and counts for loads, trucks out, delayed, exceptions and approvals. Each count is a link. **Needs attention** ranks exceptions, delays, approvals (inline Approve and a Review link to `/dashboard/control?tab=approvals&id=`), placed and on-hold orders (Assign on board / Release), and low tanks (Create order prefilled with product and refill gallons; the row opens the station). **Today's runs** shows one row per lane: identity stripe, progress bar by stop status, StatusBadge, link to the lane `?truck=`. **Plan status** shows draft and published plans for today and tomorrow, linking to the board day. Without the board, runs come from active jobs and plans from the plan list. Each widget loads with `Promise.allSettled` and has its own skeleton and `LoadErrorState`. `DispatchCockpit` is deleted and its tests are ported. |
| 2.2 Board chrome | `BoardToolbar.tsx` is split. `BoardDayControls` and `BoardPrimaryActions` go into the Dispatch title row through `usePageChrome()` (memoised). Standalone they render as their own row (`BoardTitleControls`). `BoardToolbarRow` is one 44 px row: search, a Filters popover, the View menu (Timeline/Sequence, Comfortable/Compact as radio items, same `viewState.ts` keys), Map, an "n suggestions" chip, undo/redo, and `⋯` (Suggestions, Keyboard shortcuts, Truck history). Place mode replaces the row's contents. The shadow / past-day notice is a chip in the row. `useBoardController`, the reducer, the DnD adapter, the API and keyboard handling have no diff. |
| 2.3 Board colour | Lane identity stripe and truck avatar (`assignLaneIdentities` in `BoardGrid`). Driver chip with avatar and initials + surname, full name as tooltip. Qualification chips (Tanker ✓, Hazmat ✓, expiry within 30 days). "No driver" is a red chip with an icon. Lane state is a `StatusBadge`. Loads and stops use the status bg/border/fg with an icon and a label; draft loads are dashed. Stops show a product cap and the readable name. Ghost loads are fuchsia dashed. Order cards show a product cap, name and `StatusBadge`. The gauge fills each compartment in its RP 1637 colour with symbol + % on a white label. CheckChip and presence use tokens. |
| 2.4 Jobs | New `components/dispatch/JobsView.tsx`, which has no header. Create job goes in the title row. One toolbar: search, status chips with counts (replacing `JobSummaryBar`, now deleted), a Filters popover (type, truck, dates), Export CSV, and Cargo search in `⋯` (opens in a drawer). `JobBoard` is a `DataTable` with a row menu for the valid transitions and a Fail-reason `FormDialog`, and no row tint. `?status=` picks the chip. The `/dashboard/dispatch/jobs/[id]` route no longer calls the transition API a second time and swallows its error. Failures toast from the detail view. |
| 2.5 Plans | `FuelDistributionPage` has no `PageHeader` and no `TabNavigation`. Each sub-view (`?sub=plans\|forecasts\|priorities\|clusters`) has one toolbar row holding the segmented switch plus that view's controls. The plan list is a `DataTable`. `RejectDialog` and `ReplanForm` are `FormDialog`s. A failed reject keeps the dialog open with the message. |
| 2.6 Live | New `components/live/LiveView.tsx` (`/dashboard/control`). Tabs: Overview · Approvals · Agents (`?tab=`). Counts in the title row. `AutonomyChip` links to Settings → Agents. The default-depot and inventory warnings are chips. Overview: `OperationsMap` (Google map with identity-coloured markers, and a truck list with avatars and status badges) on the left, with no lat/lon grid; exceptions and delays plus active jobs on the right. Approvals uses `ApprovalQueue` with `focusId` (`?id=`). The 9 superseded panels (`OperationsControlView`/`Page`, `OperationsSummaryBar`, `JobQueuePanel`, `DelayedOperationsPanel`, `FuelStatusSidebar`, `ApprovalQueuePanel`, `AgentAutonomyBanner`, `InventoryHealthBadge`) are deleted. None had another importer. |
| 2.7 Orders | One toolbar: search (the page's only search), status chips with counts, "Awaiting confirmation", and a Filters popover (call type, channel, customer, driver, product, date preset Today / Tomorrow / This week / Custom). `DataTable` with selection, `ProductChip`, and formatted gallons, window and created date. Bulk **Confirm** (`PATCH /orders/:id/status`) and **Hold** (`POST /orders/:id/hold`, one reason in a FormDialog) run one order at a time and report per-row results in a toast. Failed rows stay selected. The page no longer has a Create button. `CreateOrderModal` is a sectioned md `FormDialog` with `ProductSelect` and `NumberField`, and the shell can prefill it. `?status=` seeds the chip. |
| Phase 1 follow-ups | Fixtures: plans, assets, fuel alerts, approvals and autonomy were added to the e2e fake, so every Phase 2 page measures a real row. `fixtures.ts` doc corrected. Heading-less not-found states: job and order detail now render an `<h1>` ("Job not found", "Order not found"), and redirect rows 2 and 16 assert it again. Linux baselines: **not done**, see "Not verified". |

Shared component changes, all backward compatible:
- `usePageChrome` republishes only when a node changes.
- `LoadErrorState.onBack` is optional, for in-widget use.
- New `ui/FilterPopover`.
- `DefaultDepotWarning` has `variant="chip"`.
- `JobFilters` has `hideStatus`.

No backend files changed, so the backend gate was not needed. No `/portal` files changed.

## Verification

All commands ran from `.worktrees/ui-phase2/runsheet` unless noted. `node_modules` is symlinked from `.worktrees/merge-board`, which has an identical lockfile. Docker was not used.

### Type check, lint, unit, build

| Check | Result |
|---|---|
| `npx tsc --noEmit` | clean |
| `npm run lint` (biome) | 0 errors, 1 warning (pre-existing `noConfusingVoidType`, `DispatchBoard.tsx:327`, in an unchanged line) |
| `npx jest --ci` | 136 suites, 1788 passed, 1 skipped |
| `node scripts/build-tokens.mjs --check` | up to date |
| `npm run build` | exit 0 |
| Fresh checkout | `git worktree add --detach … d48decb`. `tsc` clean, `next build` exit 0, and jest for `dashboard`, `live`, `dispatch*` and `lib` (28 suites, 351 passed). The new `src/components/{dashboard,live}` and `src/lib` are in the commit. Temp worktree removed. |

The machine was shared with other agents' Playwright and VM runs (load average 7–10). Under full parallel jest, a few board tests timed out (5–13 s per test), on different tests each run. Every one passes when its file runs alone, and an unchanged `dragWiring` suite timed out the same way. One `next build` was killed by memory pressure (exit 137) and passed on rerun.

New and updated tests:
- New: `dashboard/Dashboard.test.tsx` (17), `live/LiveView.test.tsx` (6), `dispatch/JobsView.test.tsx` (6), `dispatch-board/chrome.test.tsx` (12).
- Extended: `JobBoard.test.tsx` (4), `OrdersPage.test.tsx` (4 new), `FuelDistributionPage.test.tsx` (2 new).
- Updated for the moved controls (View menu, `⋯`, chips, Filters popover): DispatchBoard, keyboard, responsive, grid, publish, suggestions, CreateOrderModal, OrderDetailPage, DispatchPage and the JobsView export test.

### UI revamp e2e

Command: `npx playwright test -c playwright.ui-revamp.config.ts --project=chromium` (production bundle, fake backend), with `--update-snapshots` and `UI_REVAMP_SHOTS_DIR`. Result: **128 expected, 0 unexpected, 0 flaky**.

`test.fail` is removed for Dashboard, Dispatch Board, Jobs, Plans, Orders and Live (chrome and whole-page axe). They now pass outright. The other pages are still expected failures owned by Phase 3.

First data row (`firstRowTop`, px from the viewport top, budget 172):

| Page | 1024×768 | 1280×800 | 1440×900 | Phase 1 |
|---|---|---|---|---|
| Dashboard (`[data-feed-row]`) | — | 145 | 145 | 212 |
| Dispatch Board (first lane) | 168 | 168 | 168 | 227 |
| Dispatch → Jobs | — | 172 | 172 | 380 |
| Dispatch → Plans | — | 172 | 172 | no row |
| Orders | — | 172 | 172 | 339 |
| Live | — | 145 | 145 | no row |

The 172 px pages are the exact sum of the chrome: 48 px top bar + 44 px title row + 44 px toolbar + 36 px table header.

Axe (wcag2a/2aa/21aa/22aa) on the whole page, 1440×900: 0 critical and 0 serious on all six Phase 2 pages, and on the shell of all 18 pages. One finding was fixed during the run: a `target-size` issue on Live's job links (min-h-6).

Other suites:
- Redirects: all 23 rows plus `/ops` pass. Rows 2 and 16 assert the not-found `<h1>` again, and row 0 asserts "Dashboard".
- URL tabs pass. The lookup is now scoped to the title row, because Plans has its own "Plans" sub-tab.
- Visual: 22 baselines were re-recorded on purpose. Dashboard, board and orders were redesigned. The new ones are jobs, plans, live and board-1024.

### Dispatch board e2e (`PW_BOARD_PROD=1 npx playwright test -c playwright.dispatch-board.config.ts --project=chromium`)

**17 passed, 2 skipped** (iPad-webkit only, by design). That is the full-suite run plus a rerun of the aria-snapshot test after its snapshot text was fixed.

N1/K15 budget at 60 lanes / 1,500 stops, production bundle:

| Run | median | p95 (≤ 20) | hover p95 | scroll p95 |
|---|---|---|---|---|
| Phase 2 first try | 16.7 | **33.6 ✗** | 32.5 | 34.1 |
| Baseline `3c5dc4a`, same load | 16.7 | 17.6 | 17.6 | 17.6 |
| Phase 2 after memoising card faces | 16.7 | **17.6 ✓** | 17.6 | 17.7 |
| Phase 2 full-suite run | 16.7 | **17.7 ✓** | 17.6 | 33.0 |

The first try regressed because every stop card re-renders on each drag frame (they read the board context), and the new icons and caps made each render heavier. The fix: `StopFace` and `OrderFace` are memoised on plain values.

The full-suite run's scroll p95 of 33.0 is not asserted. The isolated perf run under the same load measured 17.7, so it is probably load noise. The reviewer may want to rerun it on an idle machine.

Presentation-coupled assertions updated in `e2e/dispatch-board.spec.ts`:
- "Publish all ready" became `/^Publish (all|\d+) ready$/`.
- The Timeline disabled check now opens the View menu.
- The `group "Board"` aria snapshot now lists the toolbar row, plus a separate `group "Day"` snapshot.

The drag, publish, conflict, keyboard, target-size, axe and perf tests are unchanged.

### Token contrast and colour vision

- `styles/tokens.test.ts` (contrast ≥ 4.5:1 text / 3:1 UI, CVD ΔE ≥ 4 under deuteranopia/protanopia) passes in the jest run. Tokens are unchanged.
- The new pairs (`*-800` on `*-50`/`*-100`, slate-700 on status 100 backgrounds, white initials on identity colours) are all from the token ramps. Axe `color-contrast` reports 0 on all six pages.
- Colour is never the only signal. Every coloured element carries an icon, a label or both, asserted in `chrome.test.tsx`, `Dashboard.test.tsx` and `JobBoard.test.tsx`:
  - status: icon + label
  - identity: truck name / initials
  - product: symbol + name
  - gauge: symbol + %

### Screenshots (`screenshots/phase2/`)

`dashboard`, `dispatch-board` (1024×768, 1280×800, 1440×900), `dispatch-jobs`, `dispatch-plans`, `live`, `orders`, `billing-invoices`, `settings`, each at 1280×800 and 1440×900.

Compared with the mockups:
- Dashboard matches `mockup-dashboard.html`: title-row toggle and counts, Needs attention with chips and inline actions, runs with stripes and bars, Plan status. The run list is capped at 8 with "All on the board".
- The board matches `mockup-dispatch-board.html`: one title row, one toolbar, lane stripes and avatars, coloured loads and stops, product caps.

Differences, all from the fake data:
- Every lane is "No driver" / Draft and the product code is unknown (`ULSD` → "?" cap).
- No truck types.
- At 1024 px, Generate plan is icon-only and Publish reads "Publish" (accessible names keep the full label).

## Not verified / follow-ups

- **Linux visual baselines** (Phase 1 follow-up 2): not recorded. It needs a Linux run (CI or Docker), Docker Desktop is wedged, and nothing is pushed. The CI pixel compare still skips on Linux. To do on push: `UI_REVAMP_RECORD=1 npx playwright test -c playwright.ui-revamp.config.ts visual --update-snapshots` on the CI image.
- The `ui-revamp-e2e` CI job has still not run on Linux, because the branch is not pushed.
- No staging deploy and no QA accounts were used. No flags were toggled.
- The fuel station deep link `/dashboard/fuel-ops?tab=stations&station=<id>` opens the Stations tab. The Fuel page doesn't select the station yet (Phase 3, task 3.2).
- Orders: the Customer column is narrow at 1280 px (it truncates with a tooltip).
- Stop, order-card and gauge accessible names still include the raw product code (kept for the board's screen-reader and e2e contracts). Visible text uses names.

## Housekeeping

- Cleared the npm cache, Homebrew cleanup and the pip cache (standing permission).
- Removed `.next` after each build.
- Removed the temporary `.worktrees/p2-baseline` and `.worktrees/p2-fresh` worktrees.
- Scratch files went to `Runsheet/tmp/ui-phase2` and `tmp/*.py`. Another agent deleted logs from the shared `tmp/p2` folder mid-run, so later logs went to `tmp/ui-phase2`. These are deleted at the end of this step.
