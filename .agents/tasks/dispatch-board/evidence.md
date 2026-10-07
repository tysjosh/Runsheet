# Dispatch Board: implementation evidence

Branch `feature/dispatch-board` (from `production-readiness/go-live-blockers` at `ad72dde`), worktree `.worktrees/dispatch-board`. Plan: `plan.md`. Design: `design.md` (freeze rules 1–11 in K8.5). Owner accepted all Q1–Q16 defaults on 2026-10-06.

Record per phase: tasks done, commits, test commands and results, decisions and reasons, open blockers.

## Phase progress

### Phase 0: Spike and prerequisites

Status: tasks 1, 2, 3, 4, 4b, 5, 5b, 5c and 6 done. Commits on `feature/dispatch-board`: `83729c6` (DnD spike, throwaway) and `064434f` (backend prerequisites, spike removed, this record). No staging, AWS or CodeBuild action was taken.

#### Task 1: DnD spike — library chosen: **Pragmatic drag and drop**

Packages, exact versions (installed with `npm install --no-save` for the spike; `package.json` and the lockfile are unchanged until plan task 21): `@atlaskit/pragmatic-drag-and-drop@4.0.0`, `@atlaskit/pragmatic-drag-and-drop-hitbox@3.0.0`, `@atlaskit/pragmatic-drag-and-drop-auto-scroll@3.2.1`, `@atlaskit/pragmatic-drag-and-drop-live-region@2.1.0`. Deps are only `@babel/runtime`, `bind-event-listener`, `raf-schd` (no Emotion/Compiled). The `react-aria-components` fallback was not needed.

Spike code: `runsheet/src/components/dispatch-board/dnd/__spike__/{adapter.ts,SpikeBoard.tsx,adapter.spike.test.ts}`, route `src/app/dnd-spike/page.tsx`, `e2e/dnd-spike.spec.ts`, `playwright.spike.config.ts` (own port 3123 so it never reuses another checkout's dev server). Kept in history at `83729c6`, deleted in the next commit. Layout: a tray of order cards (drag starts only on the grip), a 360 px vertical scroller with 24 lanes, each lane a 600 px horizontal scroller of 14 stops (the nested layout of K1/K14.3).

| Criterion | Result | How |
|---|---|---|
| (a) tray → lane drop, insertion index from `location.current.input` | PASS (Chromium, WebKit, iPad WebKit) | `onDrop` passes `location.current.input.clientX` to `insertionIndexFromClientX` (count of stop midpoints left of the pointer). Drop just right of stop 1's midpoint in lane T1 → `index: 2`, lane order `T1-s0, T1-s1, o1, T1-s2…`; `onDrag` hover index `2` matched before the drop. |
| (b) nested vertical + horizontal auto-scroll | PASS (Chromium, WebKit, iPad WebKit) | `autoScrollForElements` on the outer vertical grid and on each lane's horizontal scroller. Pointer held near the grid's bottom edge → `scrollTop > 0`; then near a lane's right edge → that lane's `scrollLeft > 0`. |
| (c) iPad Safari long-press drag | PASS for the automated part; **one manual device check still needed** | WebKit with Playwright's `iPad Pro 11` profile (`hasTouch`, `isMobile`): the grip has `touch-action: none`, the card is `draggable="true"`, and a drag from the grip drops at `index: 0`. Playwright can't synthesize iOS's native long-press drag from touch events, so the long press itself must be checked once on a real iPad (Safari, long-press the grip, drag to a lane). Owner to run before Phase 7 (plan task 37/R20.1); nothing in Phase 1–3 depends on it. |
| (d) Jest/jsdom imports the adapter; drop handlers invoked directly | PASS | `adapter.spike.test.ts`: imports the adapter (Pragmatic CJS builds load under `next/jest` with no `transformIgnorePatterns` change), calls `insertionIndexFromClientX` / `resolveLaneDrop` directly, and registers + cleans up `draggable`, `dropTargetForElements` and `autoScrollForElements` on jsdom nodes (`draggable` attribute set then removed). Pragmatic logs a dev warning that a jsdom node isn't scrollable; harmless. |

Commands (cwd `.worktrees/dispatch-board/runsheet`):

- `npm ci --no-audit --no-fund` → ok
- `npm install --no-save --no-audit --no-fund @atlaskit/pragmatic-drag-and-drop@4.0.0 @atlaskit/pragmatic-drag-and-drop-hitbox@3.0.0 @atlaskit/pragmatic-drag-and-drop-auto-scroll@3.2.1 @atlaskit/pragmatic-drag-and-drop-live-region@2.1.0` → ok, `package.json` unchanged
- `npx jest src/components/dispatch-board/dnd/__spike__` → 1 suite, 4 passed
- `npx playwright install chromium webkit` → ok
- `npx playwright test -c playwright.spike.config.ts` (webServer `npx next dev --turbopack -p 3123`, "Next.js 15.5.22 (Turbopack)") → 7 passed, 2 skipped (test (c) runs on the iPad project only, by design)
- `npx biome check --write <spike files>` then `npx biome check <spike files>` → clean
- `npx tsc --noEmit` (whole app with the spike present) → exit 0

The final tree has no net frontend change (spike deleted), so frontend CI (`npx tsc --noEmit`, `npm run lint`, `npm test -- --ci`, `npm run build`) is identical to the base commit and was not re-run; `npm run build` was not run at all because of free disk (see below).

#### Tasks 2–6: backend

| Task | Change | New tests |
|---|---|---|
| 2 (K12) | `DriverWorkService._fetch_loading_plan(tenant_id, asset_id, run_id)`: `status in [scheduled, dispatched, completed]`, `must_not status=superseded`, and when the order has a run, `run_id == run` OR `plan_id == run`; caller passes `assigned_run_id`. | `tests/unit/test_work_service_manifest_by_run.py` (9): proposed ignored, superseded ignored (also when it is the run asked for), run match beats a newer plan, `plan_id == run_id`, completed resolves, empty run keeps truck lookup with the status filter, no asset → no read, foreign tenant, resolver passes the run. Real query through `document_matcher`. |
| 3 (K7.4, freeze rule 8) | `FuelPlanDispatchService.dispatch(..., driver_id=None, notify=True)`. Explicit driver: `driver_repository.get(tenant, id)` must be `active` in the tenant, else 409 `DRIVER_UNAVAILABLE` `reason: board_driver_unavailable` (no driver id in details). `notify=False` skips only `send_assignment`. | `tests/unit/test_plan_dispatch_explicit_driver.py` (8): active ok with no truck lookup; inactive / foreign tenant / missing / blank → same refusal before any write; `notify=False` never touches the WS manager and the result carries `plan_id`, `run_id`, `truck_id`, `route_ids`, `order_ids`, `driver_id`; default path sends once with today's payload. |
| 4 (K8.3) | `FuelOrderRepository.relink_dispatched_assignment(...) -> "relinked" | "already_relinked" | "refused"`: one `atomic_update`, full from-link CAS (run, asset, driver), status must be `dispatched`, never writes status, `last_event_timestamp` strictly later than stored (handles a stored future stamp), mirrors to the relational row like the guarded upsert does. | `tests/unit/test_order_relink.py` (17); `tests/postgres/test_order_relink_postgres.py` (2): four concurrent relinks from one link → exactly one wins; a guarded driver write based on a pre-relink read raises `OrderChangedConcurrentlyError` and the relink survives. |
| 4b (a) freeze rule 9 | `record_checkin` persists via `atomic_update`; the transform re-locates the stop by `station_id`+`sequence` on the stored doc, refuses on superseded / missing / completed, counts `completed_stops` from the stored doc. Verdicts map to today's `ValueError("... not in 'dispatched' status (current: superseded)")`, `resource_not_found` (404), `stop_already_completed` (409). Pre-read checks unchanged. | `tests/unit/test_plan_execution_checkin_cas.py` (8): read→supersede→commit refused and stays superseded; read→renumber→commit 404 and renumbered stops survive; double check-in 409; interleaved double commits once; stored count used. `tests/postgres/test_checkin_amend_race.py` (6 runs): concurrent check-in and amend serialize, never both apply. |
| 4b freeze rule 11 (b) | New `merge_plan_fields` (`plan_execution_service.py`): `atomic_update` merge of only the given keys. Used by the check-in endpoint's `completed` write, `compute_estimated_cost`, `compute_actual_cost`. Fields and responses unchanged; a vanished plan raises `LookupError` (the endpoint still answers 500 as before). | In `test_plan_execution_checkin_cas.py`: endpoint reads, board revision commits, completion commits → `revision` and `assignments` survive, status `completed`; both cost writes keep concurrent fields. |
| 4b (b) freeze rule 10 | `transition_endpoints.py`: `apply_status_transition(..., guard_stored_state=True)`; `OrderChangedConcurrentlyError`/`OrderWriteDiscardedError` → 409 `ORDER_CHANGED_CONCURRENTLY` "The order changed. Refresh and try again.", `details: {order_id}`, not stored for idempotency. | `tests/unit/test_driver_transition_guards.py` (18, all through the real endpoint, `OrderService` and `FuelOrderRepository`): stale read after relink → 409 and relink survives, no event; same-key retry after a 409 runs again (403 when the order moved to another driver; 200 when it didn't). |
| 4b (c) + freeze rule 11 (a) | Gate 0 in `DriverTransitionGateStack.evaluate`, `in_transit` only, ahead of the four gates, records no outcome (existing outcome lists unchanged). `bp-` run → read `mvp_load_plans/{run}`; a same-tenant `source: dispatch_board` plan not `dispatched`/`completed` → 409 `BOARD_ROUTE_UPDATING`. Reader `None` or read error → 409 + WARNING (fails closed). Agent runs never read. `board_plan_reader` kwarg on the stack and `configure_transition_endpoints`; `bootstrap/driver.py` passes `es_service.get_document` (WARNING if absent). | Same file: draft/scheduled/superseded → 409; dispatched/completed → 200; agent run never read; read error → 409 + WARNING; `failed`/`delivered` never gated; non-board plan with `bp-` id passes; foreign-tenant plan ignored; stack without reader refuses `bp-` (409, WARNING) and allows agent runs; bootstrap wiring test asserts the configured stack's reader is `es_service.get_document`. |
| 4b (d), 5 codes | `errors/codes.py`: `ORDER_CHANGED_CONCURRENTLY`, `BOARD_ROUTE_UPDATING`, `DISPATCH_BOARD_DISABLED` (404), `DISPATCH_BOARD_READ_ONLY` (409), `DISPATCH_BOARD_MODE_UNAVAILABLE` (503), `BOARD_LANE_CONFLICT` (409), `BOARD_COMMAND_BLOCKED` (422), `BOARD_PUBLISH_IN_PROGRESS` (409), `BOARD_PUBLISH_NOT_READY` (409), `BOARD_UNDO_STALE` (409), `BOARD_OWNED_PLAN` (409). | Covered by the endpoint tests above and `test_driver_error_codes.py`. |
| 5 (K2.4) | `mvp_es_mappings.py`: `_BOARD_PLAN_FIELDS`, `_BOARD_ROUTE_FIELDS`, `_BOARD_RECOVERY_FIELDS` spread into both the create-time mappings and `MVP_ADDITIVE_MAPPING_UPDATES` (new `mvp_plan_executions` entry for the markers). New `fuel/services/dispatch_board_es_mappings.py` (`DISPATCH_BOARD_INDEX_MAPPINGS`: drafts with `order_ids` keyword, `service_date` date, `applied_commands` `enabled: false`; commands with `response`, `content_before`, `content_after` `enabled: false`; both with `created_at`/`updated_at`), registered in `_REGISTRIES`. | `tests/unit/test_mvp_mapping_board_fields.py` (9): K7.3a plan and route, every key path declared; board fields in additive updates with identical types. `tests/unit/test_dispatch_board_mappings.py` (6): registry, `unsearchable_fields` exact sets, lookup fields searchable. `test_mapping_timestamp_contract.py` passes with the new indices. |
| 5b (K7.6) | `GET /plans`: `must_not source=dispatch_board` by default; `?source=dispatch_board` lists only board plans (other values → 422). Approve, reject, replan: `_refuse_board_plan` → 409 `BOARD_OWNED_PLAN` "Manage this plan on the Dispatch Board" `details: {plan_id}` after the read, before any write (replan now reads the plan first; a read failure → 500 like other replan failures). `ExceptionReplanningAgent._load_plan_snapshot`: same `must_not` on plan and route queries. | `tests/unit/test_mvp_board_plan_guards.py` (9): list default / param / bad value; three 409s with zero applied writes and no dispatch or agent call; agent-plan reject and replan still work; agent snapshot ignores a newer board plan and route. |
| 5c (D8, K2.5) | `work_endpoints.get_work_service()` (returns the global, never raises); `driver_daily_reset.get_tenant_timezone = _get_tenant_timezone` (in `__all__`). | `tests/unit/test_board_accessors.py` (5). |
| 6 (R8.1) | `fuel/services/dispatch_validation.py`: `build_route_requirements`, `estimate_route_hours`, `resolve_stop_locations`, `lookup_product` (+ constants). `RoutePlanningAgent` methods delegate (passing its own class constants, so overrides still apply). `route_solver.recompute_etas = _recompute_etas`. Unused `canonicalize` import dropped from the agent. | `tests/unit/test_dispatch_validation_helpers.py` (18): goldens captured from the original methods before the change, asserted on both the module functions and the agent methods. |

Commands (cwd `.worktrees/dispatch-board/Runsheet-backend`, `PY=/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend/venv/bin/python`; the worktree has no venv of its own; `rootdir` is the worktree, so the worktree code is what ran):

- `$PY -m pytest --no-cov -q -p no:cacheprovider -o log_cli=false tests/unit/test_work_service_manifest_by_run.py tests/unit/test_driver_work_service.py tests/unit/test_driver_work_endpoints.py` → 49 passed
- `… tests/unit/test_plan_dispatch_explicit_driver.py tests/unit/test_plan_dispatch_service.py` → 37 passed
- `… tests/unit/test_order_relink.py` → 17 passed
- `… tests/unit/test_checkin_volume_boundary.py tests/unit/test_mvp_endpoints_auth_regression.py tests/integration/test_loading_plan_approve_endpoint.py` → 29 passed
- `… tests/unit/test_driver_transition_endpoint.py tests/unit/test_driver_transition_gate_stack.py tests/unit/test_order_guarded_transition.py tests/unit/test_bootstrap_driver_module.py tests/unit/test_driver_error_codes.py tests/unit/test_pod_transition_reconciler.py` → 211 passed
- `… tests/unit/test_driver_transition_guards.py` → 18 passed
- `… tests/unit/test_plan_execution_checkin_cas.py` → 8 passed
- `… tests/unit/test_mvp_mapping_board_fields.py tests/unit/test_dispatch_board_mappings.py tests/unit/test_mapping_timestamp_contract.py tests/unit/test_document_field_policy.py tests/unit/test_driver_index_dynamic_tightening.py` → 138 passed
- `… tests/unit/test_mvp_board_plan_guards.py` → 9 passed; `… tests/unit/test_mvp_endpoints.py tests/unit/test_exception_replanning_agent.py tests/unit/test_replan_diff_ready.py` → 79 passed
- `… tests/unit/test_board_accessors.py tests/unit/test_driver_daily_reset.py tests/unit/test_driver_work_endpoints.py` → 40 passed
- `… tests/unit/test_dispatch_validation_helpers.py tests/unit/test_route_planning*.py tests/property/test_route_stop_source_precedence_property.py tests/unit/test_route_solver*.py` → 223 passed
- Full suite, CI env: `REDIS_URL=redis://localhost:6379 JWT_SECRET=ci-test-jwt-secret JWT_ALGORITHM=HS256 ENVIRONMENT=test $PY -m pytest --no-cov -q -p no:cacheprovider -o log_cli=false` → **12781 passed, 233 skipped, 0 failed** (8 m 44 s). CI adds `--cov … -x`; coverage was not measured locally.
- Postgres suite, CI env: `ENVIRONMENT=test REDIS_URL=redis://localhost:6379/0 DATABASE_URL=postgresql+psycopg://runsheet:runsheet@localhost:5432/runsheet POSTGRES_TEST_URL=<same> $PY -m pytest tests/postgres -q --no-cov -p no:cacheprovider` → **228 passed, 0 skipped** (local `runsheet-postgres` container), incl. the 2 relink and 6 check-in race tests.
- Endpoint registry: `ENVIRONMENT=test REDIS_URL=redis://localhost:6379 JWT_SECRET=x $PY scripts/generate_endpoint_registry.py` (343 entries) then `git diff --stat -- docs/endpoint-registry.md` → no diff.

Existing tests touched (fakes only, assertions unchanged): `test_checkin_volume_boundary.py` `_FakeES` gained `atomic_update` (same id lookup as its `update_document`); `test_driver_transition_endpoint.py` `FakeOrderRepository.upsert_with_last_event_timestamp` accepts the guard kwargs and returns the stored doc in guarded form.

Driver app (no change): `driver-app/lib/work-api.ts` `queueOrderStatus` sends `in_transit` only through `enqueueMutation` + `drainQueue` (L57–68), except the `demoPreviewEnabled` branch (L46–55), which posts directly for the demo preview build. `lib/offline-queue.ts` L696: any 409 other than `INVALID_STATUS_TRANSITION` is `retry`, so `ORDER_CHANGED_CONCURRENTLY` and `BOARD_ROUTE_UPDATING` are retried automatically.

#### Decisions and notes

- Gate 0 records no `GateOutcome`, so `GateEvaluation.outcomes` and `GATE_ORDER` stay exactly as they are for every existing caller; it either raises or returns.
- A `bp-` run whose plan doc is missing, foreign-tenant, or not `source: dispatch_board` passes gate 0 (design K8.6 gates only board plans). Only a missing reader or a read error fails closed.
- `dispatch_board_drafts` map-shaped fields other than `applied_commands` (`lanes`, `order_index`, `acknowledged`, `dismissed_suggestions`, `publishes`) are `object` + `dynamic: true`, i.e. still searchable; the design names only `applied_commands` as non-indexed. Command-log `lanes` is a plain `object` so `term lanes.truck_id` works for history (R22.3).
- `relink_dispatched_assignment` also mirrors to the relational row (`mirror_current_state_upsert`), because no guarded upsert follows a relink the way one follows `claim_assignment`.
- Risk for the reviewer (design-literal, not changed): the driver endpoint's guarded write compares against `es_documents`, but its base read is `FuelOrderRepository.get`, which serves the relational projection when `COMMERCE_READ_FROM_POSTGRES` is on. Every order write path mirrors after commit, so they agree in practice; if a best-effort mirror write failed, that order's driver transitions would 409 (and be retried) until the next mirrored write. Reading the base doc with `get_current` would remove this, but it changes the read path the design specifies, so it's left for review.
- Pre-existing, not caused here: `tests/unit/test_bootstrap_driver_wiring.py::TestOrderServiceWiring::{test_exactly_one_invoice_subscriber_on_order_delivered,test_invoice_subscriber_uses_the_container_invoice_service}` fail when that file is run alone, on the base commit too (checked with `git stash`); they pass in the full suite.
- Disk: free space was 7.1 GB at start and fell to 3.3 GB during the spike. Cleared npm and Homebrew caches; after the spike deleted the worktree's `runsheet/node_modules`, `runsheet/.next`, the spike's test output and the Playwright browser cache (all regenerable) → 5.5 GB free. No Docker build was run. Phase 4 needs `npm ci` and `npx playwright install` again in the worktree, and more free disk before `npm run build` or any Docker build.

Open: the one manual iPad long-press check for criterion (c).

### Phase 1: Backend core
Not started.

### Phase 2: Publish and changes after publish
Not started.

### Phase 3: Realtime and suggestions
Not started.

### Phase 4: Frontend foundation
Not started.

### Phase 5: Board interactions
Not started.

### Phase 6: Publish, after-publish, suggestions, map, history UI
Not started.

### Phase 7: Responsive, performance, accessibility, E2E
Not started.

### Phase 8: Staging rollout
Not started.
