# Phase 3 iteration 4 review: evidence and fixes closed; branch stale against go-live-blockers

This pass covers `5300be5..eb0eec5`, the fixes for all nine iteration-3 findings and the iteration-3 evidence. All nine are closed. The shared `FormDialog`/`Modal`/`DataTable`/touch-variant additions are committed with no-change tests (`sharedAdditions.test.tsx`). The Plans cluster chips show product names. Cost import is a two-step `lg` FormDialog, and the deviation from the `steps` prop is recorded. The Sourcing wait report and cargo manifest editor are on FormDialog. Copilot cards with no action id offer no decision. 3.11's re-scope is written into `tasks.md`. Every Phase 3 task is ticked. `phase3-evidence.md` now records tsc, lint, jest (168 suites, 2351 passed), a fresh-checkout build, a full green `e2e/ui-revamp` run with no `test.fail` marks, the board e2e with N1 at 17.5 ms, 172 px first rows on every Phase 3 list page at both sizes, and axe on 31 pages and 13 dialogs. The measurement work also caught six Billing entries that were silently measuring the Invoices fallback, and fixed them.

Watch for: `origin/production-readiness/go-live-blockers` moved after the last merge (`ab5f933` → `e636358`, 14 commits), and merging it into this branch now conflicts in 9 files (confirmed). Those commits are owner-facing data-correctness fixes (F2–F5, F11–F13): an AR aging Current bucket, Analytics real data and as-of, fuel "—" for non-consuming stations, and scheduling completion %. They land in components Phase 3 rewrote or deleted, including `FuelSummaryBar.tsx`, which Phase 3 deleted and go-live-blockers modified. Phase 3 can't merge in order until they're ported into the redesigned components and re-verified.

**Verdict**: NEEDS_CHANGES

## High-level view

The Phase 3 scope matches the spec. Every create/edit flow in design §5 plus the extra inline flows found in review is on `FormDialog`. The only `fixed inset-0` overlays outside `ui/` are the §5 exemptions (`ReportViewer`, `TankConsumptionDrillIn`, board sheets), the mobile sidebar and `request-pilot`. The only remaining `type="number"` is in the Phase 2 `CompartmentsTab`, logged as a follow-up. Products render by name through `ProductChip`/`ProductSelect`. The station capacity and compartment float-gallon bugs have regression tests. Role gating matches `modules.ts`: System health is `platform_admin` only, Tier 4 Billing tabs are `platform_admin`, and Margin is admin-only and hidden when the feed is off. The Analytics range selector stays hidden (D13). The redirect map covers row 5 and every function move, and all 24 redirect tests pass in the recorded run. Portal and auth files are byte-identical to go-live-blockers. The Phase 2 edits are format-only plus the owner-requested tray fix, and all are listed in evidence.

The blocker is integration, not implementation. Fourteen go-live-blockers commits from the owner's staging sweep (F1–F15) touch nine files this branch rewrote. A merge needs hand resolution, and some resolutions are real ports rather than picking a side. The backend now ages AR by due date and returns `bucket_current_cents`. Phase 3's `ARAgingDashboard` has no Current bucket, so taking Phase 3's side would leave the bucket bars short of `total_open_cents`. `Analytics.tsx` was rewritten on both sides (457 lines on go-live-blockers). `FuelSummaryBar` is a modify/delete conflict whose "—" for no consumption has to reappear in the Fuel title-row counts. Margin availability was fixed on both branches with the same intent, so `CommerceHub` and `marginApi` need a single reconciled version. All of the iteration-3 verification ran on the pre-merge tree, so the affected suites, the chrome/axe pages for Fuel, Analytics and Billing, and a fresh-checkout build need a re-run after the merge.

<details>
<summary>Issues (3)</summary>

1. **Branch stale against go-live-blockers; F-fixes at risk (blocking)**: merging `e636358` conflicts in `Analytics.tsx`, `CommerceHub.tsx`, `ARAgingDashboard.tsx`, `AccountDetailPage.tsx`, `FuelStationDetail.tsx`, `FuelStationList.tsx`, `FuelSummaryBar.tsx` (modify/delete), `SchedulingMetricsPage.tsx` and `e2e/ui-revamp/shellFake.ts` (confirmed with `git merge-tree`). Merge it, then port F2/F3/F13 (Analytics real data, as-of, error states), F4/F11 (scheduling completion %, UTC bucket dates), F5 ("—" for non-consuming stations in the Fuel title-row counts and station detail) and F12 (AR Current bucket and "days past due" labels) into the redesigned components. Keep one Margin availability implementation. Then re-run tsc, lint, jest, a fresh-checkout build, and the ui-revamp chrome/axe/visual runs for Fuel, Analytics, Billing and Scheduling metrics, and record the results in evidence.
2. **Phase 2 `CompartmentsTab` codes (follow-up)**: `CompartmentsTab.tsx:138, 359` still print a raw `product_code`, and `:143` is a `type="number"` input (confirmed; Phase 2 file, logged in evidence). Not blocking for Phase 3. Schedule it with the next board change.
3. **Visual baselines self-recorded, Darwin only (follow-up)**: CI skips the pixel compare on Linux, so the new Billing/Compliance/Settings baselines guard nothing in CI (confirmed, carried). Record Linux baselines before go-live.

</details>

<details>
<summary>Details</summary>

### What go-live-blockers changed underneath Phase 3

```
ab5f933 (merged into ui-phase3 at 18b4829)
   │
   ├── ui-phase3: 5300be5 … eb0eec5  (iteration-3 fixes, evidence)
   │
   └── go-live-blockers: 776055c … e636358  (F1–F15 staging sweep)
          backend: analytics snapshots/as_of/time-series, AR due-date aging,
                   fuel avg-days exclusion, live delay minutes, driver reset
          UI:      Analytics.tsx, SchedulingMetricsPage, FuelSummaryBar/
                   StationList/StationDetail/DashboardView, ARAgingDashboard
                   (+ agingLabels), AccountDetailPage, CommerceHub + marginApi
                   (margin availability), shellFake ar-aging body
```

The backend half merges cleanly. That widens the gap: after the merge, the API returns fields the Phase 3 UI doesn't render. For AR aging, the five-bucket response would show four bars that no longer sum to the total. For Fuel, the backend now excludes non-consuming stations from the average, while go-live-blockers' UI shows "—" for them, a display rule that lived in the deleted `FuelSummaryBar`. The Margin fix on go-live-blockers (`32614d9`) is the same change Phase 3 iteration 2 made (`getMarginAvailability`, `MarginUnavailable`), written independently, so the conflict in `CommerceHub` is two versions of one behaviour and should come out as one. `e636358` changes the same `shellFake` AR aging fixture that Phase 3 depends on for its Billing chrome/axe pages.

</details>

<details>
<summary>File map</summary>

- `components/ui/{FormDialog,Modal,Table,Menu,FilterChips,EmptyState,InlineBanner}.tsx`, `ui/sharedAdditions.test.tsx`, `lib/format.ts`: 3.11 shared pieces (opt-in).
- `components/commerce/margin/{CostImportDialog,MarginHub,MarginRecordsPage,marginToolbarSlot}.tsx`: two-step import, Records toolbar in the sub-tab row, 172 px.
- `components/ops/{SourcingPage,CargoManifestEditor,FuelDistributionPage}.tsx`: wait report and manifest on FormDialog, cluster chips by name.
- `components/AIChat.tsx` (+test): no decision without an action id.
- `components/commerce/PaymentsListPage.tsx`, `ops/AgentSettingsPage.tsx`, `NotificationMetrics*`, `compliance/TaxJurisdictionsPage.tsx`: axe and wrap fixes.
- `e2e/ui-revamp/{pages,chrome,axe,visual,phase3Dialogs}.spec/ts`, `phase3Fake.ts`, `__screenshots__/*`: platform_admin roles for Tier 4 pages, dialog axe, new baselines.
- `spec/tasks.md`, `phase3-evidence.md`: ticks, 3.11 re-scope, iteration-3 evidence.

Full diff: `git -C .worktrees/ui-phase3 diff origin/production-readiness/go-live-blockers...HEAD`. This pass: `git diff b70d8d9 eb0eec5`. Pending upstream: `git log HEAD..origin/production-readiness/go-live-blockers`.

</details>
