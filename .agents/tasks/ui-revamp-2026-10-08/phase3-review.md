# Phase 3 iteration 5 review: go-live-blockers F-fixes ported; AR aging title row clips

This pass covers `eb0eec5..e7b71f6`: the `e636358` (F1–F15) and `e19fb9b` merges, the hand-ported F-fixes in the redesigned components, and the iteration-4 evidence. The blocking finding from iteration 4 is closed. The branch is now level with `origin/production-readiness/go-live-blockers` (`HEAD..origin` is empty, and `ls-remote` matches `e19fb9b`). Backend, portal and auth trees are byte-identical to go-live-blockers. The ports are faithful to the upstream intent: the Current bucket and "days past due" labels in AR aging and account detail, "—" for non-consuming stations in the Fuel counts, list (sorted last) and drawer, the upstream Analytics rewrite with Phase 3 chrome on top, and the scheduling completion rate no longer multiplied by 100. Margin availability is one implementation. Every Phase 3 task is ticked. The evidence records tsc, lint, jest, a fresh-checkout build, a 257/257 ui-revamp run, the board e2e (N1 18.4 ms), 172 px on every list page, and axe clean.

Watch for: on Billing → AR aging, the title-row counts are hard-clipped for platform_admin (confirmed from the implementer's own screenshots). At 1280×800 only a stray "$" shows after the nine tabs. At 1440×900 the text stops mid-word at "$53,123.00 outstanding · 22 a". The outstanding total, the account count and the "90+ days past due" figure the F12 port added are never visible at the budget size.

**Verdict**: NEEDS_CHANGES

## High-level view

The merge was done as a port, not a pick-a-side. AR aging keeps the Phase 3 list template and adds Current first, in the `open` hue, with short legend labels and the full `AGING_LABELS` text available to assistive tech. Account detail goes to six facts. `FuelSummaryBar` stays deleted, and its "—" rule now lives in `avgDaysLeftLabel()` and `displayDaysUntilEmpty()`, with the upstream test moved to the new components. Analytics takes the upstream F2/F3/F13 body, then removes the nested hero header, publishes "As of …" to the hub title row, uses the `CHART` pie palette and keeps the range selector hidden (D13). Scheduling metrics keeps the upstream UTC bucket labels and goes through `pct()` without `fraction`. The `CommerceHub` difference from go-live-blockers is only Phase 3's detail-route navigation, and both `/dashboard/billing/accounts/[id]` and `/invoices/[id]` exist, with invoice → account wired.

The one regression is presentational. AR aging's counts are a single `whitespace-nowrap` span in the shared title row, whose counts container is `overflow-hidden` with no ellipsis. Platform_admin sees nine Billing tabs, so the counts have almost no room. The legend row directly below has about 300 px of free space at 1280.

The Phase 2 file set changed against go-live-blockers is the same one recorded and accepted in iterations 2–3 (CreateJobModal, OrderDetailView, JobsView, tray, CompartmentsTab/DriverTray format-only, CargoManifestEditor, ReplanDiffBody). The dashboard, control/Live and portal/auth files are untouched.

<details>
<summary>Issues (4)</summary>

1. **AR aging title-row counts clipped (blocking, confirmed)**: `ARAgingDashboard` counts are hidden at 1280×800 (a lone "$") and cut mid-word at 1440×900 for platform_admin. Move "outstanding · accounts · 90+ past due" into the legend row, which has room, or give the title-row counts `truncate` with a `title`. Add an e2e assertion that the counts aren't clipped (`scrollWidth <= clientWidth`) at 1280 for platform_admin, and re-shoot.
2. **AR aging visual baseline is stale (follow-up, confirmed)**: per the evidence, `billing-ar-aging` still compares against the iteration-3 render without the Current column, so it guards the old layout. Re-record it together with the fix for issue 1.
3. **Analytics trend axis shows ISO dates (follow-up, likely)**: the 30-day chart labels read "2026-10-06" (upstream `utcDay`), not `calendarDate`-style dates. This was a deliberate upstream UTC choice, so it doesn't block. Consider `calendarDate()` in UTC, with "UTC" in the axis title.
4. **Carried follow-ups (non-blocking, confirmed)**: `CompartmentsTab.tsx:138,359` raw `product_code` and `:143` `type="number"` (Phase 2 file); Darwin-only, self-recorded visual baselines; record Linux baselines before go-live.

</details>

<details>
<summary>Details</summary>

### AR aging counts vs the nine-tab Billing title row

```
| Billing (i) | Accounts | Invoices | Price Books | Pricing Rules | Contracts | Payments | AR Aging | Reconciliation | Margin | $…   ← counts, overflow-hidden
| ▇▇▇▇ Days past due: ● Current $8,000 ● 1–30 $21,000 ● 31–60 … ● 90+ $5,000            [~300 px free]
```

`PageHeader` renders contributed counts in `flex min-w-0 … overflow-hidden` with no `truncate`, and AR aging wraps its counts in `whitespace-nowrap`. The text is cut at the container edge with no ellipsis and no tooltip. In `screenshots/phase3/billing-ar-aging-1280x800.png` the only visible character is "$". In the 1440 shot it ends at "· 22 a". The F12 relabel ("90+ days past due") made the string longer, but it was already past the edge. Payments has no counts and fits, so this is specific to pages that contribute counts under the platform_admin tab set. The chrome spec measures the first row (172 px) and axe doesn't flag clipped text, so neither check caught it. A `scrollWidth <= clientWidth` check on the counts node at 1280 would.

</details>

<details>
<summary>File map</summary>

- `commerce/ARAgingDashboard.tsx`, `commerce/AccountDetailPage.tsx`, new `commerce/agingLabels.ts`, `services/commerceApi.ts`: F12 Current bucket and days-past-due labels.
- `ops/FuelDashboardView.tsx`, `ops/FuelStationList.tsx`, `ops/FuelStationDetail.tsx`, `services/fuelApi.ts`, new `ops/FuelDaysLeft.test.tsx`: F5 "—" for non-consuming stations.
- `Analytics.tsx` (+test), `services/api.ts`: upstream F2/F3/F13 body with Phase 3 chrome and as-of in the title row.
- `ops/SchedulingMetricsPage.tsx` (+test): F4/F11 with `lib/format`.
- `e2e/ui-revamp/phase3Fake.ts`, `__screenshots__/analytics-*`: AR Current and Analytics fixtures, re-recorded Analytics baselines.
- `phase3-evidence.md`: iteration 4.

Full diff: `git -C .worktrees/ui-phase3 diff origin/production-readiness/go-live-blockers...HEAD`. This pass: `git diff eb0eec5 e7b71f6`.

</details>
