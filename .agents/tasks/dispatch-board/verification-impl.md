# Dispatch Board: final implementation verification

Date: 2026-10-08. Worktree `.worktrees/dispatch-board`, branch `feature/dispatch-board`, based on `production-readiness/go-live-blockers` at `ad72dde`. Nothing pushed, merged or rebased. No staging, AWS, deploy, flag or data action was taken.

Verdict: every CI-equivalent suite passes on this branch. Every plan task is ticked except task 41 (staging), which is deferred as a runbook. Task 36b was still open when this step started; it is now implemented and tested (see "Fixed in this step").

## What was built, per phase

| Phase | Tasks | Commits | What it adds |
|---|---|---|---|
| 0 Spike and prerequisites | 1–6 (4b, 5b, 5c) | `83729c6` (spike, removed), `064434f`, `3e9ada8`, `5090b0e` | Pragmatic DnD chosen. Driver manifest resolved by run and status (R25). Dispatch takes an explicit driver and `notify`. From-link CAS relink. Check-in compare-and-set. Guarded driver transitions and the `in_transit` gate 0 for board runs. Additive mappings and codes. `BOARD_OWNED_PLAN` guards in MVP endpoints and the exception-replanning agent. Shared validation helpers lifted out of the route planning agent. |
| 1 Backend core | 7–14 | `90013c2` | Models with `extra="forbid"`, ETA module, command engine, validation service, board service (snapshot, validate, commands, undo/redo, history), the `dispatch_board` flag and admin pair, the router, telemetry and audit. |
| 2 Publish and after-publish | 15–17 | `46c90a3`, `48cf645` | `BoardPublishService` (dry run, groups, all-or-none claim, lease, first publish) and `BoardRedispatchService` (seven phases, rollback before an order status changes, forward completion after). In-process integration test through the real executor, dispatch, check-in and driver work services. |
| 3 Realtime and suggestions | 18–20 | `02975db` | `/ws/dispatch-board` with presence, the order-event listener that marks lanes stale, and agent suggestions (read, accept, dismiss, reject). |
| 4 Frontend foundation | 21–25 | `f4a802f`, `d72144a` | Pinned Pragmatic packages, typed API client, reducer and optimistic commands, socket hook, the Board tab and board shell. |
| 5 Board interactions | 26–32 | `dd6e360` | Trays, grid and lanes, gauges and check chips, the DnD adapter, menus and Place mode, keyboard model, live regions. |
| 6 Drawer, publish, suggestions, map | 33–36 | `7017539` | Detail drawer (checks, compartments, history, acknowledgements), publish dialog and recovery states, suggestion ghosts and diff, lane route map. |
| 7 Responsive, performance, a11y, e2e | 37–40 | `af36210`, `bba20e9` | Stacked layout below 1024 px, target-size audit, N1 budgets at 1,500 stops, Playwright e2e with axe scans and frame time, CI `dispatch-board-e2e` job. |
| 8 Docs | 42 (41 deferred) | `505e967` | README board section, endpoint block and admin flag docs. Staging runbook for task 41. |
| Final verification | 36b | `31b9345`, `4dc2ca5` | Lane truck type (R2.8) and driver tanker endorsement plus nearest expiry (R3.4). The N1 perf budgets are now enforced untraced in CI. Postgres tray-exclusion test. |

Each phase also has a `docs(dispatch-board): record … commit id in evidence` commit.

## Fixed in this step

Task 36b (Phase 5 review P5-1) was unticked and not deferred, so R2.8 and R3.4 were partly unmet. Implemented in `31b9345`:

- `LaneView.truck_type` is the `trucks` asset's `asset_subtype`, matched on `asset_id` or `truck_id` within the tenant, in one read per snapshot (`DispatchBoardService._truck_types`). Command answers and socket events don't carry it, so `boardReducer.withLane` keeps the type it already has for that truck.
- `DriverSummary.tanker_endorsement` and `nearest_expiry {kind, expires_on}` come from the compliance DQ record in `drivers`, matched by `driver_id` or `external_refs.ops_driver_id` (the B10 link), in one `_source`-filtered read per driver tray (`_dq_records`). The endorsement is `true` only while unexpired. Nearest expiry is the earliest of CDL, medical card, HAZMAT and tanker dates, past dates included. A driver with no DQ record gets `null` for both ("unknown", not "no").
- UI: the lane header shows the type after the truck id, and the lane's accessible name reads "Truck T1 (tank wagon), …". Driver chips add "Tanker" and "Medical card expires 2026-11-02" (or "expired") to the text and accessible name.
- A failed asset or DQ read leaves the fields empty and never fails the snapshot. It isn't reported as a degraded source, because nothing is blocked by it.
- Tests: `test_dispatch_board_service.py::test_lane_truck_type_present_missing_and_other_tenant`, `::test_driver_tray_tanker_endorsement_and_nearest_expiry`, `tests/postgres/test_dispatch_board_atomic.py::test_truck_type_and_dq_lookups_on_the_real_translator`, `grid.test.tsx` (header and lane name), `trays.test.tsx` (chip text, expired, no record), `boardReducer.test.ts` (type kept across command and socket lanes, replaced by a newer typed lane).
- Decision: these are the only data sources in the codebase that carry these fields, so no owner call was needed. Platform asset subtypes are values like `truck` and `fuel_truck`, so most lanes will read "fuel truck" until fleets record finer types.

Also added in this step: `tests/postgres/test_dispatch_board_atomic.py::test_tray_excludes_drafted_orders_on_the_real_translator`. Phase 7 left the tray query's `must_not terms order_id` clause (P7-1) unexercised on Postgres. It now passes on the real translator. Staging latency for that query still waits for task 41.

**CI blocker found and fixed: the N1 perf budgets failed under `--cov`.** CI's `backend-tests` job runs the whole suite with `--cov … -x`. Earlier phases measured the suite with `--no-cov`, so this was never seen. The first CI-exact local run stopped at `test_dispatch_board_perf.py::test_snapshot_p95_within_budget`: p95 was 3.67 s against a 2.0 s budget. Line tracing slows this code several-fold; the same test untraced gives 0.43 s. The first push would have gone red on this.

Fix: the four timing tests still run under coverage, then skip only their budget assertion, with the measured p95 in the skip reason (`assert_within` in the perf module). A new `backend-tests` step, "Dispatch Board latency budgets (no coverage tracing)", runs the module with `--no-cov` and fails if any budget is skipped there. Local untraced p95s: snapshot 434 ms (budget 2,000), batch validate 193 ms (500), position validate 60 ms (300), command 135 ms (600), 9 passed. Under `--cov`: 5 passed, 4 skipped. The budgets themselves don't change. Risk: GitHub's runners are slower than this machine. Batch validate has the least headroom (about 2.6x), so watch it on the first CI run.

## Test results

All runs are in the worktree. `PY=/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend/venv/bin/python`. Backend runs use cwd `Runsheet-backend`, and UI runs use cwd `runsheet`.

| Suite | Command | Result |
|---|---|---|
| Backend full (before 36b) | `REDIS_URL=redis://localhost:6379 JWT_SECRET=ci-test-jwt-secret JWT_ALGORITHM=HS256 ENVIRONMENT=test $PY -m pytest --no-cov -q -p no:cacheprovider -o log_cli=false` | 13190 passed, 238 skipped, 0 failed |
| Backend full (after 36b) | same | **13192 passed, 239 skipped, 0 failed** (5 m 31 s) |
| Backend, CI-exact with coverage | same env, `$PY -m pytest --cov=. --cov-report=html:coverage_html --cov-report=xml:coverage.xml --cov-report=term-missing --cov-fail-under=70 -x -q`, then `$PY scripts/check_coverage.py --threshold 0` | First run: stopped by `-x` on the perf budget under coverage (fixed above). After the fix: **13188 passed, 244 skipped, 0 failed** (the 4 budget assertions skip under tracing), total coverage **77.98 %** (gate 70 %), changed-file gate passed for 237 files. Board modules: engine 92.7 %, publish 90.3 %, service 88.0 %, suggestions 86.3 %, models 100 %. |
| Perf budgets, untraced (new CI step) | `$PY -m pytest tests/unit/test_dispatch_board_perf.py --no-cov -q -p no:cacheprovider -rs` | 9 passed, 0 skipped |
| Board-focused backend | `… pytest tests/unit/test_dispatch_board_*.py tests/unit/test_dispatch_validation*.py tests/integration/test_dispatch_board_publish_flow.py` | 432 passed (includes the N1 perf budgets) |
| Postgres suite | `ENVIRONMENT=test REDIS_URL=redis://localhost:6379/0 DATABASE_URL=postgresql+psycopg://runsheet:runsheet@localhost:5432/runsheet POSTGRES_TEST_URL=<same> $PY -m pytest tests/postgres -q --no-cov -p no:cacheprovider` | **235 passed, 0 skipped** (233 before this step, plus the 36b lookup test and a new tray-exclusion test) |
| Endpoint registry | `REDIS_URL=redis://localhost:6379 JWT_SECRET=ci-test-jwt-secret JWT_ALGORITHM=HS256 ENVIRONMENT=test $PY scripts/generate_endpoint_registry.py`, then `git diff --exit-code docs/endpoint-registry.md` | 354 entries, exit 0 (clean) |
| UI install | `npm ci` | ok |
| UI typecheck | `npx tsc --noEmit` | exit 0 |
| UI lint | `npm run lint` (`biome check`) | exit 0. 468 files, 0 errors, 1 warning (`noConfusingVoidType` in `DispatchBoard.tsx:320`, present since Phase 4, not an error in CI) |
| UI unit | `npm test -- --ci` | **104 suites, 1227 passed, 1 skipped, 0 failed** |
| UI build | `npm run build` with the CI `NEXT_PUBLIC_*` placeholders | exit 0, 40/40 static pages |
| Board e2e (CI job) | `PW_BOARD_PROD=1 <CI placeholders> npx playwright test -c playwright.dispatch-board.config.ts --project=chromium` | **17 passed, 2 skipped** (iPad-only tests), frame time p95 17.6 ms on the production bundle |
| Driver app install | `npm ci` | ok (needed an `npm cache verify` after one EEXIST cache error) |
| Driver app typecheck | `npm run check-types` | exit 0 |
| Driver app lint | `npm run lint` | exit 0 |
| Driver app tests | `npm test -- --ci` | **14 suites, 187 passed, 0 failed** |
| Git hygiene (CI job, locally) | `git ls-files` artefact scan, ADR index, `main.py` line count | no artefacts, ADR index present, `main.py` 170 ≤ 200 |

Not run locally: the `docker-image` and `migration-check` jobs. The Docker CLI hung on this machine (the daemon didn't answer `docker system df`), and this branch adds no migration, Dockerfile or backend dependency. The `dispatch-board-e2e` job has never run on GitHub because the branch isn't pushed.

Load-related reruns, recorded for honesty:

- The first full UI run after adding the 36b tests had one wrong expectation in my new test (fixture defaults). I fixed it and the suite reran clean.
- The second UI run had 3 timeouts and one jest worker SIGSEGV in unrelated suites (`nonDrag`, `map`, `trayOrders`, `ApprovalQueue`). The load average was about 13 because a VM process was using about 190 % CPU. Those 4 suites then passed alone (43/43), and the next full run passed (1227/1227).
- The first driver-app run had one 5 s timeout in `sign-in-forgot-password.test.tsx`. The rerun passed 187/187. This branch doesn't change `driver-app/` (`git diff --stat ad72dde -- driver-app` is empty).

## Plan status

46 tasks: 45 ticked, 1 deferred.

- Task 41 (staging rollout) is **deferred, runbook only**. The owner deferred staging for this workflow. `staging-runbook.md` has the full procedure: baselines, deploy, `QA-BOARD-*` fixtures, shadow, `active_gated` publish, driver app manifest, Fuel Distribution hiding, the published-stop move, N1 latency, cleanup and flag restore. Run it, then tick the task.
- No other task is open.

## Requirement coverage (code → tests)

Backend paths are under `Runsheet-backend/`. UI paths are under `runsheet/src/components/dispatch-board/` unless a fuller path is given.

| Req | Code | Tests |
|---|---|---|
| R1 entry, flag, visibility | `fuel/api/dispatch_board_endpoints.py` (`board_mode_guard`), `fuel/api/feature_flag_admin_endpoints.py`, `runsheet/src/components/DispatchPage.tsx`, `DispatchBoard.tsx` | `test_dispatch_board_flags.py`, `test_dispatch_board_endpoints.py`, `DispatchPage.test.tsx`, `DispatchBoard.test.tsx`, `grid.test.tsx` (`?truck=`) |
| R2 snapshot, day, shift | `fuel/services/dispatch_board_service.py` (`snapshot`, `_truck_types`), `dispatch_board_eta.py`, `dispatch_validation.py` (HOS today only), `BoardToolbar.tsx`, `ShiftPicker.tsx`, `viewState.ts`, `grid/` | `test_dispatch_board_service.py`, `test_dispatch_board_eta.py`, `test_dispatch_validation.py`, `DispatchBoard.test.tsx`, `viewState.test.ts`, `grid.test.tsx` |
| R3 trays | service `_trays`, `_driver_tray`, `_dq_records`, `_truck_tray`; engine (on hold, other day); `trays/` | `test_dispatch_board_service.py`, `test_dispatch_board_engine.py`, `tests/postgres/test_dispatch_board_atomic.py`, `trays.test.tsx`, `trayOrders.test.tsx` |
| R4 filters, search, density | service tray filters, `viewState.ts`, `FilterChips.tsx`, `boardMatch.ts`, `BoardToolbar.tsx` | `test_dispatch_board_service.py::test_filters_narrow_only_the_tray`, `viewState.test.ts`, `trays.test.tsx`, `grid.test.tsx`, `responsive.test.tsx` |
| R5 load an order | `dispatch_board_engine.py` (assign, best fit, insertion), `dispatch_board_eta.py`, `dnd/adapter.ts`, `intents.ts`, `CompartmentGauge.tsx`, `drawer/` | `test_dispatch_board_engine.py`, `test_dispatch_board_eta.py`, `dragWiring.test.tsx`, `CompartmentGauge.test.tsx`, `drawer.test.tsx`, `e2e/dispatch-board.spec.ts` |
| R6 pair a driver | engine `pair_driver`, `fuel/services/plan_dispatch_service.py` (explicit driver), validation qualification and HOS, `grid/LaneHeader.tsx`, `trays/DriverTray.tsx` | `test_dispatch_board_engine.py`, `test_plan_dispatch_explicit_driver.py`, `test_dispatch_validation.py`, `grid.test.tsx` (Pair), `nonDrag.test.tsx`, e2e |
| R7 rearrange | engine move, reorder, unassign, `remove_lane` | `test_dispatch_board_engine.py`, `dragWiring.test.tsx`, `nonDrag.test.tsx`, `grid.test.tsx` (Remove lane), e2e |
| R8 live validation | `dispatch_validation.py`, service `validate`, `state/precheck.ts`, `useBoardController.ts` | `test_dispatch_validation.py`, `test_dispatch_validation_helpers.py`, `test_dispatch_board_service.py`, `precheck.test.ts`, `dragWiring.test.tsx` |
| R9 invalid-drop policy, overrides | `dispatch_board_models.py`, validation catalogue, engine `acknowledge_warning`, `CheckChip.tsx`, `drawer/` | `test_dispatch_board_models.py`, `test_dispatch_validation.py`, `test_dispatch_board_service.py`, `drawer.test.tsx`, `CompartmentGauge.test.tsx` |
| R10 draft and published states | models, service `_lane_state`, `dispatch_board_order_listener.py`, `dispatch_board_es_mappings.py`, `Agents/support/mvp_es_mappings.py` | `test_dispatch_board_service.py`, `test_dispatch_board_order_listener.py`, `test_dispatch_board_mappings.py`, `test_mvp_mapping_board_fields.py`, `grid.test.tsx`, `publish.test.tsx` |
| R11 undo and redo | engine `restore_lanes`, service revert and reapply, `state/boardReducer.ts`, `keyboard/` | `test_dispatch_board_service.py`, `test_dispatch_board_engine.py`, `boardReducer.test.ts`, `keyboard.test.tsx`, `liveRegion.test.tsx` |
| R12 publish | `fuel/services/dispatch_board_publish.py` (`BoardPublishService`), `plan_dispatch_service.py`, `Agents/support/mvp_endpoints.py`, `Agents/overlay/exception_replanning_agent.py`, `publish/` | `test_dispatch_board_publish.py`, `test_mvp_board_plan_guards.py`, `tests/integration/test_dispatch_board_publish_flow.py`, `publish.test.tsx`, `liveRegion.test.tsx`, e2e publish |
| R13 changes after publish | `BoardRedispatchService`, `fuel/order_repository.py` (`relink_dispatched_assignment`), `Agents/support/plan_execution_service.py`, `driver/api/transition_endpoints.py`, `driver/services/order_transition_service.py` (gate 0), engine post-publish rules, `grid/ReassignShelf.tsx`, `publish/` recovery | `test_dispatch_board_redispatch.py`, `test_order_relink.py`, `tests/postgres/test_order_relink_postgres.py`, `test_plan_execution_checkin_cas.py`, `tests/postgres/test_checkin_amend_race.py`, `test_driver_transition_guards.py`, `test_driver_transition_endpoint.py`, integration flow, `publish.test.tsx`, `grid.test.tsx` |
| R14 concurrency, presence | service K4.2 (versions, idempotency), `dispatch_board_ws_manager.py`, `state/useBoardCommands.ts`, `state/announce.ts`, `PresenceBar.tsx` | `test_dispatch_board_service.py`, `tests/postgres/test_dispatch_board_atomic.py`, `test_dispatch_board_ws.py`, `useBoardCommands.test.tsx`, `announce.test.ts`, `runsheet/src/services/dispatchBoardApi.test.ts`, e2e second dispatcher |
| R15 live updates | `dispatch_board_ws_manager.py`, `bootstrap/websockets.py`, order listener, `runsheet/src/hooks/useDispatchBoardSocket.ts` | `test_dispatch_board_ws.py`, `test_ws_origin_routes.py`, `test_dispatch_board_order_listener.py`, `useDispatchBoardSocket.test.ts`, `boardReducer.test.ts`, `map.test.tsx` |
| R16 suggestions | `fuel/services/dispatch_board_suggestions.py`, `suggestions/`, `runsheet/src/components/ops/ReplanDiffBody.tsx` | `test_dispatch_board_suggestions.py`, `suggestions.test.tsx` |
| R17 map | `map/` | `map.test.tsx` |
| R18 non-drag and keyboard | `menus.ts`, `dialogs/`, `PlaceModeBanner.tsx`, `keyboard/` | `nonDrag.test.tsx`, `keyboard.test.tsx`, e2e accessibility |
| R19 announcements | `BoardLiveRegion.tsx`, `state/announce.ts`, text alternatives in `CheckChip.tsx` and `CompartmentGauge.tsx`, `laneSummary` | `liveRegion.test.tsx`, `announce.test.ts`, `CompartmentGauge.test.tsx`, `grid.test.tsx` (accessible names) |
| R20 tablet, small screens | `responsive/`, coarse-pointer grips in the DnD wiring | `responsive.test.tsx`, e2e stacked layout and target audit. iPad e2e runs on the WebKit projects. The real-device long press is an owner check. |
| R21 empty, loading, error | `BoardBanners.tsx`, `DispatchBoard.tsx`, `runsheet/src/services/apiErrors.ts`, `dispatchBoardApi.ts` | `DispatchBoard.test.tsx`, `dispatchBoardApi.test.ts` |
| R22 audit trail | command log in the service, publish audit and activity log, `history`, drawer History tab | `test_dispatch_board_service.py` (logs, history, actor names), `test_dispatch_board_telemetry.py`, `drawer.test.tsx`, Postgres history query |
| R23 permissions, tenancy | endpoint `_guards` (flag before role), `parse_body`, WS `_authenticate_dispatcher` | `test_dispatch_board_endpoints.py`, `test_dispatch_board_ws.py`, `test_dispatch_board_flags.py` |
| R24 telemetry | `fuel/services/dispatch_board_telemetry.py` | `test_dispatch_board_telemetry.py` (no customer text in tags or logs) |
| R25 driver manifest | `driver/services/work_service.py` | `test_work_service_manifest_by_run.py`, integration flow |
| D8 accessors | `driver/api/work_endpoints.py` `get_work_service`, `fuel/services/driver_daily_reset.py` `get_tenant_timezone` | `test_board_accessors.py` |
| N1 performance | scoped copies, best-fit trials, tray query that excludes drafted orders | `test_dispatch_board_perf.py` (60 lanes, 1,500 stops: all four budgets), e2e frame time. Staging numbers come from task 41. |
| N2 reliability | publish and redispatch recovery, write boundary | `test_dispatch_board_redispatch.py` (P12, freeze rules), `test_dispatch_board_publish.py`, `test_dispatch_board_write_boundary.py` |
| N3 security | `extra="forbid"` models, `parse_body`, no echo of submitted values | `test_dispatch_board_models.py`, `test_dispatch_board_endpoints.py` |
| N4 dependencies | `runsheet/package.json`: the four Pragmatic packages pinned exactly, plus the dev-only `@axe-core/playwright@4.13.0` (allowed as optional by task 40). No backend or driver-app dependency change. | `npm ci` from the committed lockfile |
| N5 accessibility | `keyboard/`, `responsive/`, live regions, text alternatives | e2e axe scans (0 violations), target audit, aria snapshots. Manual AT passes are open. |
| N6 rollout | flag default `disabled` (unset → 404) | `test_dispatch_board_flags.py::test_unset_flag_is_404_on_reads_and_writes`, `DispatchPage.test.tsx` (404/403 hide the tab) |
| N7 compatibility | see the next section | full suites unchanged and green |

## Flag default and flag-off behaviour

- The `dispatch_board` flag defaults to `disabled`. An unset flag, an explicit `disabled`, or a missing flag service returns 404 on every board route. A Redis error returns 404 on reads and 503 on writes (`test_dispatch_board_flags.py`). The UI shows the Board tab only when `/status` answers a mode, and a 404 or 403 hides it (`DispatchPage.test.tsx`).
- With the flag off, existing dispatch, MVP plans and the driver app behave as before, apart from the exceptions N7 names:
  - R25: the manifest resolves by run and status.
  - The additive mapping fields.
  - The R12.10 board-plan guards: `BOARD_OWNED_PLAN` on approve, reject and replan, the MVP plan-list filter, and the exception-replanning filter. They only affect `source: dispatch_board` plans, and none exist while the flag is off.
  - The R13.11 serialization: check-ins use compare-and-set and return today's errors. Driver transitions use the guarded write, so a concurrent change now returns a 409 that the driver app's queue already retries, instead of being overwritten. `in_transit` on a `bp-` run whose plan isn't `dispatched` returns 409 `BOARD_ROUTE_UPDATING`.
- Evidence that agent-written plans are unaffected:
  - `test_driver_transition_guards.py::test_agent_run_is_never_read`.
  - `test_mvp_board_plan_guards.py::test_reject_still_works_for_agent_plans` and `::test_replan_still_triggers_for_agent_plans`.
  - `test_plan_dispatch_explicit_driver.py::test_default_path_sends_assignment_once_with_todays_payload` (freeze rule 8).
  - The route-planning golden tests in `test_dispatch_validation_helpers.py`.
- The driver app has no code change on this branch, and its full suite passes.

## Git state

- `git log --oneline` shows these commits on `feature/dispatch-board`, on top of `ad72dde`: the phase commits above, then `31b9345` (36b), `4dc2ca5` (perf budgets untraced in CI, Postgres tray test) and the final `docs(dispatch-board): final verification` commit.
- `git status -sb` → `## feature/dispatch-board`, with no upstream. `git log @{u}..` doesn't apply: it fails with "no upstream configured". No remote-tracking ref has `dispatch-board` in its name, and none contains `HEAD`. Nothing was pushed.
- Main checkout (`/Users/olukotunjosh/Downloads/Runsheet`): `production-readiness/go-live-blockers` at `ad72dde`, with an empty `git status --porcelain` before and after this step. The only writes outside the worktree were scratch logs in the gitignored `tmp/dbv/`, deleted at the end. The npm cache was cleaned twice, and `brew cleanup -s` ran. `uv cache clean` and `pnpm store prune` waited on locks held by running tools, so they reclaimed nothing. Docker pruning wasn't possible because the CLI hung. Free disk was 5.7 GB at the start, which is below the ~8 GB guideline. That guideline is for Docker builds, and none ran here. Free disk was 4.3 GB after the builds; `.next`, coverage output and test results were deleted.
- `.agents/` is excluded by the local `.git/info/exclude`, not by `.gitignore`. The task files are tracked through `git add -f`, as in earlier phases. CI's hygiene check reads only `.gitignore`, so it isn't affected.

## Freeze decisions from the review loops

- **Design, freeze rule 11** (design review pass 5): passes 2–4 had no HIGH findings but kept raising new MEDIUMs in post-publish editing. Rule 11 states that nothing in post-publish depends on a skipped guard, an unlocked write or in-memory state. It has five items, each with tests:
  - (a) the gate-0 reader is required, and a stack without one refuses `bp-` `in_transit`;
  - (b) check-in plan writes merge only the given keys under `atomic_update`;
  - (c) removing the last pending stop completes the load;
  - (d) a resume after a crash before notify rebuilds payloads from documents;
  - (e) the move back after a rollback commits.
- **Freeze rules 1–10** (K8.5) are written into the design, and each has a named test in `test_dispatch_board_redispatch.py` or `test_dispatch_board_publish.py` (evidence Phase 2 table).
- **Phase 1, P1-2 freeze narrowing** of rule 11 (e): a `move_stops` that only returns pinned orders to their `assigned_run_id` load commits even when an unrelated block already exists on that lane. This narrows the rule instead of adding a path. Pass 2 found no new MEDIUM in that area, so no escalation was needed.
- **Phase 1, P1-5 (owner option A)**: the driver transition base read uses `get_current`, which removes the stale-projection 409 loop.
- The Phase 2–8 reviews record no further freeze decision. The Phase 7 decisions (test N1 at the larger 1,500-stop figure, scoped copies that fail closed, frame time advisory on CI) are recorded in evidence.md.

## Manual checks the owner must run

1. **iPad long-press drag on a real device** (R20.1, spike criterion (c)). Playwright can't synthesize iOS's long press. On an iPad with Safari, open the board (flag `shadow` or `active_gated` on a QA tenant). Long-press a stop's grip for about 300 ms, drag it to another truck, and release. Expected: a drag starts only from the grip, a tap selects (Place mode), and scrolling the lane list never starts a drag. Record the result in evidence.md, Phase 7.
2. **Assistive-technology passes**: VoiceOver with Safari (macOS and iPadOS), NVDA with Firefox, a keyboard-only pass of every dialog, real 200 % zoom, and Windows High Contrast. The automated axe scans pass. Full WCAG 2.2 AA conformance needs this manual testing and an expert review.
3. **Task 41 staging runbook** (`staging-runbook.md`), which includes warmed-service N1 latency on staging. Phase 7 review asked for this because the local budgets ran with `gc.freeze()` and a JSON-copy store shim.

## Decisions for the owner before merge or deploy

1. **Merge path.** `feature/dispatch-board` branches from `production-readiness/go-live-blockers` at `ad72dde`, and `origin/production-readiness/go-live-blockers` is 70 commits ahead of that as of the last fetch. No fetch ran in this step. Choose a merge into the go-live branch (and resolve conflicts) or a rebase. Rebasing and pushing both need your go-ahead. The CodeBuild branch rule in the standing permissions also applies before the next deploy.
2. **Push and CI.** The branch has never run on GitHub, including the new `dispatch-board-e2e` job. Push it when you want CI and a PR.
3. **Staging** (task 41). Run the runbook. The deploy-safety rules apply: a pinned worktree, CI green on the exact commit, and the UI ancestor check.
4. **Product follow-ups with no spec default** (from Phase 6, not blocking):
   - The HOS override fix link opens the driver record because there's no override form. Proposed default: keep it until a compliance form exists.
   - The Running late threshold uses the 15-minute default because the snapshot carries no tenant setting. Proposed default: keep 15 minutes.
5. **Truck types** (36b) show the platform's coarse `asset_subtype`, such as "fuel truck". Finer types (tank wagon, transport) need fleet data, not code.
