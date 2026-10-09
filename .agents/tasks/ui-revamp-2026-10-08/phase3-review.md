# Phase 3 iteration 3: Billing, Compliance, Settings, Copilot parity and guard-to-zero

Iteration 3 (`713cee9..b70d8d9`, 26 commits) merges 3P's go-live-blockers head, then covers almost all of the remaining Phase 3 scope. Billing (invoices, accounts, payments, AR aging, reconciliation, price books, margin) and Compliance (certifications, meters, BOLs, IFTA, exemptions, tax, contracts, pricing rules, terminal detail) move onto the list and detail templates with `FormDialog` create/edit flows. Settings depots, flags, intake channels, integrations, templates, road restrictions and agent settings are migrated. `/ops/command` is deleted, with redirect row 5 and an inline Copilot confirmation that now records the decision through the approvals API. The Plans view's Emergency stop and Cost configuration dialogs are fixed (the iteration-2 finding). The format guard is at 0 and every `test.fail` mark is gone. The phase still can't be approved, because the bookkeeping and verification for this iteration don't exist yet.

Watch for: none of the work since `4fc1e2c` has any evidence (confirmed). There's no tsc, lint, jest, build, e2e, chrome-height, axe or FormDialog-test record, even though `b70d8d9` removes every `test.fail` mark on the claim that all pages pass. 3.4, 3.5, 3.6, 3.8, 3.10 and 3.11 are still unticked (confirmed). The worktree has uncommitted changes to `FormDialog`, `Modal` and `Table` (confirmed). 3.11's portal swap was reverted (`dcb70b3`), and `tasks.md` doesn't record the hand-off (confirmed).

**Verdict**: NEEDS_CHANGES

## High-level view

The migration itself matches the spec. Every create/edit row in design §5 now opens a `FormDialog`, except `CostImportDialog`, which moved to the shared `Modal` instead of the "lg steps" target. The bespoke `fixed inset-0` overlays outside `ui/` are down to the §5 exemptions (`ReportViewer`, `TankConsumptionDrillIn`, board sheets) and `request-pilot`. Products render as `ProductChip`/`ProductSelect` by name. Tax rates are now converted correctly: the old form asked for "1840 for 18.4¢", which is ten times the backend's `RATE_SCALE = 10`. The new form edits cents and stores tenths of a cent. One raw-code site survived in a file Phase 3 now owns: Plans' cluster member chips print `fuel_grade` verbatim.

The gate can't pass on the record as it stands. The implementer's process puts verification in `phase3-evidence.md`, and that file stops at iteration 2. This iteration's chrome heights for the 13 new Compliance, Billing and Settings pages, axe results, jest counts and fresh-checkout build are all unrecorded. The review brief forbids re-running the suites to fill the gap. Removing the `test.fail` marks without a recorded green run is the most visible instance of the gap.

3.11 is in an awkward state. The shared pieces are landing (the `open`/`partial` tokens and the `StatusBadge` `icon` prop are committed). `FormDialog`/`Modal` `mobile="sheet"`, the `aria-disabled` submit, `keepOpenOnSuccess`, and `DataTable` `stickyTop`/touch rows sit uncommitted in the worktree. The portal-side swap was reverted because "the portal workflow owns" those files. That revert keeps the portal-untouched rule, but 3.11 as written can't then be ticked. The task needs a recorded re-scope: shared pieces here, portal swap handed to the 3P workflow, with the gate it inherits.

Copilot parity is functionally better than the console it replaces. The console's Approve and Reject buttons only changed local state, while the Copilot card calls `approveAction`/`rejectAction`. The backend chat stream doesn't appear to emit a `confirmation` event, though, so neither UI can reach this path end to end.

Phase 2 and portal/auth boundaries hold. Portal and auth files are untouched across the whole branch. The Phase 2 edits this iteration are formatting-only, for guard-to-zero: `CompartmentsTab`, `DriverTray`, `ReplanDiffBody` and `OrderDetailView`. `driver-app/lib/tokens.ts` was regenerated with the two new statuses. All of these are additive, but none is noted in evidence.

<details>
<summary>Issues (9)</summary>

1. **Phase 3 tasks unticked (blocking)**: 3.4, 3.5, 3.6, 3.8, 3.10 and 3.11 are `[ ]` in `spec/tasks.md` (confirmed), and none is post-deploy-only. Tick each one with a Done note once its evidence exists.
2. **No iteration-3 evidence (blocking)**: `phase3-evidence.md` ends at iteration 2 (confirmed). Add an iteration-3 section with tsc, lint, jest counts, a fresh-checkout build, a full `e2e/ui-revamp` run green with no `test.fail` marks, the board e2e with N1, a first-row px table at 1280×800 and 1440×900 for every new Compliance, Billing and Settings page in `pages.ts`, axe per new page and open dialog, tokens/CVD for `open`/`partial`, and the FormDialog behaviour tests per migrated flow.
3. **Uncommitted shared-component changes (blocking)**: `ui/FormDialog.tsx` (`mobile`, `submitDisabled`, `keepOpenOnSuccess`), `ui/Modal.tsx` (`mobile="sheet"`) and `ui/Table.tsx` (`rowHeight="touch"`, `stickyTop`) are modified but not committed, and have no tests (confirmed). Commit them with no-change tests for existing callers, or drop them, and note them as additive in evidence.
4. **3.11 scope versus the portal revert (blocking)**: `dcb70b3` reverted the portal-side swap, but 3.11 still requires removing `PORTAL_EXTRA_STATUS`/`PortalBadge`, `portalFormat.ts`, `PortalTable` and the rest (confirmed). Record in `tasks.md` that 3.11 delivers the shared pieces only and hands the portal swap to the 3P workflow, gated on unchanged portal baselines, then tick it. Otherwise do the swap.
5. **Raw product code in Plans clusters**: `ops/FuelDistributionPage.tsx:2615` renders `` ` · ${m.fuel_grade}` `` (confirmed). The file is Phase 3-owned under 3.10. Use `productName()` or a `ProductCap`.
6. **Cost import not on FormDialog**: `commerce/margin/CostImportDialog.tsx` uses the shared `Modal`, while design §5 targets "lg steps" (confirmed). Move it to a stepped `FormDialog` (Check file → Import), or record the deviation and the reason in evidence.
7. **Inline numeric create forms outside §5**: the Sourcing wait report (`ops/SourcingPage.tsx:707`) and `ops/CargoManifestEditor.tsx:294` are still inline `type="number"` create forms (confirmed; both predate this branch). D9 says every create/edit flow. Migrate them, or list them as recorded exceptions.
8. **Copilot confirmation unreachable and empty-id approve**: the chat stream doesn't appear to emit `type: "confirmation"` (likely; no emitter found in `Runsheet-backend`), and `AIChat` shows Approve/Reject even when `actionId` is `""`, which would call the approvals API with an empty id (confirmed). Hide the buttons (keep "Open in Approvals" hidden too) when there's no id, and note in evidence that the path is backend-gated.
9. **Phase 2 and driver-app edits not recorded**: format-only edits to `CompartmentsTab`, `DriverTray`, `ReplanDiffBody`, `OrderDetailView`, plus the regenerated `driver-app/lib/tokens.ts` (confirmed additive). List them in evidence. `CompartmentsTab.tsx:138, 359` still print a raw `product_code` (pre-existing, Phase 2 file): log it as a follow-up.

</details>

<details>
<summary>Details</summary>

### Evidence gap and the `test.fail` removal

`b70d8d9` empties `pages.ts` of `chromeTask`/`axeTask` marks, and the header comment now says "no `test.fail()` marks remain after task 3.10". At iteration 2, three of those pages were measurably over budget or row-less: Compliance at 262 px, Billing invoices at 197 px, and Reconciliation, Settings depots and flags with no row under fixtures. The commits between them (`7483c51`, `844919f`, `bbe65c7`, `4aa4149`, `c30dd6f`) plausibly fix this: list templates, fixtures, and title-row counts in place of bands. Nothing records the numbers, though. The same goes for 13 new entries in `pages.ts` (Compliance ×4, Billing ×8, Settings tax/exemptions). The brief's rule applies: missing evidence for a required check is a finding, not something to re-run here.

### 3.11 and the portal boundary

```
0cfa3d5  tokens open/partial + StatusBadge icon + portal badge swap   (3.11)
dcb70b3  revert portal files (portal workflow owns them), keep tokens
worktree FormDialog mobile/submitDisabled/keepOpenOnSuccess, Modal sheet,
         Table touch/stickyTop  (uncommitted)
```

The shared side is additive. `StatusBadge` gains an optional `icon` and a `data-icon` attribute, and the uncommitted props all default to today's behaviour. One caveat applies to the `Table` change: with `stickyTop` set, the `overflow-x-auto` wrapper is dropped, so a wide table on a narrow screen will overflow its container. That trade-off is fine for the portal but should be stated in the prop doc. The portal itself is byte-identical to go-live-blockers. What's missing is a decision record that reconciles the task text with the revert.

### Copilot confirmation

The deleted console's `handleInlineConfirm` only flipped `resolved` and appended a message, with no API call. The Copilot card calls `approveAction`/`rejectAction` and links to Live → Approvals, and `AIChat.test.tsx` covers show, approve, reject and failure. This raises the stakes on a correct `actionId`. `action.action_id || ""` lets a card with no id render live buttons. A search of `Runsheet-backend` found no chat SSE emitter for `confirmation`, so in practice approvals still go through Live → Approvals.

### Format guard and remaining codes

The guard reaching 0 counts only `toFixed`/`toLocale*` calls. It doesn't catch raw enum interpolation, and the Plans cluster chip (`m.fuel_grade`) slipped through that way. The same chip also shows `station_id`/`customer_tank_id` instead of names, which is the same readability class. `MarginRecordsPage` still shows `customer_id`/`terminal_id` columns raw, but those are identifiers, not codes.

### Carried from iteration 2

The visual baselines for Fleet, Customers and the board were self-recorded, and Linux baselines are still absent. Neither is addressed yet. Neither blocks approval, but both should be in the iteration-3 "not verified" list.

</details>

<details>
<summary>File map</summary>

- `components/commerce/*` (Invoices, Accounts, Payments, AR aging, InvoiceDetail, AccountDetail, PriceBookEditor, PortalAccessPanel, billingStatus, useCursorPages) and `commerce/margin/*`: Billing list/detail templates, sm/lg FormDialogs, ProductChip.
- `components/ops/ReconciliationPage.tsx`: list template, Variance badge, shared Drawer.
- `components/compliance/*`: Certifications, Meters, BOLs, IFTA, Exemptions, Tax (cents ↔ tenths), Contracts, Pricing rules, Terminal detail on templates + FormDialog.
- `components/admin/*`, `NotificationSettingsTab.tsx`, `ops/AgentSettingsPage.tsx`: Settings sections, depot/flag/channel/integration/template/road-restriction FormDialogs, confirms on Modal.
- `components/AIChat.tsx` (+test), `app/ops/command/**` (deleted), `ops/AgentToast.tsx` (deleted), `config/redirects.ts`, `e2e/ui-revamp/redirects.spec.ts`: 3.6 parity and row 5.
- `components/ops/FuelDistributionPage.tsx` (+dialogs test): Emergency stop, Cost configuration on FormDialog; `ui/FormDialog.tsx` blocks NaN.
- `components/ui/StatusBadge.tsx`, `design/tokens.json`, `styles/tokens.*`, `driver-app/lib/tokens.ts`: `open`/`partial`, `icon` prop.
- `dispatch-board/drawer/CompartmentsTab.tsx`, `trays/DriverTray.tsx`, `ops/ReplanDiffBody.tsx`, `orders/OrderDetailView.tsx`, other ops/import files: lib/format only.
- `lib/format.guard.test.ts` (0), `types/commerce.ts` (deleted), `e2e/ui-revamp/{pages,chrome,axe,phase3Fake,shellFake}.ts`.
- Uncommitted: `ui/FormDialog.tsx`, `ui/Modal.tsx`, `ui/Table.tsx`.

Full diff: `git -C .worktrees/ui-phase3 diff origin/production-readiness/go-live-blockers...HEAD`; this iteration: `git diff 713cee9 HEAD` plus `git diff` (worktree).

</details>
