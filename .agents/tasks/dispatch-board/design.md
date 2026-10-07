# Dispatch Board: design (revision 4)

Main checkout `/Users/olukotunjosh/Downloads/Runsheet`, branch `production-readiness/go-live-blockers` at `ad72dde`. Paths are relative to the repo root unless a table says otherwise. Requirements are `requirements.md` revision 4 (R1–R25, N1–N7, D1–D8, Q1–Q16). Code evidence is in `research.md`; anything not in the codebase today is marked **NEW**. Revision 2 resolved every finding of `design-review.md` pass 1 (1 HIGH, 14 MEDIUM, 4 NIT). Revision 3 resolves every finding of pass 2 (0 HIGH, 5 MEDIUM, 3 NIT) and records freeze rules 7 and 8 for post-publish (K8.5). Revision 4 resolves every finding of pass 3 (0 HIGH, 2 MEDIUM, 5 NIT) and records freeze rules 9 and 10 (K8.5); responses to all three passes are at the end.

## Overview

The board is a React client over a small set of NEW backend services in `Runsheet-backend/fuel/services/`. The backend owns one draft document per tenant and service day. Dispatchers change it only through typed commands. Each command is validated by a NEW `DispatchValidationService` that wraps the existing validators, then committed with a single-document compare-and-set (`atomic_update`) that checks the lane versions the client expected. Nothing outside the draft changes until Publish. Publish turns each lane's loads into `mvp_load_plans` / `mvp_routes` documents and pushes them through the existing `LoadingPlanExecutor` (orders to `scheduled`) and `FuelPlanDispatchService` (orders to `dispatched`, driver notified). Changes after publish go through a NEW `BoardRedispatchService` that relinks dispatched orders with a guarded repository primitive and notifies the drivers who gain or lose work. A NEW tenant-scoped socket `/ws/dispatch-board` carries lane updates, presence and publish progress.

On the client, a NEW `components/dispatch-board/` tree renders lanes as rows with time on the x-axis. Drag and drop uses Pragmatic drag and drop behind a thin adapter folder, so the library can be swapped without touching board components. Every drag has a menu and a Place-mode equivalent that call the same command functions. Server state lives in a pure reducer; React 19 `useOptimistic` shows pending moves.

## Technology stack (locked on approval)

- **Backend:** Python 3.11, FastAPI, the existing document store (`persistence/document_store.py`, Postgres-backed, reached through `services/elasticsearch_service.py`), Pydantic v2 models, `pytest` + `hypothesis==6.151.4` (already pinned in `Runsheet-backend/requirements.txt`). No new backend dependency.
- **Frontend:** Next 15.5.22, React 19.1.0, TypeScript, Tailwind 4, `components/ui`, `lucide-react`, `@vis.gl/react-google-maps` (existing). Jest + React Testing Library + jsdom, Playwright, Biome.
- **New frontend dependencies (NEW, pinned exact after the T1 spike):** `@atlaskit/pragmatic-drag-and-drop@4.0.0`, `@atlaskit/pragmatic-drag-and-drop-hitbox@3.0.0`, `@atlaskit/pragmatic-drag-and-drop-auto-scroll@3.2.1`, `@atlaskit/pragmatic-drag-and-drop-live-region@2.1.0` (research 1.4, [S32]). Not `react-accessibility` or `react-drop-indicator` (they pull Emotion/Compiled). If the spike fails any of its four criteria, the fallback is `react-aria-components@1.21.1` used only inside `components/dispatch-board/dnd/`; the plan records which one landed.
- **No** state library, no virtualization library, no client analytics SDK, no node-graph library.

## Evidence from the code that shapes the design

- Order status cannot go backwards (`fuel/order_state_machine.py` L22–30: no `scheduled → confirmed`, `dispatched` only to `in_transit`/`failed`/`cancelled`). So planning must happen in a draft, and post-publish moves of a dispatched order are relinks, not transitions.
- `LoadingPlanExecutor.execute` (`fuel/services/loading_plan_executor.py` L396) takes `action_id: Optional[str]` and an explicit `mode`, and treats orders already linked to the plan's run and truck as applied (loading-plan-executor R4.2). Its idempotency key is `plan_id`, and a succeeded plan replays with no writes (R4.3). So the board can call it directly, and a published load that gains new tray orders needs a new `plan_id`.
- `FuelPlanDispatchService.dispatch(tenant_id, plan_doc, actor_user_id)` (L148) resolves the driver from `drivers_current.assigned_truck_id` (`_resolve_driver` L439) and fetches routes by `plan_id` + `truck_id` (`_fetch_routes` L417). The board needs an explicit driver parameter (D6).
- `PLAN_EXECUTION_LOCK.hold` (`persistence/plan_execution_lock.py` L145) is not reentrant and both the executor and dispatch take it internally. So board Publish must not wrap them in it; Publish serializes through its own lane claim on the draft instead. This corrects research R8/F26 ("Publish runs under `PLAN_EXECUTION_LOCK`").
- `atomic_update(index, doc_id, transform, upsert=)` (`persistence/document_store.py` L378) is a row-locked read-modify-write with a synchronous transform. Validators are async, so validation runs before the transform and the transform only compares versions and writes.
- The driver manifest resolves the newest plan for a truck in any status (`driver/services/work_service.py` `_fetch_loading_plan` L457). Board drafts must not be written as plans before Publish, and R25 must land before multi-load days.
- Route geometry helpers are pure, haversine-based (`Agents/support/route_solver.py`: `build_distance_matrix` L43, `check_sla_windows` L118, `optimize_route` L165, `_recompute_etas` L652). Stop coordinates come from `ship_to_lat`/`ship_to_lon` on the order, falling back to `fuel_stations` (`RoutePlanningAgent._resolve_stop_locations` L2380, `_query_station_locations` L2024).
- `OrderService.subscribe(event_name, handler)` (`fuel/services/order_service.py` L164) with `event_name = f"order.{new_status}"` (L599) is the in-process hook for order changes.
- The backend runs as one ECS task in staging (`scripts/staging_aws.sh` `--desired-count 1`), and WebSocket managers broadcast in-process (`websocket/base_ws_manager.py` L148). Board correctness must not depend on socket delivery; versions and refetch do.
- Overlay-style flags are four-state strings read with `FeatureFlagService.get_overlay_state` / `get_overlay_state_strict` (`ops/services/feature_flags.py` L257, L312), and a non-agent key already uses that store (`order_intake_pipeline`, `fuel/api/feature_flag_admin_endpoints.py` L51, router prefix `/api/ops/admin` L28). `get_overlay_state` returns `"disabled"` both when unset and on a Redis error; `get_overlay_state_strict` returns `None` when unset and raises on a Redis error.
- The driver app reads exact route and plan keys (added in revision 2). `DriverWorkService._fetch_route_plan` (`driver/services/work_service.py` L476) filters `run_id` + `truck_id` and sorts by `timestamp`; `_stop_entry` (L698) reads `sequence`, `station_id`, `eta`, `drop` (grade → litres) and resolves coordinates by matching `station_id` against `fuel_stations.station_id` or `customer_tanks.station_id|customer_tank_id` (L564–567); `_fetch_execution` (L497) matches `plan_id` and `route_id`; stop status is matched by `sequence` first, then `station_id` (L955–963). `FuelPlanDispatchService._resolve_orders` reads `stops[].order_ids` (L494). `mvp_routes`, `mvp_load_plans` and `mvp_plan_executions` declare `dynamic: strict` (`Agents/support/mvp_es_mappings.py` L130, L195, L485), but the Postgres document store keeps documents in a `jsonb` column and enforces nothing; `tests/unit/test_mapping_timestamp_contract.py` calls `dynamic: strict` "a statement of intent with no enforcement behind it". Mappings matter for two things only: the exact keys readers use, and the field policy. `persistence/document_field_policy.py` discovers mappings through `_REGISTRIES` (L75) and derives `unsearchable_fields(index)` (L168) from them; `MVP_ADDITIVE_MAPPING_UPDATES` has no consumers outside its file, and `bootstrap/fuel.py` registers no mappings.
- Orders carry no priority field. `fuel_orders_current` (`fuel/services/order_es_mappings.py`) has `call_type`, `delivery_window_start` and `delivery_window_end`; priority lives in `mvp_delivery_priorities` as a nested `priorities[]` list with `order_id`, `priority_score` and `priority_bucket` (`mvp_es_mappings.py` L81–100). The Postgres translator sorts ascending with NULLS LAST and appends a document-id tiebreak (`persistence/document_query.py` `build_order_by`, around L800).
- Nothing in plan execution records a terminal lift: `mvp_plan_executions` stop records have `station_id`, `sequence`, `status` (`pending`/`completed`), `planned_eta`, `actual_arrival`, quantities and check-in fields (`plan_execution_service.py` L93–117). The only observable departure signal is the driver moving an order to `in_transit` (`driver/services/order_transition_service.py` `GATED_TARGET_STATUSES` L106); `completed_stops` increments on check-in (L297).
- `ErrorCode.MISSING_IDEMPOTENCY_KEY` exists (`errors/codes.py` L181) and defaults to 400 (L481), so the board passes `status_code=422` explicitly.
- The executor refuses with `order_changed_since_plan` when an assignment's `product_code` or `station_id` differs from the order's `product_code` / `customer_id`, or when `customer_tank_id`, `gallons_requested` or `fill_to_full` differ from the passed snapshot (`loading_plan_executor.py` `_SNAPSHOT_FIELDS` L165, `_changed_field` L1081). The compartment loading agent writes `station_id = order.customer_id` on assignments (`compartment_loading_agent.py` L843) and canonical product codes in `fuel_grade` (L1976).
- `PlanExecutionService` (`Agents/support/plan_execution_service.py`) has `create_execution` and `record_checkin` and no update or cancel path. Execution statuses are `in_progress` and `completed`. `record_checkin` matches a stop on `station_id` **and** `sequence` (L231–236) and requires the plan to be `dispatched`. It reads the execution, edits `stops[target_idx]` in memory and persists `{stops, completed_stops, status, updated_at}` with `update_document` (L311–313), a shallow top-level merge with no compare (`persistence/document_store.py` L292–317). The only execution writers are `create_execution` (L124) and `record_checkin` (L312); the check-in endpoint (`mvp_endpoints.py` L1088–1129) takes no lock. So a board write to an execution is only safe if `record_checkin` also writes with compare-and-set (freeze rule 9).
- `atomic_update`'s transform returns `None` for "no write", and the call returns `(stored_document, applied)` (`document_store.py` L378–460), so a refusal inside a transform is observable without an exception.
- Driver status transitions are **not** compare-and-set. `POST /api/driver/orders/{order_id}/status` (`driver/api/transition_endpoints.py` L194–273) reads the order, runs the gate stack (several async reads), then calls `OrderService.apply_status_transition` without `guard_stored_state` (L265), so the write is `upsert_if_newer` of the whole order model built from the earlier read. A board relink that lands between that read and that write is overwritten with the old links. The guarded form exists and is used by the executor and dispatch (`guard_stored_state=True`, `order_service.py` L270–339 → `order_repository.py` `_guarded_upsert` L662–707, raising `OrderChangedConcurrentlyError` on a changed status or `last_event_timestamp`). The driver app's offline queue retries any 409 other than `INVALID_STATUS_TRANSITION` as transient (`driver-app/lib/offline-queue.ts` L676–677).
- The executor counts orders in `scheduled`, `dispatched`, `in_transit` or `delivered` that are linked to the plan's run and truck as applied, and `cancelled`/`failed` linked orders as settled (`loading_plan_executor.py` `_APPLIED_STATUSES` L160, `_LINKED_SETTLED_STATUSES` L162). Dispatch accepts `dispatched`/`in_transit` orders whose driver, asset and run match (`plan_dispatch_service.py` `_ACTIVE_STATUSES` L75, L586–601) and refuses any other status with `order_not_dispatchable` (L579). So once every order of a load is linked to its run, executor and dispatch can be completed forward whatever the driver does next, except for an order that became `delivered`, `failed` or `cancelled` before dispatch (K8.4 Recovery handles that one case).
- `FuelPlanDispatchService.dispatch` always calls `send_assignment` itself after its writes, including when every order was already dispatched (L384–402), with the payload `{plan_id, run_id, truck_id, route_ids, order_ids}`; every one of those fields is already on the returned `PlanDispatchResult` (L86–95), together with `driver_id`.
- `DriverWorkService.invalidate(tenant_id, order_id, *, assigned_run_id=None)` (L333) exists with no callers; the service is a module global built by `driver/api/work_endpoints.py` `configure_work_endpoints` (L78) with no public accessor.
- `GET /api/fuel/mvp/plans` (`Agents/support/mvp_endpoints.py` L752) lists every plan; `POST /plan/{plan_id}/approve|reject|replan` (L806, L906, L445) act on any plan id. `ExceptionReplanningAgent._load_plan_snapshot` (`Agents/overlay/exception_replanning_agent.py` L309) reads the newest `dispatched|in_transit` plan and route for the tenant with no source filter.
- `create_document(index, doc_id, document) -> bool` (`persistence/document_store.py` L241) is `INSERT … ON CONFLICT DO NOTHING`. The Postgres query translator's `term` matches a value inside a keyword array (`persistence/document_query.py` `_containment` L293), and `_source` filtering is applied to hits (L940, `document_store.py` L696).
- Session claims carry `tenant_id`, `roles`, `has_pii_access`, `driver_id` (`auth/supertokens_init.py` L337–347) and no display name; SuperTokens puts the user id in `sub`. `auth_users` (Postgres) has `email`, `tenant_id`, `st_user_id` (`auth/supertokens_init.py` L309–311, `auth/password_admin.py` L120).
- `TenantSettings` (`services/tenant_settings.py` L74) has no `timezone` attribute, so `_get_tenant_timezone(tenant_id, settings)` (`fuel/services/driver_daily_reset.py` L54) returns `America/Chicago` for every tenant today. `TenantContext.settings` already carries the tenant's `TenantSettings` (`ops/middleware/tenant_guard.py` L382–396).
- FastAPI's default `RequestValidationError` response echoes `input` (documented in `driver/api/pin_endpoints.py`), and `ErrorCode.VALIDATION_ERROR` defaults to 400 (`errors/codes.py`); `AppException(..., status_code=...)` overrides the default (`errors/exceptions.py` L37–69).
- `auth/router_guards.py` L30–33: where a feature-flag guard answers 404, the role check comes after it.

## Components and files

Backend (all under `Runsheet-backend/`):

| File | Status | Purpose |
|---|---|---|
| `fuel/services/dispatch_board_models.py` | NEW | Pydantic models: draft, lane, load, stop, check, command union, responses (K2, K4). |
| `fuel/services/dispatch_board_es_mappings.py` | NEW | Exports `DISPATCH_BOARD_INDEX_MAPPINGS = {"dispatch_board_drafts": …, "dispatch_board_commands": …}`. `applied_commands`, `content_before`, `content_after` and `response` are declared `enabled: false` (not queryable). Both declare `created_at` and `updated_at`, because `tests/unit/test_mapping_timestamp_contract.py` walks the same registries once they are listed. |
| `persistence/document_field_policy.py` | MODIFY | Add `("fuel.services.dispatch_board_es_mappings", "DISPATCH_BOARD_INDEX_MAPPINGS")` to `_REGISTRIES` (L75), so the field policy knows the new indices. |
| `fuel/services/dispatch_board_engine.py` | NEW | Pure functions: apply a command to lanes, best-fit insertion, lane content hash, inverse snapshots (K4, K6). No I/O. |
| `fuel/services/dispatch_board_eta.py` | NEW | Pure ETA and window math over `route_solver` helpers; promotes `_recompute_etas` to a public `recompute_etas` alias in `route_solver.py` (K3). |
| `fuel/services/dispatch_validation.py` | NEW | `DispatchValidationService` and `ValidationContext`; extracted `build_route_requirements` and `estimate_route_hours` (moved from `RoutePlanningAgent`, which then calls them) (K3). |
| `fuel/services/dispatch_board_service.py` | NEW | Snapshot read, validate, command handling, presence bookkeeping, history (K2, K4, K5). |
| `fuel/services/dispatch_board_publish.py` | NEW | `BoardPublishService` (preflight, dry run, group split, first-publish groups) and `BoardRedispatchService` (redispatch groups: every group that holds a dispatched order or a Modified lane) (K7, K8). |
| `fuel/services/dispatch_board_suggestions.py` | NEW | Suggestion read and `ReplanDiff`-shaped diff producer (K9). |
| `fuel/services/dispatch_board_ws_manager.py` | NEW | `DispatchBoardWSManager(BaseWSManager)` (K10). |
| `fuel/api/dispatch_board_endpoints.py` | NEW | Router `/api/fuel/board`, `roles_dependency("admin", "dispatcher")` (K11). |
| `fuel/api/feature_flag_admin_endpoints.py` | MODIFY | Add GET/POST `dispatch-board` flag endpoints mirroring the order-intake pair (K13). |
| `fuel/services/plan_dispatch_service.py` | MODIFY | `dispatch(..., driver_id: Optional[str] = None, notify: bool = True)` (K7.4). |
| `fuel/order_repository.py` | MODIFY | NEW `relink_dispatched_assignment` CAS primitive (K8.3). |
| `Agents/support/plan_execution_service.py` | MODIFY | `record_checkin` persists through `atomic_update` with a transform that re-locates the stop and refuses on a superseded execution, a missing stop or a completed stop (freeze rule 9, K8.6). Errors and responses unchanged. |
| `driver/api/transition_endpoints.py` | MODIFY | Pass `guard_stored_state=True` to `apply_status_transition`; map `OrderChangedConcurrentlyError` / `OrderWriteDiscardedError` to 409 `ORDER_CHANGED_CONCURRENTLY` (freeze rule 10, K8.6). |
| `driver/services/order_transition_service.py` | MODIFY | Gate 0 "board plan dispatched" for `in_transit` on `bp-` runs → 409 `BOARD_ROUTE_UPDATING` (freeze rule 10, K8.6). |
| `driver/services/work_service.py` | MODIFY | `_fetch_loading_plan` by run and status (R25, K12). |
| `driver/api/work_endpoints.py` | MODIFY | NEW public `get_work_service() -> Optional[DriverWorkService]` (returns the module global, no raise) so the board can call `invalidate` (K8.4). |
| `Agents/support/mvp_endpoints.py` | MODIFY | `GET /plans` hides board plans unless `?source=dispatch_board`; approve, reject and replan refuse board plans with 409 `BOARD_OWNED_PLAN` (K7.6). |
| `Agents/overlay/exception_replanning_agent.py` | MODIFY | `_load_plan_snapshot` plan and route queries add `must_not term source=dispatch_board` (K7.6). |
| `fuel/services/driver_daily_reset.py` | MODIFY | Public alias `get_tenant_timezone = _get_tenant_timezone` (K2.5). |
| `Agents/support/mvp_es_mappings.py` | MODIFY | Additive fields on `mvp_load_plans` and `mvp_routes` (K2.4). |
| `Agents/support/route_solver.py` | MODIFY | Public alias `recompute_etas = _recompute_etas`. |
| `Agents/overlay/route_planning_agent.py` | MODIFY | Call the extracted helpers; behaviour unchanged. |
| `errors/codes.py` | MODIFY | NEW codes: `DISPATCH_BOARD_DISABLED` (404), `DISPATCH_BOARD_READ_ONLY` (409), `DISPATCH_BOARD_MODE_UNAVAILABLE` (503), `BOARD_LANE_CONFLICT` (409), `BOARD_COMMAND_BLOCKED` (422), `BOARD_PUBLISH_IN_PROGRESS` (409), `BOARD_PUBLISH_NOT_READY` (409), `BOARD_UNDO_STALE` (409), `BOARD_OWNED_PLAN` (409), `ORDER_CHANGED_CONCURRENTLY` (409) and `BOARD_ROUTE_UPDATING` (409), both for driver transitions (K8.6). |
| `bootstrap/agents.py` | MODIFY | Build the board services after `plan_dispatch_service` and `loading_plan_executor` (L1027–1073) and put them on the container. |
| `bootstrap/routing.py` | MODIFY | Include the board router. |
| `bootstrap/websockets.py` | MODIFY | `/ws/dispatch-board` endpoint with a presence handler. |
| `bootstrap/agents.py` | MODIFY | Subscribe the board's order listener on `order.*` statuses (K10.4), in the same block that builds the board services (one place, not `bootstrap/driver.py`). Pass `BoardRedispatchService` a `work_cache_invalidator` that calls `work_endpoints.get_work_service()` at call time (K8.4). |

Repo root:

| File | Status | Purpose |
|---|---|---|
| `docs/endpoint-registry.md` | REGENERATE | `Runsheet-backend/scripts/generate_endpoint_registry.py` writes it (`DEFAULT_OUTPUT_PATH`); CI diffs `../docs/endpoint-registry.md` from `Runsheet-backend`. |

Frontend (all under `runsheet/src/`):

| File | Status | Purpose |
|---|---|---|
| `components/DispatchPage.tsx` | MODIFY | Board tab, default when active, `?tab=` deep link (R1). |
| `components/dispatch-board/DispatchBoard.tsx` | NEW | Container: snapshot load, socket, state, layout. |
| `components/dispatch-board/state/boardReducer.ts` | NEW | Pure reducer for server state, pending commands, selection, place mode. |
| `components/dispatch-board/state/useBoardCommands.ts` | NEW | Command senders shared by drag, menu, place mode, keyboard; `useOptimistic`. |
| `components/dispatch-board/state/precheck.ts` | NEW | Optimistic pre-check from snapshot data (K3.8). |
| `components/dispatch-board/state/announce.ts` | NEW | Announcement text builders. |
| `components/dispatch-board/dnd/` | NEW | Pragmatic adapter: `useDraggableCard`, `useLaneDropTarget`, `useSlotDropTarget`, `useDriverSlotTarget`, `useBoardAutoScroll`, `dragData.ts` (typed payloads). |
| `components/dispatch-board/BoardToolbar.tsx`, `PresenceBar.tsx`, `FilterChips.tsx`, `ShiftPicker.tsx` | NEW | Toolbar row. |
| `components/dispatch-board/trays/OrderTray.tsx`, `DriverTray.tsx`, `TruckTray.tsx`, `OrderCard.tsx`, `DriverChip.tsx` | NEW | Trays. |
| `components/dispatch-board/grid/BoardGrid.tsx`, `TimeAxis.tsx`, `Lane.tsx`, `LaneHeader.tsx`, `LoadBlock.tsx`, `StopCard.tsx`, `InsertionIndicator.tsx`, `SuggestionGhost.tsx`, `ReassignShelf.tsx` | NEW | Lanes. |
| `components/dispatch-board/CompartmentGauge.tsx`, `CheckChip.tsx` | NEW | Shared visuals. |
| `components/dispatch-board/drawer/DetailDrawer.tsx` (+ `ChecksTab`, `CompartmentsTab`, `MapTab`, `HistoryTab`, `SuggestionTab`) | NEW | Right drawer. |
| `components/dispatch-board/dialogs/PublishDialog.tsx`, `AssignToMenu.tsx`, `ShortcutHelpDialog.tsx`, `OverrideReasonPopover.tsx` | NEW | Dialogs and menus. `PublishDialog` renders the publish dry run (K7.1). |
| `components/dispatch-board/BoardLiveRegion.tsx` | NEW | Polite + assertive regions. |
| `components/dispatch-board/LaneRouteMap.tsx` | NEW | Map, built like `components/ops/OperationsMap.tsx`. |
| `components/ops/ReplanDiffBody.tsx` | NEW (extracted) | Move `ReplanDiffBody` (`FuelDistributionPage.tsx` L1483) here; `FuelDistributionPage` imports it. No behaviour change. |
| `services/dispatchBoardApi.ts` | NEW | Client using `fetchWithTimeout` and `apiErrorFromResponse`. |
| `hooks/useDispatchBoardSocket.ts` | NEW | On `useWebSocket`. |
| `package.json`, lockfile | MODIFY | Four pinned dependencies. |
| `e2e/dispatch-board.spec.ts` | NEW | Playwright drags. |

Driver app: no change (R12.7).

## Key decisions

### K1. Shape and orientation (D1, R2)

Rows, one per truck, time on the x-axis; a fixed 280 px lane header column; a 320 px left tray (collapsible to 48 px); a 400 px right drawer that overlays below 1440 px. Timeline zoom maps the selected shift window to the scroll width at 120 px/hour (Comfortable) or 80 px/hour (Compact); Sequence zoom places cards at a fixed 160/120 px pitch. A load renders as a block: terminal chip, then stop cards, then a return marker. Lanes have fixed heights (Comfortable 104 px, Compact 72 px), which makes vertical virtualization a computation, not a library (K15). Node-graph alternatives were rejected in research 1.8; columns in Part 2.

### K2. Data model

#### K2.1 Draft document (`dispatch_board_drafts`, NEW)

Doc id `"{tenant_id}:{service_date}"`. One per tenant and service day (D5). Written only through `atomic_update`.

```python
class BoardDraft(BaseModel, extra="forbid"):
    tenant_id: str
    service_date: date                 # tenant-local calendar date
    timezone: str                      # IANA, captured at creation
    draft_version: int                 # +1 per committed command
    lanes: dict[str, Lane]             # key: truck_id
    order_index: dict[str, str]        # order_id -> truck_id (derived, kept in sync by the engine)
    order_ids: list[str]               # == keys(order_index); keyword-indexed for listener and cross-day lookups
    acknowledged: dict[str, WarningAck]  # warning_id -> ack
    applied_commands: dict[str, AppliedCommand]  # client_command_id -> {draft_version, payload_hash, at}; last 500, pruned > 24 h (K4.5)
    dismissed_suggestions: dict[str, Dismissal]  # plan_id -> {actor_user_id, at} (K9)
    publishes: dict[str, str]          # client_request_id -> publish_id; last 20 (K7.5)
    created_at: datetime; updated_at: datetime

class Lane(BaseModel, extra="forbid"):
    truck_id: str
    version: int                       # +1 whenever lane content, checks or publish state change
    driver_id: str | None              # shift-scoped pairing (D6)
    loads: list[Load]                  # time order
    shelf: list[str]                   # dispatched order_ids awaiting placement (R13.5)
    checks: list[Check]                # last validation of the whole lane
    checks_computed_at: datetime | None
    checks_stale: bool                 # set by order/compliance listeners (K10.4)
    publish: LanePublish

class Load(BaseModel, extra="forbid"):
    load_id: str                       # uuid4 hex, stable for the life of the load
    shift_id: str                      # "day" | "night" | "all" (Q2 presets)
    terminal_id: str | None
    planned_start: datetime | None     # tenant-local, from ETA calc
    stops: list[Stop]
    allocation_overrides: dict[str, list[CompartmentShare]]   # order_id -> shares
    allocations: list[Allocation]      # computed by the solver at commit
    source: Literal["dispatcher", "suggestion"]
    suggestion_id: str | None

class Stop(BaseModel, extra="forbid"):
    order_id: str
    snapshot: OrderSnapshot            # product_code, customer_id, customer_tank_id, gallons_requested, fill_to_full, window, call_type, status at insert
    location: LatLon | None
    eta: datetime | None

class LanePublish(BaseModel, extra="forbid"):
    state: Literal["draft", "publishing", "published", "failed"]
    attempt_id: str | None; lease_until: datetime | None
    published_version: int | None      # lane.version that was published
    published_content: LaneContent | None   # driver_id + loads, for Discard and diff
    published_hash: str | None
    plans: dict[str, PublishedPlan]    # load_id -> {plan_id, route_id, run_id, revision}
    last_result: PublishResult | None
    attempt: RedispatchAttempt | None  # set by the claim of a redispatch group; cleared at finalize or after rollback (K8.4)

class RedispatchAttempt(BaseModel, extra="forbid"):
    attempt_id: str
    group_truck_ids: list[str]         # every lane of the group; a Retry reclaims all of them
    phase: Literal["claimed", "retire", "stage_relink", "apply", "amend", "notify", "finalize"]  # last phase started
    loads: dict[str, AttemptLoad]      # load_id -> {class, plan_id, route_id, run_id, revision}; fixed at claim
    relinks: list[AttemptRelink]       # {order_id, from: Link, to: Link}; Link = {run_id, truck_id, driver_id}; fixed at claim
    notify_baseline: dict[str, list[str]]  # driver_id -> order_ids assigned before the attempt (from published_content)
    recovery: Literal["none", "rollback", "forward"]   # K8.4 Recovery; "rollback" only while a failed rollback awaits Retry
```

`attempt` is written in the same `atomic_update` as the claim, on every lane of the group (the same object on each), so it is fixed for the life of the attempt. A lane whose `attempt` is set and whose state is `failed` (or `publishing` with an expired lease) is **in recovery**: commands on it are refused with 409 `BOARD_PUBLISH_IN_PROGRESS` and `reason: "recovery_pending"`, exactly like a `publishing` lane (K4.2 step 6 and the transform check), and the only action is Retry (K8.4 Recovery).

A lane is **Modified** when `publish.state == "published"` and `content_hash(lane) != publish.published_hash`. `content_hash` (engine) is a SHA-256 of the canonical JSON of `driver_id`, `loads` (ids, stops order, terminal, overrides, shift) and `shelf`; it excludes checks, versions, ETAs and publish state.

Order exclusivity (invariant I1): an order id appears at most once across all lanes' stops and shelves of a draft. `order_index` makes this O(1) and is rebuilt and asserted by the engine on every apply.

Volumes are litres in storage (`gallons_requested` stays as the order carries it; allocations in litres as the solver uses). The API returns gallons alongside litres for display, converted with the same factor as `services/fuelApi.ts` `litersToGallons`.

Delivery windows live only on the draft (`Stop.snapshot.window`), never on route documents (the route shape is fixed by its readers, K7.3a).

#### K2.2 Command log (`dispatch_board_commands`, NEW)

Append-only, written only with `create_document` (insert-if-absent), never `index_document`, so no write can replace an existing record. Two doc id forms:

- Committed command: `"{tenant_id}:{client_command_id}"`. Exactly one can exist per command id. It stores the full response the client received, so a replay returns it verbatim (K4.5).
- Refused command (blocked or conflict): `"{tenant_id}:{client_command_id}:refused:{uuid4 hex}"`. A refused attempt never occupies the committed id, so a refused duplicate racing a committed one can't take its place (review finding 9).

Fields: `tenant_id`, `service_date`, `command_id`, `type`, `payload` (validated model dump), `payload_hash` (SHA-256 of canonical JSON), `actor_user_id`, `actor_name` (resolved at write time, K17.1), `input_modality` (`drag|menu|place|keyboard|suggestion|system`), `result` (`committed|blocked|conflict`), `response` (committed only), `lanes` (`truck_id`, `version_before`, `version_after`, `hash_before`, `hash_after`), `content_before` and `content_after` for touched lanes (committed only; for undo, K6), `checks_summary` (worst outcome and reason codes per lane), `overrides` (warning ids + reasons), `created_at`. Retention follows the tenant's document store retention; no TTL is added.

#### K2.3 Check model

```python
class Check(BaseModel, extra="forbid"):
    check: CheckId                    # see K3.2 catalogue
    outcome: Literal["pass", "warn", "block", "info"]
    reason_code: str                  # snake_case, stable
    message: str                      # fixed template + ids/numbers only
    source: str                       # validator name, e.g. "DriverQualificationService"
    scope: Scope                      # {truck_id, load_id?, order_id?}
    warning_id: str | None            # for warn only: sha256(check, reason_code, scope, key value)[:16]
    fix_link: FixLink | None          # {kind: driver|asset|order|hos_override|compartment, id}
```

`warning_id` is deterministic, so a warning that persists across commands keeps its acknowledgement, and a changed condition (new reason code or key value such as minutes late bucket) yields a new id and reopens it (R9.7).

#### K2.4 Additive plan and route fields (MODIFY `Agents/support/mvp_es_mappings.py`)

`mvp_load_plans`: `source` (keyword: `agent`|`dispatch_board`), `board_draft_id` (keyword), `board_load_id` (keyword), `service_date` (date), `shift_id` (keyword), `load_seq` (integer), `driver_id` (keyword), `revision` (integer), `supersedes_plan_id` (keyword), `superseded_by_plan_id` (keyword). `mvp_routes`: `source`, `board_load_id`, `service_date`, `revision`, `supersedes_route_id` (keyword). On all three of `mvp_load_plans`, `mvp_routes` and `mvp_plan_executions` (revision 4, freeze rule 10): `superseded_by_attempt` (keyword; the attempt that retired the document), `status_before_retire` (keyword; restored by rollback) and `created_by_attempt` (keyword; the attempt that staged the document, used by rollback to find what to retire). Every field is declared in both the create-time mapping (`MVP_LOAD_PLANS_MAPPING`, `MVP_ROUTES_MAPPING`) and `MVP_ADDITIVE_MAPPING_UPDATES` (L577), the same way `product_code` and `window_misses` were added. The store doesn't enforce `dynamic: strict` (Evidence); the fields are declared by convention, so the mapping stays the reference for the exact keys, and for the field policy, which reads the create-time mappings through `_REGISTRIES`. New status value `superseded` on plans, routes and executions (status is a keyword; no mapping change). Agent-written plans omit the fields; readers treat a missing `source` as `agent`.

A mapping parity test (task 5) builds one board plan and one board route exactly as K7.3a and asserts every key path is declared in both mapping dicts. A field-policy test asserts `unsearchable_fields("dispatch_board_drafts")` contains `applied_commands` and `unsearchable_fields("dispatch_board_commands")` contains `response`, `content_before` and `content_after`.

#### K2.5 Tenant time zone

The board resolves the zone as `get_tenant_timezone(tenant_id, tenant_context.settings)` (NEW public alias of `fuel/services/driver_daily_reset.py` `_get_tenant_timezone`). `TenantSettings` has no `timezone` attribute today, so every tenant resolves to `America/Chicago` until one is added; the board then picks it up with no change. The zone is captured on the draft at creation (`BoardDraft.timezone`, E11). Background paths with no request (the order listener, K10.4) use `get_tenant_timezone(tenant_id, await tenant_settings_service.get(tenant_id))`. Test: default resolves to `America/Chicago`; a settings stub with `timezone="America/Denver"` is honoured.

### K3. Validation (R8, R9)

#### K3.1 Service shape

```python
class DispatchValidationService:
    async def build_context(self, tenant_id, service_date, *, truck_ids, driver_ids, order_ids,
                            fresh: bool = False) -> ValidationContext
    async def validate_lane(self, ctx, lane: Lane, *, focus: Scope | None = None) -> list[Check]
    async def validate_candidates(self, ctx, draft, item: DragItem, truck_ids: list[str]) -> dict[str, CandidateResult]
```

`ValidationContext` holds everything the checks need, fetched in parallel with `asyncio.gather`: order docs (`FuelOrderRepository.get_current`), driver docs, qualification results (`DriverQualificationService.is_dispatch_eligible`), HOS (`HOSAdvisoryService.resolve` + `gate_verdict`), asset certification (`AssetCertificationService.is_dispatch_eligible`), compartments and compartment states (the same reads as `listTruckCompartments` server-side), tenant compatibility rules (`load_tenant_compatibility_rules`), terminal wait and contract lift (`terminal_wait_resolver`, `contract_lift_service`), stop locations. Qualification, HOS, certification, compartment state and rules are cached in-process for 30 s per `(tenant_id, key)` (`TTLCache`, a 40-line NEW helper in `dispatch_validation.py`, no library). `fresh=True` bypasses the cache; Publish always uses it (K7.2). Each fetch has a 2 s timeout; a timeout or exception marks that source unavailable for this context (K3.4) and logs WARNING once per context with the source name and exception type only.

All I/O happens in `build_context`; `validate_lane` only reads the context (it is `async` for interface stability but awaits nothing). This keeps per-lane validation pure and property-testable (P3).

`validate_candidates` applies the item to each candidate lane with the engine (best-fit position when no position is given), then validates each proposed lane. It returns per truck `{outcome, worst_checks[≤3], preview: {fill_by_compartment, insertion_index, eta_delta_minutes}}`.

#### K3.2 Check catalogue

| Check id | Source (existing) | Block | Warn | Info |
|---|---|---|---|---|
| `order_state` | `FuelOrderRepository.get_current`, `is_terminal_status`, `on_hold`, other drafts (K5.1) | terminal, `on_hold`, `order_committed_elsewhere` (linked to a run that is not this lane's published plan), `order_not_found`, `order_identity_changed` (`product_code`, `customer_id` or `customer_tank_id` differs from the stop snapshot; fix: remove and re-add), `order_on_other_day` (the order is on another service day's draft; message names the date) | — | `order_quantity_changed` (`gallons_requested` or `fill_to_full` differs; allocations are recomputed from the current order, K7.2) |
| `delivery_window` | `assert_window_present_for_transition(order, "scheduled")`, `dispatch_board_eta` | `missing_delivery_window` | `eta_after_window` (minutes late) | `early_arrival` |
| `driver_qualification` | `DriverQualificationService.is_dispatch_eligible(tenant_id, driver_id, build_route_requirements(load assignments))` | each `DriverEligibility.reasons` entry (`driver_inactive`, `cdl_expired`, `medical_expired`, `missing_hazmat`, `missing_tanker`, `cdl_class_below`), `check_unavailable` | — | `expires_within_7_days` |
| `driver_pairing` | engine | `driver_double_booked` (same driver on two lanes with overlapping load windows), `driver_not_in_tenant` | `no_driver` (lane has loads, no driver) | — |
| `hos` | `HOSAdvisoryService.gate_verdict`; `HOSChecker.is_eligible(driver_id, drive_h, total_h, exemption)` with hours from `dispatch_board_eta` (solver distances; falls back to extracted `estimate_route_hours`) | `hos_gate_blocked` (verdict `blocked`, no active `HOSGateOverride`) | `hos_projected_over` (verdict `skipped` or advisory), `check_unavailable` | `hos_remaining` |
| `asset_certification` | `AssetCertificationService.is_dispatch_eligible(tenant_id, truck_id)` | each reason, `check_unavailable` | — | `expires_within_7_days` |
| `compartment_fit` | `compartment_accepts`, `segregation_key`, `check_feasibility`, `optimize_loading_plan` | `no_compatible_compartments`, `capacity_shortfall`, `total_overage`, `weight_exceeded`, `override_invalid` (dispatcher allocation breaks a rule) | `below_min_drop` | `fill_percent` |
| `compartment_compatibility` | `check_compatibility` per compartment vs its current state | `compatibility_blocked` | `requires_cleaning` | — |
| `dyed_diesel` | `DyedDieselEnforcer.validate_load_plan` over the proposed load | each violation, `check_unavailable` | — | — |
| `schedule` | engine + ETA | — | `load_overlap` (estimated end of load n after start of load n+1), `outside_shift`, `eta_unavailable` | — |
| `terminal_supply` | `terminal_wait_resolver`, `contract_lift_service` | — | `long_terminal_wait` (> 45 min), `contract_lift_limit` (≥ 90 % used), `check_unavailable` | `terminal_unset`, `better_terminal` |
| `post_publish` | engine + order status + plan execution | `load_started`, `stop_pinned`, `unplaced_dispatched_order` (publish only) | — | — |
| `suggestion` | K9 | — | — | `suggestion_disagrees`, `better_slot` |

**HOS applies to today only (review finding 3).** `HOSAdvisoryService.gate_verdict` and `resolve` describe the driver's reading now. When `service_date == today` in the tenant zone (K2.5), the `hos` row runs as listed. When `service_date > today`, the `hos` check returns one `info hos_not_projected` ("Hours of service are checked on the day") with no gate or resolve call, the snapshot omits `hos` figures for that day's drivers, and the lane body draws no 14/11-hour shading (R2.9 applies to today). Publish preflight on a future day therefore cannot be blocked by today's HOS state. Projecting tomorrow's hours (34-hour restart, off-duty recovery) is backlog B9. Past days are read-only and show the stored checks. Test: a future-day lane with a currently `blocked` driver commits and shows `hos_not_projected`; the same lane on today blocks with `hos_gate_blocked`.

`HOSChecker.is_eligible` takes no `tenant_id` (research 4.2). The service only passes driver ids that were read from the tenant's driver repository in `build_context`, so a foreign id cannot reach it. Route requirements use the extracted `build_route_requirements` so the agent and the board agree.

#### K3.3 Tier rule

`outcome(lane) = max(block > warn > info > pass)` over its checks. Commands commit when no check is `block` (R9.1–9.2). `block` has no override path for any role (R23.4); the only lift for HOS is the existing `HOSGateOverride`, which changes the verdict input, not the board outcome.

#### K3.4 Unavailable sources (R9.5, Q5)

If a source is unavailable, its checks return `check_unavailable` with outcome `block` for `driver_qualification`, `asset_certification`, `dyed_diesel`, and `warn` for the rest. The snapshot carries `degraded_sources: [..]` for the banner (R21.6).

#### K3.5 Timing (R8.3–8.6, N1)

- Drag start: the client calls `POST /validate` with `{item, candidates: visible truck ids (≤ 60)}`. One `build_context` covers all candidates (drivers and trucks of those lanes plus the item's orders). Budget: context ≤ 350 ms warm cache, per-lane validation ≤ 2 ms × 60.
- Hover over a position: debounced 150 ms, `{item, candidates: [truck_id], position: {load_id, index}}`. Client cancels the previous request with `AbortController`.
- Drop: the command endpoint rebuilds the context (cache allowed) and re-validates; the client's earlier answers are never trusted.
- Client cache: last 20 `(item key, truck_id, lane.version, position)` answers.

#### K3.6 ETAs (`dispatch_board_eta.py`)

Start location: the load's terminal coordinates when `terminal_id` is set and the terminal has coordinates; else the previous load's last stop; else the truck's last known position from the fleet feed; else none (ETA unavailable → Sequence layout, `eta_unavailable` warn). Speed 40 km/h default (the solver's default), stop service time 30 min, terminal lift 45 min, return leg included. `best_insertion_index(stops, new_stop)` evaluates every position (n ≤ 30) and picks the least added time that does not create `eta_after_window`; ties keep the earlier index; if every position is late, it picks least lateness. Locations are resolved once per stop at insert time from `ship_to_lat/lon`, else `fuel_stations` (extracted from `_resolve_stop_locations`), and stored on the stop. No geocoding on the hot path; an order without coordinates gets `location: null`.

#### K3.7 Shadow mode and past days

Validation runs in every non-disabled mode. Commands and Publish are refused in `shadow` and for past service days.

#### K3.8 Client pre-check (D2)

`precheck.ts` uses only snapshot fields the server computed: per compartment `accepts_products: string[]`, `capacity_l`, `state`; per lane `remaining_capacity_by_segregation_key`; per driver `eligible_for: {tanker, hazmat, cdl_class}`; per order `draggable` and `block_reason`. It returns `likely_block(reason)` or `unknown`, never `pass`. It colours a target only until the server answer arrives and is discarded when it does. It contains no rule logic of its own (no product compatibility tables), so it cannot drift from the backend.

### K4. Commands and concurrency (R5–R7, R10, R14)

#### K4.1 Command union

All commands carry `client_command_id` (uuid4, client generated), `expected_lane_versions: dict[truck_id, int]` for every lane the command touches (including source lanes of moved stops), `input_modality`.

| Type | Payload | Lanes touched |
|---|---|---|
| `add_lane` | `truck_id` | that lane (expected version 0 = absent) |
| `remove_lane` | `truck_id` | that lane; stops return to tray. Only for lanes never published; a lane with `publish.published_version` set (Published, Modified, Failed after a prior publish) is refused with 422 `VALIDATION_ERROR reason: lane_published` and the UI disables the action with "Move or reassign this truck's stops first" (R7.5, R13.5) |
| `pair_driver` | `truck_id`, `driver_id \| null` | that lane + the lane the driver leaves. Refused with `block load_started` when the lane has a started load (Q12) |
| `assign_orders` | `order_ids[1..25]`, `truck_id`, `target: {load_id \| "new", index \| null}` | target lane (+ source lanes if any order is already on the board: it becomes a move) |
| `move_stops` | `order_ids[1..25]`, `truck_id`, `target` | source lanes + target lane |
| `unassign_orders` | `order_ids[1..25]` | source lanes |
| `move_load` | `load_id`, `truck_id`, `index` | source + target lanes |
| `set_terminal` | `load_id`, `terminal_id \| null` | lane |
| `set_allocation` | `load_id`, `order_id`, `shares: [{compartment_id, liters}] \| null` (null clears override) | lane |
| `set_load_shift` | `load_id`, `shift_id` | lane |
| `acknowledge_warning` | `truck_id`, `warning_id`, `reason (3..500)` | lane (version not required; acks don't conflict, K4.4) |
| `accept_suggestion` | `suggestion_id`, `load_ids?` (subset) | target lanes |
| `discard_lane_changes` | `truck_id` | lane (Modified only) |
| `revert` / `reapply` | `target_command_id` | lanes of the target command (K6) |

`index: null` means best fit (R5.1). `"new"` creates a load appended in time order.

#### K4.2 Handling sequence (`DispatchBoardService.handle_command`)

1. Auth and tenant from `TenantContext`; flag-mode guard then role guard at the route (K11.1). Body parsed with `Model.model_validate` inside the handler (External input validation).
2. Mode (strict read, review finding 4):

   ```python
   try:
       state = await flags.get_overlay_state_strict("dispatch_board", tenant_id)
   except Exception:
       raise AppException(ErrorCode.DISPATCH_BOARD_MODE_UNAVAILABLE, "Dispatch Board is unavailable. Retry shortly.", status_code=503)
   state = state or "disabled"          # unset means disabled (K13), not unreadable
   ```

   `disabled` → 404 `DISPATCH_BOARD_DISABLED`; `shadow` → 409 `DISPATCH_BOARD_READ_ONLY`. Past service day → 409 `DISPATCH_BOARD_READ_ONLY` with `reason: past_service_day`.
3. Idempotency fast path: `get_document("dispatch_board_commands", "{tenant}:{client_command_id}")`. If present with the same `payload_hash`, return its stored `response` (200) without re-applying; with a different hash → 409 `IDEMPOTENCY_CONFLICT` (existing code).
4. Read the draft (`get_document`; absent → empty draft, version 0). If `client_command_id` is in `draft.applied_commands`, go to step 11's replay branch (crash between commit and log, K4.5).
5. Version pre-check: if any `expected_lane_versions[t] != draft.lanes[t].version` → 409 `BOARD_LANE_CONFLICT` with the current lanes (cheap early exit).
6. Publishing check: any touched lane in `publishing` → 409 `BOARD_PUBLISH_IN_PROGRESS`; any touched lane in recovery (K2.1) → the same code with `reason: "recovery_pending"`.
7. `ctx = validation.build_context(tenant_id, service_date, truck_ids=touched lanes, driver_ids=drivers of touched lanes plus the command's driver, order_ids=command orders plus every order on touched lanes)`. This is the only I/O before the commit (review finding 13; there is no `ctx_light`).
8. `proposed = engine.apply(draft, command, ctx)`. The engine reads the context (locations, compartments, ETAs inputs, solver feasibility for best fit, K4.3) and does no I/O. Engine errors (unknown order, order already on another lane with a stale view, index out of range, limit exceeded) → 422 `VALIDATION_ERROR` with `reason`.
9. `checks = validation.validate_lane(ctx, lane)` for each touched lane in `proposed`, with the same `ctx`. If any `block`: write a refused log doc (K2.2) and return 422 `BOARD_COMMAND_BLOCKED` with the checks; draft unchanged.
10. Commit: `atomic_update(drafts, doc_id, transform)`. The transform refuses (returns `None`, no write) when `client_command_id` is already in `applied_commands` (`already_applied`), any expected lane version moved, a touched lane is `publishing`, or a cross-lane rule fails (below); the handler maps `already_applied` to step 11's replay branch and the rest to 409 `BOARD_LANE_CONFLICT` with a `reason`. Otherwise it writes the proposed touched lanes with `version + 1`, new checks, `checks_computed_at`, `order_index`, `order_ids`, `draft_version + 1`, and `applied_commands[client_command_id] = {draft_version, payload_hash, at}` (pruned to 500 entries, none older than 24 h).
11. Command log: `create_document` with the committed id and the response. `True` → done. `False` → a concurrent duplicate committed first is impossible (the transform's `applied_commands` check lets only one commit), so `False` means this is a retry whose log already landed; read it and return its `response`. **Replay branch** (from steps 4 or 10): if `applied_commands[id].payload_hash` differs from this request's hash → 409 `IDEMPOTENCY_CONFLICT`; else read the committed log doc, retrying 3 × 100 ms; return its `response` if found; otherwise return 200 with the current touched lanes and `already_applied: true`. If the log write itself errors after the commit, log ERROR and return success with `audit_degraded: true` (E9).
12. Broadcast `board_lanes_updated` (K10). Record metrics (K16). Return `{draft_version, lanes: [touched lanes], checks}`.

Refused results (step 9 blocks and step 10 conflicts) are logged under the refused id form, so they never occupy or replace the committed record (review finding 9). Test: two concurrent requests with the same `client_command_id` against the fake atomic store and against Postgres (`tests/postgres/test_dispatch_board_atomic.py`) produce exactly one committed log doc and one draft commit. The winner's response equals the committed log `response`. The loser's response is either equal to it, or `already_applied: true` with the same lane versions (the loser can read before the winner's log write lands, after its 3 × 100 ms retries).

Validation runs outside the row lock (steps 7–9) and the commit inside it only compares versions, so a concurrent change to the same lane between 9 and 10 makes the commit refuse, never commit stale validation (invariant I2). Two commands on different lanes both commit; their lanes are validated independently, which is correct because every check is lane-scoped except `driver_double_booked` and order exclusivity. Those two cross-lane rules are re-checked inside the transform from the stored draft (pure, cheap): if they fail, the transform refuses with `BOARD_LANE_CONFLICT` and `reason: cross_lane_rule`.

#### K4.3 Best fit and auto-load selection

For `target.index = null`: the engine tries each existing not-started load of the lane in time order, then a new load; for each, `best_insertion_index`, then the solver feasibility. It picks the first load with no `block`, preferring fewer added minutes. This is deterministic given the context.

#### K4.4 Acknowledgements

`acknowledge_warning` writes into `draft.acknowledged[warning_id] = {reason, actor_user_id, at, truck_id}` without a lane version check, because an ack never changes lane content. It is refused (422) if the warning id is not currently open on that lane. A lane change that removes the warning leaves the ack orphaned (harmless); a changed condition yields a new id (K2.3).

#### K4.5 Idempotency

The authoritative idempotency record is `draft.applied_commands`, written in the same `atomic_update` as the change, so "committed" and "recorded as applied" can't diverge. The committed log doc (insert-if-absent) is the replayable response. A retry after a crash between steps 10 and 11 finds the id in `applied_commands` at step 4 and gets `already_applied: true` with the current lanes instead of a conflict. The client treats `already_applied` as success, and also treats a conflict whose returned lane already contains the intended result as success (`useBoardCommands.isAlreadyApplied`), which covers ids pruned after 24 h.

### K5. Snapshot read (R2, R3)

`GET /api/fuel/board/{service_date}` (optional `?lanes=`, `?call_type=`, `?product=`, `?window=`) returns:

```json
{
  "data": {
    "service_date": "2026-10-08", "timezone": "America/Chicago", "mode": "active_gated",
    "draft_version": 42, "read_only": false, "degraded_sources": [],
    "shifts": [{"id": "day", "start": "06:00", "end": "18:00"}, {"id": "night", "start": "18:00", "end": "06:00"}],
    "lanes": [ { "truck_id": "...", "version": 7, "truck": {...}, "compartments": [...], "driver": {...},
                 "loads": [...], "shelf": [], "checks": [...], "publish": {...}, "modified": false } ],
    "trays": { "orders": [...], "drivers": [...], "trucks": [...] },
    "suggestions": [...],
    "acknowledged": {"<warning_id>": {...}}
  },
  "request_id": "..."
}
```

Order tray query (review findings 4 and 5): one `search_documents` on `fuel_orders_current` with `tenant_id`, `status in [placed, confirmed, scheduled, on_hold]`, and either delivery window overlapping the service day or no window (will-call). Size 1,000, sorted `[{delivery_window_start: asc}, {order_id: asc}]`; the translator puts orders with no window last (NULLS LAST, `build_order_by`). Orders with a non-empty `assigned_run_id` are excluded unless that run is one of this draft's published plans. Orders already in the draft are excluded via `order_index`. Above 1,000 matches, the response includes `orders_truncated: true` and the UI says so (E5). When truncated, the tray therefore holds the 1,000 earliest windows, and will-call orders with no window are the first to drop out. The banner says "Showing the 1,000 earliest delivery windows. Filter to narrow the list."

- **On hold.** `on_hold` orders are returned with `draggable: false` and `block_reason: "on_hold"` and count toward the 1,000 cap. The client groups them into the collapsed "On hold" section (R3.6). Every other order has `draggable: true`, `block_reason: null`. A command naming an `on_hold` order still goes through validation and is blocked by `order_state on_hold` (K3.2), so the flag only drives the UI.
- **Priority join.** Orders have no priority field, so priority isn't part of the store query. After the tray query, the service reads the newest `mvp_delivery_priorities` doc for the tenant with `created_at` in the last 24 h (one search, `sort created_at desc`, size 1, `_source: ["priorities", "created_at"]`). It joins `priority_score` and `priority_bucket` onto tray orders by `order_id` in process. Entries without `order_id` (station-level scores) are ignored. If an order has several entries, the highest score wins. A doc older than 24 h counts as no doc, so a stale run never orders today's tray.
- **Returned order.** The page is sorted by `priority_score desc` (orders with no score after every scored order), then `delivery_window_start asc` (no window last), then `order_id asc` (R3.3). The client re-sorts locally for the other sort options. If the priority read fails or times out (2 s), the tray is returned in window order with `degraded_sources: ["delivery_priorities"]` and no buckets on the cards, logged at WARNING.

Server-side tray filters (review finding 8): `call_type` (enum of the four call types), `product` (1–32 chars, product code charset), `window` (`overdue|today|later`) are optional and narrow only the tray query; lanes are always complete. The client applies every filter locally (R4.1) and adds these three to the request only after a response with `orders_truncated: true`, so a tenant under 1,000 orders never round-trips on a filter change.

#### K5.1 Orders on another day's draft (review finding 10)

After the tray query, one `search_documents` on `dispatch_board_drafts` with `tenant_id`, `service_date` in [today, today + 14] and not equal to this date, `terms order_ids = <tray candidate ids>`, `_source: ["service_date", "order_ids"]`, size 15. Candidates found there are dropped from the tray. The command path runs the same lookup for the command's orders inside `build_context` and the engine adds `block order_on_other_day` naming the other date ("Order 1042 is planned on Oct 9. Remove it there first."). This is a best-effort guard: two drafts are separate documents, so two dispatchers adding one order to two days at the same instant can both commit. The publish preflight repeats the lookup (fresh), and the executor's `order_committed_elsewhere` is the final guard, so at most one day publishes the order. Test: an order on tomorrow's draft is absent from today's tray and blocked on assign.

#### K5.2 Suggested permanent-driver pairing (R6.5, review finding 6)

Each lane with `driver_id == null` carries `suggested_driver: {driver_id, name, source: "assigned_truck_id"} | null`, from `driver_repository.search(tenant_id, assigned_truck_id=truck_id, status="active")` (`fuel/driver_repository.py` L587). It is set only when exactly one active driver matches and that driver is not paired to another lane of this draft; zero or several matches give `null`. The searches for all unpaired lanes run in parallel inside the snapshot's read budget. The driver slot shows "Pair {name}" as a button that sends `pair_driver` with `input_modality: "menu"` (validated as usual). Tests: zero, one and two matches; one match already paired elsewhere gives `null`. Drivers: `driver_repository.search` for the tenant (active and inactive, size 500). Trucks: compartment-bearing trucks (`listCompartmentTrucks` server path). Lanes with `checks_stale` or `checks_computed_at` older than 60 s are re-validated in the read (one shared context) and written back with `atomic_update` only if the lane version is unchanged (no version bump for check refresh; `checks` are derived data). `?lanes=T1,T2` returns only those lanes (used after `board_lane_stale`).

On today's service day only (K3.2), the snapshot also includes HOS figures per paired driver (`HOSAdvisoryService.resolve`), which closes G6 for dispatchers without changing the self-only `/api/driver/hos`.

### K6. Undo and redo (R11)

Snapshot-based, not inverse-operation based, so every command type is undoable without per-type inverse code.

- `revert {target_command_id}`: load the log doc; require `actor_user_id == session user`, `result == committed`, the command is in the same service day, and for every touched lane `content_hash(current lane) == hash_after` and the lane is not `publishing` and was not published after the command (`publish.published_version < version_after` or never published). Else 409 `BOARD_UNDO_STALE` with reason (`changed_by_other`, `published_since`, `not_owner`). Then the proposed lanes are `content_before` (restoring `order_index` accordingly), validated as usual (a `block` refuses with `BOARD_COMMAND_BLOCKED`), and committed as a new command with type `revert` (its own log doc).
- `reapply {target_command_id}`: the mirror: require `hash == hash_before` of the target, then apply `content_after`.
- The client keeps two stacks of command ids (max 50) in memory for the session; a new non-undo command clears the redo stack. Publish of a lane removes stack entries touching it (R11.3).
- Cross-lane order exclusivity: restoring `content_before` for lanes A and B is safe because the transform re-checks exclusivity against the stored draft (K4.2).

### K7. Publish (R12)

#### K7.1 API

`POST /api/fuel/board/{service_date}/publish` with `{client_request_id, lanes: [{truck_id, expected_version}], warning_reasons: {warning_id: reason}}` → 202 `{publish_id, lanes: [{truck_id, state: "queued"}], groups: [{truck_ids, kind, added_lanes}]}` (the same `groups` shape as the dry run below; `kind` is `first_publish` or `redispatch`). Progress via `board_publish_progress` events and `GET /api/fuel/board/{service_date}/publish/{publish_id}`. 202 + background task because 60 lanes × (executor + dispatch) can exceed the ALB idle timeout.

**Dry run (review dialog data, R12.3, R13.7).** The same endpoint with `{dry_run: true, lanes, warning_reasons?}` runs the K7.2 preflight, group expansion (K8.4) and change classification (K8.2) and returns 200 with no claim and no writes:

```json
{"ready": true, "not_ready": [{"truck_id": "...", "reasons": ["..."]}],
 "groups": [{"truck_ids": ["T1", "T7"], "kind": "redispatch", "added_lanes": ["T7"]}],
 "loads": [{"truck_id": "T1", "load_id": "...", "class": "new_revision", "info": ["driver_may_be_loading"]}],
 "notifications": [{"driver_id": "...", "name": "...", "revoke_order_ids": [], "assign_order_ids": [], "route_updated": false}],
 "open_warnings": [{"truck_id": "...", "warning_id": "...", "message": "..."}]}
```

The `PublishDialog` calls it when it opens and when the lane selection changes, debounced 400 ms, and aborts any earlier in-flight call with an `AbortController` (review pass 3 finding 5). It does **not** call it when reason text changes: the client marks an `open_warnings` entry satisfied when its reason is 3–500 characters after trimming, and the real publish re-checks every reason. The dialog renders exactly the last completed response. The real publish repeats the same preflight, so a dry run never has to be trusted. The handler picks the body model before `parse_body` with `dry_run = isinstance(body, dict) and body.get("dry_run") is True`: `True` → `PublishPreviewBody` (no `client_request_id`); anything else, including a body that is not a JSON object, → `PublishBody`, which requires `client_request_id`, so a non-object body returns 422 `VALIDATION_ERROR` from `parse_body` (review pass 3 finding 6). Reuse instead of a new endpoint: the preflight already computes everything the dialog shows.

#### K7.2 Preflight (synchronous, before 202)

Mode active; day not past; 1 ≤ lanes ≤ 60; each lane exists, version matches, not `publishing`; each lane is Draft, Failed or Modified (Published-unmodified → returned as `already_published`, no work); `warning_reasons` validated (3–500 chars). Then a fresh (`fresh=True`) validation of each lane. A lane is not ready if it has a `block`, no driver, no loads, a non-empty shelf (`unplaced_dispatched_order`), or an open warning without an ack or reason in `warning_reasons`. The cross-day lookup (K5.1) runs fresh for the claimed lanes' orders.

**Snapshot refresh (review finding 12).** The fresh context reads every order of the claimed lanes. For each stop the preflight rebuilds `Stop.snapshot` from the current order. If `product_code`, `customer_id` or `customer_tank_id` changed, the stop has `block order_identity_changed` and the lane is not ready (fix: remove and re-add). If only `gallons_requested` or `fill_to_full` changed, the solver recomputes the load's `allocations` from the current orders (the same `optimize_loading_plan` call validation uses); a resulting `compartment_fit` block makes the lane not ready, and an `allocation_overrides` entry for that order whose shares no longer total the order's litres (± 1 %) gives `block override_stale` ("Reset or update the compartment split"). The refreshed snapshots and allocations are written into the lanes by the claim below, so the plan documents and `order_snapshots` passed to the executor match the orders as read at preflight. The executor then only guards the window between preflight and apply. Tests: changed gallons publishes with new allocations; changed product blocks with `order_identity_changed`.

If any requested lane is not ready → 409 `BOARD_PUBLISH_NOT_READY` with per-lane reasons; nothing is claimed (all-or-none claim keeps the UI simple). Otherwise one `atomic_update` writes the acks from `warning_reasons`, the refreshed snapshots and allocations, and claims every lane: `publish.state = "publishing"`, `attempt_id = uuid4`, `lease_until = now + 120 s`, `lane.version + 1`, and, on every lane of a redispatch group, `publish.attempt` (K2.1: the load classes and target ids from `plan_changes`, the relinks with their from-links from K8.4 phase 2, and the notify baseline). A lane in recovery (K2.1) can be claimed only by a request whose lanes include the whole `attempt.group_truck_ids` (the UI's Retry sends exactly that set); such a request skips fresh classification and resumes the recorded attempt (K8.4 Recovery). It refuses if any version moved (409 conflict). One draft document means the multi-lane claim is atomic.

#### K7.3 Worker

`BoardPublishService.run(publish_id)` runs as an `asyncio.create_task` started by the endpoint; the service keeps a strong reference in a `set` and removes it on completion (the pattern `bootstrap/agents.py` L618 uses for its periodic tasks), and logs any unexpected exception at ERROR before marking the lane `failed {reason: "worker_error"}`. The preflight split the claimed lanes into publish groups (K8.4). Groups are processed sequentially in request order (serialize instead of racing), and a group's failure stops only that group. A **redispatch group** (it holds a Modified lane or any `dispatched` order) is handed whole to `BoardRedispatchService.run_group` (K8.4), including any never-published lane in it. Every other lane is a **first-publish group** of one and runs the steps below. The worker renews `lease_until` for the group's lanes before each load. First publish per load, in load order:

1. `plan_id = f"bp-{load_id}-r1"`, `route_id = f"br-{load_id}-r1"`, `run_id = plan_id`.
2. Upsert the plan doc (shape K7.3a) with `atomic_update(mvp_load_plans, plan_id, transform, upsert=doc)`: if absent, create it with `status: "draft"`. If present and `status == "draft"` and `board_load_id` matches, overwrite `assignments` and `updated_at` (retry after a pre-apply failure). If present in any other status, leave it (already applied; the executor will replay).
3. Upsert the route doc (shape K7.3a) the same way, keyed by `route_id`; overwrite `stops` only while `status == "planned"`.
4. `executor.execute(tenant_id, plan_id, expected_order_ids, expected_truck_id=truck_id, order_snapshots={order_id: {customer_tank_id, gallons_requested, fill_to_full} from the refreshed stop snapshots}, actor_user_id, action_id=None, approved_at=now, mode="active_gated")`. The board passes `mode` explicitly because the loading agent's overlay flag must not gate a dispatcher's publish; the board flag already gated it (K13). Non-success → lane `failed` with `{stage: "apply", reason, writes_made, retryable, failures}`; stop this lane.
5. `dispatch_service.dispatch(tenant_id, plan_doc=<re-read plan>, actor_user_id, driver_id=lane.driver_id)` with the default `notify=True`: a first-publish group holds no dispatched order, so nothing is revoked and each `send_assignment` describes work that is already committed for that load. An `AppException` (409) → lane `failed` with `{stage: "dispatch", reason from details, writes_made: true}` (orders are `scheduled` and linked; retry resumes).
6. Record `plans[load_id] = {plan_id, route_id, run_id, revision: 1}`.

After the last load: one `atomic_update` sets `state = "published"`, `published_version`, `published_content`, `published_hash`, `last_result`, clears the lease, `version + 1`, refusing only if `attempt_id` differs (lease was taken over). Then activity log (`ActivityLogService.log`), `log_audit_event`, `board_publish_progress`, `board_lanes_updated`.

Redispatch groups go to `BoardRedispatchService.run_group` (K8.4) inside the same worker loop. `BoardPublishService` never processes a lane that holds a `dispatched` order (freeze rule 7).

#### K7.3a Plan, route and execution document shapes (review finding 1)

These are the only shapes the board writes. Every key is declared in the mappings (K2.4, by convention; the store doesn't enforce them); the driver app, dispatch and the execution service read them as listed in "Evidence".

```python
# mvp_load_plans/{plan_id}   (board)
{"plan_id": "bp-{load_id}-r{n}", "run_id": plan_id, "tenant_id", "truck_id", "terminal_id",
 "status": "draft",                       # executor → scheduled, dispatch → dispatched
 "source": "dispatch_board", "board_draft_id": "{tenant_id}:{service_date}", "board_load_id": load_id,
 "service_date", "shift_id", "load_seq": index of the load in the lane (1-based),
 "driver_id": lane.driver_id, "revision": n, "supersedes_plan_id": "bp-{load_id}-r{n-1}" | None,
 "created_at", "updated_at",
 "assignments": [{"order_id", "station_id": order.customer_id,          # executor _changed_field
                  "product_code": canonical code, "fuel_grade": canonical code,  # as the loading agent persists it
                  "compartment_id", "quantity_liters", "compartment_capacity_liters"}]}

# mvp_routes/{route_id}   (board)
{"route_id": "br-{load_id}-r{n}", "plan_id", "run_id": plan_id, "tenant_id", "truck_id",
 "status": "planned",                     # dispatch → dispatched
 "timestamp": now, "created_at", "updated_at", "distance_km": from dispatch_board_eta,
 "source": "dispatch_board", "board_load_id", "service_date", "revision": n,
 "supersedes_route_id": "br-{load_id}-r{n-1}" | None,
 "stops": [{"sequence": int, "station_id": order.customer_tank_id or order.customer_id,  # driver-app coordinates
            "order_ids": [order_id],      # dispatch _resolve_orders
            "eta": iso datetime,           # key omitted when unavailable
            "drop": {product_code: liters}}]}   # driver app planned_gallons_by_grade
```

Rules:

- Plan assignments use `station_id = customer_id` (the executor compares it with `order.customer_id`); route stops use `station_id = customer_tank_id`, falling back to `customer_id` (the driver app resolves coordinates from `customer_tanks.customer_tank_id|station_id` or `fuel_stations.station_id`). The two documents deliberately use different identifiers for the same stop.
- One route stop per order (`order_ids` has one id), `sequence` 1..n in load order on first publish. Sequence rules after publish are in K8.4.
- Windows, call type and snapshots are not written to the route (no reader needs them there, and the route shape stays exactly what the mapping declares); they stay on the draft.
- `eta` is omitted (not null) when the ETA is unavailable, because the mapping types it as a date.
- Executions are created by `FuelPlanDispatchService._ensure_execution` → `PlanExecutionService.create_execution`, which copies `station_id`, `sequence`, `eta`, `drop` from these route stops. The board writes execution documents only in the cases in K8.4.

Test (task 17): after a board publish, `GET /api/driver/work` for the paired driver returns, for every published stop, non-null `lat`/`lon`, `planned_arrival` and `planned_gallons_by_grade`, plus `manifest_available: true` with one manifest entry per assignment; and the mapping parity test (task 5) passes for both shapes.

#### K7.6 Board plans in existing MVP surfaces (review finding 11)

Board plans live in the same indices as agent plans, so the existing surfaces must not act on them:

- `GET /api/fuel/mvp/plans` (`mvp_endpoints.py` L752) adds `must_not: [{term: {source: "dispatch_board"}}]` unless the request passes `?source=dispatch_board`, which returns only board plans (read-only listing).
- `POST /plan/{plan_id}/approve`, `/reject` and `/replan` (L806, L906, L445) read the plan first; when `source == "dispatch_board"` they raise `AppException(ErrorCode.BOARD_OWNED_PLAN, "Manage this plan on the Dispatch Board", status_code=409, details={"plan_id": plan_id})` before any write.
- `ExceptionReplanningAgent._load_plan_snapshot` adds the same `must_not` to its plan and route queries. A disruption on a board-published truck is therefore not auto-replanned by the agent; the board shows the disruption through the order and execution events, and the dispatcher re-plans on the board. Surfacing exception replans for board lanes as suggestions is backlog B10.

Tests (task 5b): list hides board plans by default and shows only them with the param; approve, reject and replan return 409 `BOARD_OWNED_PLAN` and write nothing; the agent's snapshot ignores a newer board plan and returns the agent plan.

#### K7.4 Dispatch with an explicit driver (MODIFY `plan_dispatch_service.py`)

`dispatch(*, tenant_id, plan_doc, actor_user_id, driver_id: Optional[str] = None, notify: bool = True)`.

- `driver_id`: when given, `_resolve_driver` is replaced by a read of that driver in the tenant that requires `status == "active"`; failure raises the same `DRIVER_UNAVAILABLE` 409 with `reason: "board_driver_unavailable"`.
- `notify` (review finding 2): when `False`, the `send_assignment` block (L384–402) is skipped. The caller builds the same payload from the returned `PlanDispatchResult` (`plan_id`, `run_id`, `truck_id`, `route_ids`, `order_ids`, sent to `driver_id`); no new result field is needed. The `order.dispatched` event and push notification (fired by the order transition inside the writes) are unchanged, since they report a committed order state, not an assignment bundle. `BoardRedispatchService` always passes `notify=False` and sends the assignment in its notify phase (K8.4, freeze rule 8).

All other behaviour (route fetch, validation, link, transition) is unchanged. Existing callers pass nothing and behave as today. The plan doc's `driver_id` is written by the board for traceability; dispatch does not read it. Tests (task 3): with `notify=False` the WS manager is never called and the result carries the payload fields; the default path still calls `send_assignment` once with the same payload as today.

#### K7.5 Idempotency and recovery

- Same `client_request_id` within 10 minutes returns the same `publish_id` (stored in the draft under `publishes[client_request_id]`, trimmed to the last 20).
- Plan ids are deterministic per load and revision, the executor replays succeeded plans, and dispatch is idempotent for orders already dispatched to the same run and driver. A retry (`Retry` on a failed lane = a new publish request for that lane) therefore completes without duplicates. For a redispatch group, Retry follows K8.4 Recovery instead: it is a fresh publish after a rollback, or it resumes the recorded attempt when the recovery is forward or an interrupted rollback is still pending, and it always covers the whole group.
- Crash recovery: a lane in `publishing` whose `lease_until` is past is shown as `failed` with `reason: "interrupted"`, `writes_made: unknown`, `retryable: true` (snapshot read computes this; the next publish request may reclaim it). No background sweeper is needed.
- Lane edits during `publishing` are refused (R14.5), so the published content is exactly the claimed version.

### K8. Changes after publish (R13)

#### K8.1 What counts as started and pinned

A stop is **pinned** when its order is `in_transit`, `delivered` or `failed`. A load is **started** when any of its orders is `in_transit`, `delivered` or `failed`, or its execution (`mvp_plan_executions`, matched by `plan_id` + `route_id`) has `completed_stops > 0` (review finding 3). Both signals exist today (Evidence). Loading at the rack before the driver marks the first order `in_transit` is not observable, so such a load counts as not started and a change re-publishes it as a new revision. For a not-started published load whose `planned_start` is in the past, the change classification attaches `info driver_may_be_loading` ("The driver may already be loading at the terminal. Call before re-publishing."), which the publish dry run returns and the review dialog shows next to that load (K7.1, R13.7). It is information only and needs no acknowledgement. `build_context` reads the executions of the published loads on the touched lanes (one `search_documents` on `mvp_plan_executions` with `tenant_id` and `terms plan_id`, `_source: ["plan_id", "route_id", "completed_stops"]`), so commands and the publish preflight apply the same definition. Engine rules (block `stop_pinned` / `load_started`): pinned stops cannot move; a started load accepts only reorder of unpinned stops and removal of unpinned stops; its allocation and terminal are frozen; and the lane's driver can't change while it holds a started load (Q12: the driver is physically on the truck mid-load; a swap is handled outside the board).

#### K8.2 Change classification per load (`BoardRedispatchService.plan_changes`)

Compare `published_content` with the draft for each lane in the publish group (the requested lanes plus any lane that a moved dispatched order came from or goes to; the preflight adds them to the claim automatically and the dry run lists them as `added_lanes` for the review dialog, R13.7). A never-published lane has no `published_content`, so every one of its loads is New. `plan_changes` is pure: it takes the lanes, the fresh order statuses and execution `completed_stops` from the preflight context, and `now`.

- **Unchanged** load (same content hash and same lane driver): nothing.
- **New load**: a load with no entry in `publish.plans`, on a published lane or on a never-published lane that is in a redispatch group. It runs the K8.4 New load path, which relinks any `dispatched` order it holds (review finding 1).
- **New revision**: a published load that is **not started** and changed in any way (stops gained, lost or reordered, allocation, terminal, or the lane's driver). It gets new `plan_id` / `route_id` `…-r{n+1}`, where `n = publish.plans[load_id].revision` (updated only at finalize, so a retry recomputes the same ids). One path for every not-started change keeps sequence numbers, executions and caches simple: the old plan, route and execution are retired whole and new ones are created (review finding 2, freeze rule 6).
- **Amend in place**: a published load that **is started** and changed. By K8.1 the only possible changes are reorder or removal of unpinned stops. The plan, route and execution ids stay; their documents are rewritten under guards (K8.4).
- **Removed** load (published, now empty because every stop moved elsewhere): retired whole. A started load can't become empty, because its pinned stops can't move.

Each load gets exactly one class (property P8).

#### K8.3 Relink primitive (NEW `FuelOrderRepository.relink_dispatched_assignment`)

```python
async def relink_dispatched_assignment(self, tenant_id, order_id, *,
        from_run_id, from_asset_id, from_driver_id,
        to_run_id, to_asset_id, to_driver_id, claim_id) -> Literal["relinked", "already_relinked", "refused"]
```

One `atomic_update` on the order's current-state document. It writes `assigned_run_id`, `assigned_asset_id`, `assigned_driver_id`, `assigned_claim_id = claim_id`, `last_event_timestamp`, `updated_at` only when the tenant matches, status is `dispatched`, and the three current links equal the `from_*` values. It returns `already_relinked` when they already equal `to_*`, and `refused` otherwise (with the observed status in the log). It never changes status. It always sets `last_event_timestamp` to a server time later than the stored one, which is what makes a driver write based on an earlier read fail the K5a guard (K8.6). The same primitive with `from` and `to` swapped is the rollback relink (K8.4 Recovery). The service then appends an `order_reassigned` event (`append_event`, payload: from/to run, asset, driver, plan ids, actor) and broadcasts the existing `order_assigned` WS event shape on `/ws/orders`. `in_transit` orders are never relinked (they are pinned).

#### K8.4 Re-publish sequence per publish group

**Publish groups (review finding 1, freeze rule 7).** The preflight splits the claimed lanes into groups. Lanes X and Y are in the same group when a `dispatched` order linked to one of X's published plans (`assigned_run_id` equals a `run_id` in `X.publish.plans`) now sits on Y's loads or shelf. Groups are the connected components of that relation. When only Y was requested, the preflight adds X to the claim. A group is a **redispatch group** when it contains a Modified lane or any lane whose content holds a `dispatched` order; `BoardRedispatchService.run_group` processes the whole group, including never-published lanes in it. Every other lane is a first-publish group of one (`BoardPublishService`, K7.3). A dispatched order that isn't linked to any of this draft's published plans is already blocked by `order_committed_elsewhere` (K3.2), so it can't reach a group. The preflight context also reads the executions of every published load in the claimed lanes (one `search_documents` on `mvp_plan_executions` with `terms plan_id`), so `plan_changes` sees `completed_stops` (K8.1).

**Phases (revision 4, freeze rule 10).** Inside a redispatch group the steps run as phases across all of the group's lanes, not lane by lane. Each phase completes for every lane (lanes in request order, loads in time order) before the next phase starts, and the worker records the phase it starts in `publish.attempt.phase` (one draft `atomic_update` per phase, refused if `attempt_id` differs). The phases are ordered so that **every reversible write comes before every irreversible one**:

1. Retire (reversible): old documents of every new-revision load and every removed load.
2. Stage and relink (reversible): write every new plan and route as `draft` / `planned`, then relink every dispatched order of the group.
3. Apply (irreversible: tray orders advance): executor and dispatch for every staged load.
4. Amend: every changed started load.
5. Invalidate the driver work cache.
6. Notify.
7. Finalize.

All operational writes (orders, plans, routes, executions) finish before the cache is invalidated, and the cache is invalidated before any driver is notified (freeze rules 4 and 8). A failure in phases 1–2 is **rolled back**; a failure in phases 3–4 is **completed forward** (Recovery, below). An order moved from lane A to lane B is relinked to B's new run in phase 2, after A's not-started load was retired in phase 1 and before A's started load (if any) is amended in phase 4.

All writes to an order go through compare-and-set on the order document, including the driver's own status changes (K8.6), and all writes to an execution go through compare-and-set, including check-ins (K8.6). So each relink, retire and check-in is linearizable with every driver action on the same document, and the board learns of every conflicting driver action through a refused write or a fresh read, never through a lost update.

**Phase 1, Retire** (it closes the check-in path for the old plan):

1. `atomic_update(mvp_plan_executions, old execution_id)`: set `status: "superseded"`, `superseded_by_attempt`, `status_before_retire` (the prior status), `updated_at`, only when `completed_stops == 0` and every stop is `pending`; already `superseded` by this attempt counts as done. Otherwise refuse → group failure `{stage: "retire", reason: "load_started_concurrently"}` → rollback. Because `record_checkin` is now a compare-and-set that refuses on a superseded execution (freeze rule 9), a check-in either lands before this write (and this guard refuses) or after it (and the check-in is refused). It can't land in between.
2. `atomic_update(mvp_load_plans, old plan_id)`: `status: "superseded"`, `superseded_by_plan_id` (new-revision only), `superseded_by_attempt`, `status_before_retire`, `updated_at`, only from `dispatched` or `scheduled`. A driver check-in against the old plan is now refused at its plan-status check as well, and the driver app refetches.
3. `atomic_update(mvp_routes, old route_id)`: `status: "superseded"`, `superseded_by_attempt`, `status_before_retire`.

R25 (K12) excludes `superseded` plans from the manifest, so between this phase and phase 3 the affected orders show no manifest. That window lasts while the worker runs; after a failure it ends at rollback (phases 1–2) or when the forward retry completes (phases 3–4).

**Phase 2, Stage and relink.** One path for new revisions and new loads, differing only in ids:

- New revision: `plan_id = bp-{load_id}-r{n+1}`, `route_id = br-{load_id}-r{n+1}`, with `supersedes_plan_id` and `supersedes_route_id`.
- New load (on a published lane or a never-published lane): `bp-{load_id}-r1` / `br-{load_id}-r1`, as in K7.3.

Steps:

1. For every new revision and new load: upsert the plan and route (shapes K7.3a, upsert rules K7.3 steps 2–3, plus `created_by_attempt`), stop `sequence` 1..k in the new order. They stay `draft` / `planned`, which the driver app never shows (R25) and `record_checkin` refuses. New documents mean new sequence numbers can never collide with a stale driver view of an old route.
2. For every entry of `attempt.relinks` (every order of the group whose status was `dispatched` at preflight and whose target link differs from its published link): `relink_dispatched_assignment(from = entry.from, to = entry.to)` (K8.3). `from` is the order's **published** link, recorded at claim: the `run_id` in `publish.plans[load_id]` of the lane and load where `published_content` held the order, that lane's `truck_id`, and its `published_content.driver_id` (review pass 3 finding 3). `to` is the staged load's `run_id`, the lane truck and the lane driver. If the order's current links differ from `from`, the relink is `refused`, as K8.3 specifies; `already_relinked` counts as done. This applies equally to new loads (review pass 2 finding 1). A `refused` relink is a group failure `{stage: "relink", order_id, observed_status}` → rollback.

Nothing in phases 1–2 moves an order's status, so all of it can be undone (Recovery).

**Phase 3, Apply.** For every staged load, in lane then load order:

1. `executor.execute` on the plan. Relinked dispatched orders count as applied (`linked_here`; `_APPLIED_STATUSES` also includes `in_transit` and `delivered`, which matters only for a resumed attempt); tray orders go to `scheduled`. A refusal for a not-yet-applied tray order follows the drop rule (Recovery).
2. `dispatch(..., driver_id=lane.driver_id, notify=False)`: creates the execution through `_ensure_execution`, moves the remaining orders to `dispatched`, and sets the plan `dispatched`, which is what lets the load's driver start its orders (K8.6 board-plan gate). The returned `PlanDispatchResult` is kept in `last_result` (so a resumed attempt has it for phase 6). A plan already `dispatched` by this attempt is skipped, and its stored result is used. `order_not_dispatchable` for a `failed`/`cancelled` order follows the drop rule.

**Phase 4, Amend in place** (started load; only unpinned reorder or removal). The execution transform computes its result from the stored document, so it never refuses (revision 4):

1. Execution first. `atomic_update(mvp_plan_executions, execution_id)`, transform over the stored document: keep every non-`pending` stop record unchanged, sequence included (this includes a stop a driver completed a moment ago). Give each still-`pending` stop of the new order a fresh sequence starting at `max(all sequences on the route, ever) + 1`, carrying its existing record (matched by `order_id` through the previous route's `order_ids`). Drop `pending` records for removed stops; a removed stop whose record is no longer `pending` is kept and reported in `last_result.kept_completed_stops` (WARNING). Set `total_stops`. The transform returns the resulting stop list, and steps 2–3 write exactly that list, so the route always matches the execution.
2. `atomic_update(mvp_routes, route_id)` guarded by `revision == published revision`: the stops and sequences from step 1, `revision + 1`, `updated_at`. An equal target counts as done. The driver app sorts by `sequence`, so completed stops show first in their original order and the remaining stops follow in the new order. A stale driver view that checks in with an old sequence gets 404 (`record_checkin` re-locates by `station_id` and `sequence` inside its transform) and refetches; a stale sequence can never hit a different stop because numbers are never reused.
3. `atomic_update(mvp_load_plans, plan_id)` guarded by `revision`: drop assignments of removed orders, `revision + 1`. The compartment that held a removed order's product stays physically loaded; the manifest lists what the route will deliver.

**Phase 5, Invalidate.** `await work_cache_invalidator(tenant_id, order_id)` for every order on any touched load of the group, old or new (no run id, so every run's bundle is dropped; `DriverWorkService.invalidate` L333). The invalidator resolves `driver.api.work_endpoints.get_work_service()` at call time and is a no-op (DEBUG log) when it returns `None`. Invalidation failures are already swallowed by `invalidate`.

**Phase 6, Notify** (the only phase that talks to drivers). Compute per driver the set of orders assigned before the group (`attempt.notify_baseline`, taken from every group lane's `published_content` at claim, so a resumed attempt computes the same sets) and after it (from the attempt's content).

- `send_assignment_revoked(driver_id, {order_ids: lost, plan_ids: old plan ids, reason: "reassigned_by_dispatcher", service_date})`, only for orders a driver lost. A driver who keeps an order under a new revision gets no revocation for it.
- `send_assignment(result.driver_id, {plan_id, run_id, truck_id, route_ids, order_ids})` for every `PlanDispatchResult` kept in phase 3, which is the payload `dispatch()` would have sent itself.
- `send_new_route(driver_id, {plan_id, route_id, run_id, truck_id, order_ids, revision})` for each amended load's driver.

Revocations go first, so a driver who loses and regains nothing ends with no stale work. The driver app treats all three events as "refetch work" (`driver-app/lib/websocket.ts` L789–810), which is why phase 5 must come first. Notification failures are logged at WARNING and reported in `last_result.notifications_failed`; they never fail the publish. Cancellations chosen in the review are already done (the cancel flow is separate, R13.5); cancelled orders are dropped from content.

**Phase 7, Finalize.** One `atomic_update` on the draft for all lanes of the group, as in K7.3: `publish.plans[load_id] = {plan_id, route_id, run_id, revision}` for every new revision, new load and amend, entries of removed loads deleted, `state = "published"`, `published_version`, `published_content` (the attempt's content minus any order dropped under the Recovery drop rule), `published_hash`, `last_result`, lease cleared, `attempt` cleared, `version + 1`. It refuses only if `attempt_id` differs. Finalize is board bookkeeping, not an operational write, and it comes after notify on purpose: a crash between notify and finalize leaves the lanes `interrupted` with `attempt.phase` at `notify` or `finalize`, and the forward retry re-sends notifications. A duplicate "refetch work" is harmless, and a lost one is not. If an order was dropped, the lane's draft still holds it, so the lane is Modified after finalize and the dropped order's check explains why (`order_state` or `order_identity_changed`).

**Recovery (revision 4, freeze rule 10; replaces "retry re-runs the whole group from phase 1").** On any failure the worker stops the group before phase 5, so no driver is notified about a failed group (I10). Which way it recovers depends only on `attempt.phase`. It never depends on a fresh classification, because a fresh classification of half-written documents is what made pass 3 finding 2 diverge.

*Why rollback is always possible after phases 1–2.* Two guards on the driver's `in_transit` transition (K8.6) make it impossible for a driver to start an order while it is linked to a retired or staged board plan. The first is the board-plan gate: it refuses `in_transit` unless the order's board plan is `dispatched` (or `completed`). The second is the guarded write: a driver write based on a read taken before a relink is refused, because the relink changed `last_event_timestamp`. So during phases 1–2 a driver can start an order only while it is still on its published, `dispatched` plan, which means at its `from` link, before its relink. That start then makes the relink refuse. Orders at their `to` link are therefore always still `dispatched` or terminal (`failed`/`cancelled`, which are never gated) when rollback runs.

**Rollback** (failure in phase 1 or 2, or an interrupted attempt whose `phase` is `claimed`, `retire` or `stage_relink`). It runs in the worker straight after the failure. For an interrupted attempt it runs as the first step of the Retry. Every step is idempotent (an equal target counts as done):

1. Relink back. For each `attempt.relinks` entry, read the order:
   - Links equal `to` and status is `dispatched`: `relink_dispatched_assignment(from = entry.to, to = entry.from)`.
   - Links equal `from`: nothing to do.
   - Status `failed` or `cancelled`: leave it where it is. It's terminal, so no driver works it. Log INFO.
   - Any other state contradicts the guards above. Log ERROR, emit metric `dispatch_board.recovery_inconsistent` and stop with `{stage: "restore", retryable: true}`. Task 16 test (m) asserts this state can't be produced.
2. Retire staged documents. Every plan and route with `created_by_attempt == attempt_id` gets `status: "superseded"`, guarded on `draft`/`planned`. Phase 3 never ran, so no execution exists for them and no tray order was applied to them.
3. Restore retired documents. Every execution, plan and route with `superseded_by_attempt == attempt_id` goes back to `status_before_retire`, and the marker fields are cleared. Each write is guarded on `status == "superseded"` and the marker.
4. One draft `atomic_update`:
   - clear `publish.attempt`;
   - set each lane to `failed {stage, reason, writes_made: false, rolled_back: true, retryable: true}`;
   - `version + 1`.

   The lanes are editable again.

After rollback, every order of the group is on the plan, route and execution it had before the attempt, in the status they had. No driver was notified, so none needs to be told anything. The driver's manifest and check-ins work again. Retry is then an ordinary new publish with a fresh classification, so a load that started meanwhile (the usual cause of a phase 1–2 failure) is now an amend on its live documents (E14, E22). This is the reviewer's Restore step, applied to the whole group and keyed on the attempt markers. A rollback that itself fails (store down) leaves the attempt in place with `recovery: "rollback"`, and the next Retry runs rollback first.

**Forward completion** (failure in phase 3 or 4, or an interrupted attempt whose `phase` is `apply`, `amend`, `notify` or `finalize`). By then tray orders may have advanced, which can't be undone (freeze rule 1). So the attempt is completed, never reversed:

- The worker first retries the failing step in place, 3 times with 1 s, 2 s and 4 s backoff.
- If the step still fails, the lanes become `failed {stage, reason, writes_made: true, recovery: "forward", retryable}` and stay read-only (K2.1).
- Retry claims the whole `attempt.group_truck_ids` and resumes at `attempt.phase` with the recorded `attempt.loads` and `attempt.relinks`. It doesn't run a fresh classification.

Every phase from 3 on can be completed against any later driver action:
- The executor counts linked `in_transit`/`delivered` orders as applied and linked `failed`/`cancelled` orders as settled.
- Dispatch skips plans this attempt already dispatched.
- The amend transform recomputes from the stored execution.

**Drop rule.** Forward completion may drop an order from a staged load only in two cases, and never anything else:
- **A terminal order.** Dispatch refuses an order that became `failed` or `cancelled` before its load was dispatched with `order_not_dispatchable`. The order is removed from the staged route and plan, which are still `planned` / `scheduled` and unseen by any driver. The stops are renumbered 1..k and dispatch is called again.
- **A tray order the executor refuses.** The executor refuses an order that hasn't been applied yet with `order_changed_since_plan`, or the refreshed solver blocks it. The staged plan is still `draft`, so the order is removed from it and the executor is called again.

Each drop is recorded in `last_result.dropped_orders` with its reason, and happens at most once per order.

A forward step that keeps failing for a non-transient reason (for example `board_driver_unavailable` because the lane's driver was deactivated) stays `retryable: true` with the reason shown on the lane: "Reactivate {driver}, then retry. This lane can't change until the publish completes." While a forward recovery is pending, drivers on the group's staged loads have no manifest and can't start those orders (K8.6). So the lane shows an assertive banner, "{n} drivers can't see their routes until this publish completes", and the worker logs WARNING every 5 minutes while it is pending (Q16).

The only state where nothing can proceed is `recovery_inconsistent`. The guards make it unreachable, and if it happens anyway it is loud (ERROR plus metric) and retryable.

Tests (task 16):

- (a) An amend on a started load keeps the pinned stop's sequence and status, gives the moved stops sequences above the old max, and a `/api/driver/work` read shows the delivered stop as delivered.
- (b) A new revision leaves the old plan, route and execution `superseded`, and the old execution unreadable by the driver app.
- (c) After an amend with an unchanged run, the next `/api/driver/work` read returns the new stop order.
- (d) Call order, with a spy over every collaborator: all relinks, executor, dispatch and document writes, then every `invalidate`, then every notification. `dispatch` is always called with `notify=False`, and the dispatch WS manager is never called directly.
- (e) A failure injected at the amend phase (after 3 in-place retries) sends zero notifications and leaves the lanes `failed {recovery: "forward"}` and read-only. The Retry resumes at `amend` without reclassifying, completes, and notifies once.
- (f) Review finding 1: move a dispatched stop from published lane A to never-published lane B, then publish B only. The preflight adds A, and the dry run lists A in `added_lanes`. The order is relinked to B's run, and A becomes `r2` without the order. A's driver gets a revocation for that order, B's driver gets the assignment, and no `order_committed_elsewhere` is raised.
- (g) The same move into a new load on a published lane C relinks the order and publishes C's new load in the same group.
- (h) A driver who keeps an order under a new revision gets no revocation for it. A check-in committed before retire makes retire refuse and the group rolls back; a check-in after retire is refused (freeze rule 9).
- (i) Review finding 3: a load with every order `dispatched`, `completed_stops == 0` and a past `planned_start` is classified as a new revision with `info driver_may_be_loading`. One order `in_transit` makes it an amend, and so does `completed_stops == 1`.
- (j) Pass 3 finding 2. The driver marks an order `in_transit` after retire and before its relink, while it is still on its published plan. The gate is passed, because the plan was `dispatched` when the gate read it. This uses an injected interleaving: the gate reads first, then the board retires, then the driver write commits. The relink refuses and the group rolls back. Then:
  - r{n}'s execution is `in_progress`, and its plan and route are `dispatched`.
  - Every other relinked order is back at its `from` link.
  - The staged r{n+1} plan and route are `superseded`.
  - `/api/driver/work` for the driver shows the manifest, and a check-in succeeds.
  - Zero notifications are sent.
  - The Retry classifies the load as an amend and completes.
- (k) A crash (worker task cancelled) during `stage_relink` leaves the lanes `interrupted` and read-only. The Retry runs rollback before anything else, then publishes fresh. A crash during `apply` makes the Retry resume forward at `apply`, with no reclassification and no duplicate plan, route, execution or notification.
- (l) Rollback run twice, and forward completion run twice, each leave the same documents as running it once.
- (m) Gate and guard. After phase 2 relinks an order, a driver `in_transit` request based on a read taken before the relink returns 409 `ORDER_CHANGED_CONCURRENTLY`. One based on a fresh read returns 409 `BOARD_ROUTE_UPDATING`, because the staged plan isn't `dispatched`. After phase 3 dispatches the plan, the same request succeeds. Property: for random interleavings of driver transitions with phases 1–2 (hypothesis, fake store), rollback never meets `recovery_inconsistent`.
- (n) Drop rule. An order cancelled between executor and dispatch is dropped from the staged route, the stops are renumbered, dispatch succeeds, and the lane is Modified after finalize. A tray order whose gallons changed after claim is dropped from the staged plan, and the executor reruns.
- (o) Pass 3 finding 3: a relink whose order's current links differ from the recorded published link is `refused` even when the order is `dispatched`.

#### K8.5 Freeze decision for this area

Post-publish editing is the part most likely to grow edge cases, and it raised MEDIUM findings in review passes 1, 2 and 3. These rules are frozen. Each has a named test in plan task 16; rule 8's `notify` flag is also tested on its own in task 3, and rules 9 and 10's existing-code changes in task 4b:

1. No `dispatched → scheduled` path, ever.
2. A dispatched order is only ever relinked by `relink_dispatched_assignment` with a full from-link CAS, where `from` is the order's recorded published link (or, in rollback, the recorded `to` link), never its current links (revision 4, pass 3 finding 3).
3. Pinned stops and started-load allocations are immutable on the board.
4. Notifications are sent only after all operational writes of the group succeed and after the driver work cache is invalidated.
5. A group is processed sequentially under the lane claim, never in parallel.
6. (Revision 2.) A not-started published load is never amended in place. Any change retires its plan, route and execution and creates revision `n+1`. Amend in place exists only for started loads, where it may only reorder or remove unpinned stops, and sequence numbers are never reused.
7. (Revision 3, review finding 1.) Every publish that touches a dispatched order runs as one redispatch group, processed phase by phase across all of its lanes by `BoardRedispatchService`. `BoardPublishService` only publishes lanes that hold no dispatched order. One path relinks, so no path can forget to.
8. (Revision 3, review finding 2.) Redispatch never lets a collaborator notify. It calls `dispatch(..., notify=False)` and sends every driver message itself in phase 6.
9. (Revision 4, pass 3 finding 1.) **Execution writes are compare-and-set on both sides.** `record_checkin` persists through `atomic_update` with a transform that re-locates its stop and refuses on a superseded execution, a missing stop or a completed stop (K8.6). The board's retire and amend are `atomic_update`s too. Named tests: task 4b (interleaving units and the Postgres race) and task 16 (h).
10. (Revision 4, pass 3 finding 2.) **A retire is undone, never worked around.**
    - Redispatch phases put every reversible write (retire, stage, relink) before every irreversible one (apply).
    - A failure before apply is rolled back to the exact pre-attempt documents, using `superseded_by_attempt`, `status_before_retire` and `created_by_attempt`.
    - A failure from apply on is completed forward from the attempt recorded at claim.
    - Neither path reclassifies half-written state, and lanes in recovery are read-only.
    - Rollback can always succeed because drivers can't start an order linked to a non-`dispatched` board plan (board-plan gate), and stale driver writes are refused (guarded transitions) (K8.6).
    - Named tests: task 16 (j)–(o), task 4b, and property P12.
11. (Revision 5, pass 4 freeze directive.) **Freeze rule 11: Nothing in post-publish depends on a skipped guard, an unlocked write, or in-memory state.** Each item reuses an existing mechanism or narrows a rule; none adds a new path.
    - (a) **The board-plan gate can't be skipped by wiring.** `DriverTransitionGateStack` and its configure function take a `board_plan_reader` keyword (the document store's `get_document`), passed at the existing bootstrap call site. When the reader is `None` and `assigned_run_id` starts with `bp-`, gate 0 fails closed: 409 `BOARD_ROUTE_UPDATING` and a WARNING log, never a skip. Tests (task 4b): a stack built without a reader refuses `in_transit` on a `bp-` run and allows it on an agent run; a bootstrap wiring test asserts the configured stack has a reader.
    - (b) **Plan writes on the check-in path use compare-and-set.** The check-in endpoint's plan `completed` write (`mvp_endpoints.py` ~L1162), `compute_actual_cost` and `compute_estimated_cost` (`plan_execution_service.py`) become `atomic_update` transforms that merge only their own fields into the stored document; fields and responses are unchanged. Both files join N7's exception list. Test (task 4b): an interleaving fake store where the endpoint reads, then the board's revisioned plan write commits, then the completion commits; the revision and assignments survive and the status is `completed`.
    - (c) **An amend that leaves no pending stop finishes the load.** When the amend transform's result has `completed_stops >= total_stops`, it sets the execution `status: "completed"`. Phase 4 step 3 then sets the plan `completed`, guarded on `dispatched` and the revision, and calls `compute_actual_cost`, exactly as `driver_checkin` does after its last check-in. Test (task 16): remove the only pending stop from a started load; the execution and plan are `completed`, an outcome doc exists, and the driver's work read shows the load finished.
    - (d) **The notify payload is rebuilt from documents, not from memory.** Phase 6 builds each `send_assignment` payload from stored documents for every plan with `created_by_attempt == attempt_id` and `status == "dispatched"`: `plan_id` and `run_id` from the plan; `truck_id`, `route_ids` and `order_ids` from its routes' `stops[].order_ids`; `driver_id` from the lane driver in `attempt.loads`. This is the same payload `dispatch()` builds. Phase 3 no longer keeps results in `last_result`. Test (task 16 (k)): a crash after dispatch and before notify; the resume sends each assignment once, with a payload equal to the uninterrupted run.
    - (e) **A pinned order that's sitting on the wrong lane can always go home.** `stop_pinned` blocks moves away from the load that the order's `assigned_run_id` names; a move onto that load is always allowed. On any other lane the check's message is "Started on {truck}. Move it back." and its fix action is that move. Tests (task 9 and task 16 (j)): after rollback, B shows the block with the move-back action; the move back commits; publishing A then amends.

"Started" is defined only by observable state (an order `in_transit`/`delivered`/`failed`, or `completed_stops > 0`). Unobservable rack loading is surfaced as information (`driver_may_be_loading`), not modelled.

Pass 3 was the third consecutive pass with post-publish MEDIUMs. Both had one root cause: driver writes weren't serialized with board writes. Rules 9 and 10 remove that class instead of patching each interleaving. They serialize on the documents both sides already write, and they reuse existing mechanisms (`atomic_update`, the K5a guarded order write, the gate stack). If a later review finds another MEDIUM in this area that these rules don't cover, the response is to narrow the area further, for example by refusing the change with a reason, rather than adding a new path.

#### K8.6 Serializing driver writes with board writes (MODIFY existing code; freeze rules 9 and 10)

These are the only changes to existing driver-facing code. Agent-written plans behave exactly as before (N7).

**Check-in compare-and-set** (MODIFY `Agents/support/plan_execution_service.py` `record_checkin`, pass 3 finding 1):
- Keep the plan read, the plan-status check, the driver-assignment check, `_fetch_execution`, and the POD and variance computation as they are.
- Replace the in-memory edit plus `update_document` (L282–313) with `atomic_update(MVP_PLAN_EXECUTIONS_INDEX, execution_id, transform)`. The transform works on the stored document:
  - If `status == "superseded"`: verdict `superseded`, return `None`.
  - Re-locate the stop by `station_id` and `sequence`. If it's absent: verdict `missing`, return `None`.
  - If it's `completed`: verdict `completed`, return `None`.
  - Otherwise write exactly the fields the current code writes on that stop record, plus `completed_stops + 1` (from the stored value), the derived status, and `updated_at`.
- After the call, map the verdicts to the errors raised today:
  - `superseded`: `ValueError(f"Plan {plan_id} is not in 'dispatched' status (current: superseded)")`, the same branch as a non-dispatched plan.
  - `missing`: `resource_not_found` (404) with the same message and details.
  - `completed`: `stop_already_completed` (409).

  The driver app already refetches on each of these.
- `all_complete`, `completed_stops` and `total_stops` in the return value come from the document the transform wrote.
- The pre-read checks stay too, so an unassigned driver still learns nothing about the stops (R6.7 of that spec).

**Guarded driver transitions** (MODIFY `driver/api/transition_endpoints.py` L265):
- Call `apply_status_transition(..., guard_stored_state=True)`. That is the existing K5a path (`order_service.py` L270–339) that the executor and dispatch already use.
- Catch `OrderChangedConcurrentlyError` and `OrderWriteDiscardedError` and raise `AppException(ErrorCode.ORDER_CHANGED_CONCURRENTLY, "The order changed. Refresh and try again.", status_code=409, details={"order_id": order_id})`. Don't store the idempotency response for this error, so a retry with the same key runs again.
- The driver app's offline queue treats any 409 other than `INVALID_STATUS_TRANSITION` as transient and retries (`offline-queue.ts` L676–677). The retry re-reads the order, so it carries the current links. The driver app needs no change.
- This also closes today's silent overwrite of a concurrent dispatcher or executor write by a stale driver write.

**Board-plan gate** (MODIFY `driver/services/order_transition_service.py`, gate 0, ahead of the four existing gates, for `in_transit` only, since `GATED_TARGET_STATUSES` is unchanged):
- If the order's `assigned_run_id` starts with `bp-`, read `mvp_load_plans/{assigned_run_id}` (board runs use `run_id == plan_id`, K7.3a).
- If that plan has `source == "dispatch_board"` and its status is neither `dispatched` nor `completed`, raise 409 `BOARD_ROUTE_UPDATING`: "Your dispatcher is updating this route. Try again in a moment." `completed` passes so that a plan the endpoint closes after its last check-in never strands an order. During phases 1–2 no involved plan is `completed`, so this doesn't weaken the argument below.
- Agent runs don't have the prefix, so they are never read and never refused.
- If the read fails, the gate fails **closed** for board runs only (409 `BOARD_ROUTE_UPDATING`, WARNING). Allowing the start could break rollback; refusing costs the driver one automatic retry.
- `failed`, `cancelled` and `delivered` stay ungated, as the existing rule requires (a driver can always record a finished or failed delivery).

**Why the gate and the guard together close finding 2.**
- To start an order, a driver's write must be based on a read taken after the order's last relink, or the guard refuses it.
- With such a read, the gate sees the plan the order is linked to at that moment.
- During phases 1–2 that plan is either the published plan before its retire (the start then makes the relink refuse, and rollback is clean) or a retired or staged plan (the gate refuses).
- The gate reads the plan and the write commits separately. If the gate reads the plan as `dispatched`, then the board retires it, then the driver write commits, the order is still at its `from` link: retire doesn't change the order. So the relink refuses and rollback is still clean. Test (j) uses exactly this interleaving.

New error codes: `ORDER_CHANGED_CONCURRENTLY` (409) and `BOARD_ROUTE_UPDATING` (409), both in `errors/codes.py`.

### K9. Agent suggestions (R16)

- **Read:** `mvp_load_plans` with `must_not term source=dispatch_board` (agent plans have no `source`), status `proposed` or `draft`, truck in the tenant, and created for the service day (`created_at` within the day in tenant time, or `service_date` when set), plus `mvp_routes` for those plans; joined with pending `apply_loading_plan` approvals (`ApprovalQueueService.list_pending`) by `parameters.plan_id`. Hidden unless `overlay.compartment_loading` / `overlay.route_planning` mode is `active_gated` or `active_auto` (R16.5, Q6), read with `get_overlay_state` per agent.
- **Suggestion id:** the plan id. Rejected suggestions (board-side) are remembered in the draft (`dismissed_suggestions`) so they don't reappear.
- **Diff producer (`dispatch_board_suggestions.diff`):** pure function `(lane_content, suggested plan, suggested route) -> ReplanDiff` reusing `Agents/support/replan_diff_models.py` (`StopRef`, `ReorderedStop`, `ReassignedStop`, `QuantityChange`, `EtaShift`, `ReplanDiff`). Orders the suggestion puts on another truck than the draft are `ReassignedStop`.
- **Why text:** priority bucket and score from `mvp_delivery_priorities` (`GET /api/fuel/mvp/priorities` data), forecast/runout from `mvp_tank_forecasts` when present, fill from the suggestion's allocations; templated, no LLM text.
- **Accept:** `accept_suggestion` converts the plan into loads (one load per plan, stops in route order, `source: "suggestion"`), touching each target lane; orders already on other lanes in the draft are moved (they show as reassigned in the diff the dispatcher saw). A `block` on any lane refuses the whole command unless `load_ids` names a subset; the UI offers "Accept the N loads that pass" after a refusal (R16.3).
- **Reject:** `POST /suggestions/{id}/reject {reason?}` adds to `dismissed_suggestions` and, when a pending approval exists, calls `ApprovalQueueService.reject(action_id, reviewer_id=session user, reason, tenant_id=tenant)`. A reject failure (already approved, expired) is reported but the dismissal stays.
- **Accept never approves the approval entry (R16.7).** If someone approves the agent's approval in the queue while the board has the same orders in a draft lane, those orders become `scheduled` and linked to the agent's plan; the board's `order_state` check then blocks with `order_committed_elsewhere` and the lane shows a fix link to the plan. Accepted on the board and later approved in the queue is therefore safe but needs the dispatcher to remove the stops (E7).
- **Generate plan:** the toolbar calls the existing `generatePlan` client; new suggestions arrive via `/ws/fuel-planning` and the existing approval events, which make the board refetch suggestions.

### K10. Realtime and presence (R14, R15)

#### K10.1 Endpoint

`/ws/dispatch-board?service_date=YYYY-MM-DD` in `bootstrap/websockets.py`, authenticated by a NEW `_authenticate_dispatcher(websocket) -> Optional[Tuple[str, str, list[str]]]` built like `_authenticate_tenant` (L276) on `_resolve_ws_claims`. It returns `(claims["tenant_id"], claims["sub"], claims["roles"])`; `sub` is where SuperTokens puts the user id in `get_access_token_payload()` (used by `_default_ws_verify`). It returns `None` when `sub` or `tenant_id` is missing or empty, or when the roles include neither `admin` nor `dispatcher` (exact match), and the endpoint then closes with the existing `_reject` (4001). The board's flag mode is read after authentication; `disabled` closes with `_reject` too. Tests: payload without `sub` is rejected; driver role is rejected; dispatcher in an active tenant connects. The other sockets don't check roles today; this one does because it carries draft content. `DispatchBoardWSManager(BaseWSManager)` keeps connections keyed by `(tenant_id, service_date)`.

#### K10.2 Server → client events

- `board_lanes_updated {service_date, draft_version, actor: {user_id, name}, command_type, lanes: [Lane]}`. If the payload exceeds 256 KB, `lanes` carries only `{truck_id, version}` and clients fetch `?lanes=`.
- `board_lane_stale {service_date, truck_ids, reason}` (order or compliance change) → client fetches `?lanes=`.
- `board_presence {service_date, users: [{user_id, name, focus_truck_id, last_seen}]}`; `name` and `actor.name` come from `resolve_actor_name` (K17.1).
- `board_publish_progress {publish_id, truck_id, state, stage, result?}`.
- `board_suggestions_changed {service_date}`.

#### K10.3 Client → server

`{"type": "presence", "focus_truck_id": string|null}` every 15 s and on focus change; expiry 45 s. Handled by a handler passed to `_ws_loop`. Presence is in memory only; on a multi-instance deployment it would be per instance (backlog B3); correctness does not depend on it.

#### K10.4 Order and compliance listeners

A NEW `BoardOrderListener` subscribes through `OrderService.subscribe` to `order.cancelled`, `order.on_hold`, `order.scheduled`, `order.dispatched`, `order.in_transit`, `order.delivered`, `order.failed`. For each event it finds drafts for today and later that contain the order (`search_documents` on `dispatch_board_drafts` with `tenant_id`, `service_date >= today` in the tenant zone (K2.5), `term order_ids = <id>`, `_source: ["service_date", "order_index"]` to find the owning lane, size 15; the Postgres translator matches `term` inside a keyword array, `persistence/document_query.py` `_containment` L293), sets `checks_stale` on the owning lane with an `atomic_update` that does not bump the version, and broadcasts `board_lane_stale`. Newly placed orders need no server hook: the board client subscribes to `/ws/orders` through `useOrdersWebSocket` (`order_placed`, `order_status_changed`, `order_assigned`) and refetches the order tray, debounced 1 s like `DispatchCockpit`'s `WS_REFRESH_DEBOUNCE_MS`. To make that lookup indexable, the draft doc also stores `order_ids: [keyword]`, a flat list the engine keeps equal to the keys of `order_index`. Handler exceptions are caught and logged at WARNING; order processing is never affected (the existing subscriber contract).

Compliance changes (qualification, certification, HOS) have no subscriber hook today; their effect appears through the 60 s re-validation in the snapshot read and the 30 s context TTL. Backlog B4.

#### K10.5 Client handling (R15.6–15.7)

`useDispatchBoardSocket` wraps `useWebSocket` (reconnect with backoff, token refresh). While disconnected, the board polls `GET` every 30 s and shows "Live updates paused". On reconnect it refetches once. Incoming lane payloads for a lane with a pending command are queued; when the last pending command for that lane settles, the board applies whichever lane has the higher `version` (server answer or queued event).

Telematics: the board reuses `useFleetWebSocket` (`/api/fleet/live`) for truck positions on the map and for the "Running late" badge: lateness = now − ETA of the next unpinned stop, when the truck is not within 200 m of that stop; threshold 15 min (Q8). Computed client-side from server ETAs; display only, no validation effect.

Plan execution: `usePlanExecutionSocket` (`execution_update`) updates stop progress on published lanes; the board refetches the lane when a stop becomes pinned.

### K11. API surface (NEW unless stated)

Router `fuel/api/dispatch_board_endpoints.py`, prefix `/api/fuel/board`, `ROUTER_AUTH_POLICY = "jwt_required"`, tenant from `get_tenant_context`. Responses use the existing `{data, request_id}` envelope and `AppException` error shape.

#### K11.1 Guard order (review findings 4 and 17)

Each route declares `dependencies=_guards(strict)`, where `_guards(strict) = [Depends(board_mode_guard(strict)), Depends(roles_dependency("admin", "dispatcher"))]`. FastAPI resolves a route's dependency list in order, so the flag check runs before the role check, as `auth/router_guards.py` L30–33 asks: a user of any role in a `disabled` tenant gets 404, never 403.

- `board_mode_guard(strict=False)` (reads: `/status`, snapshot, validate, history, publish status): `get_overlay_state("dispatch_board", tenant)`; `disabled` (which also covers unset and Redis errors on this read) → 404 `DISPATCH_BOARD_DISABLED`. It stores the mode on `request.state.board_mode`.
- `board_mode_guard(strict=True)` (writes: commands, publish, suggestion reject): the strict block from K4.2 step 2. Unset → `disabled` → 404; Redis error → 503 `DISPATCH_BOARD_MODE_UNAVAILABLE`; `shadow` → 409 `DISPATCH_BOARD_READ_ONLY`.

Tests: driver role in a `disabled` tenant gets 404; driver role in an active tenant gets 403; unset flag → 404 on reads and on commands; Redis error → 404 on reads and 503 on commands.

| Method and path | Purpose | Success | Errors |
|---|---|---|---|
| `GET /status` | Mode for tab visibility | 200 `{mode}` (`shadow`, `active_gated` or `active_auto`) | 404 disabled or unset (client hides the tab), 403 wrong role (client hides the tab) |
| `GET /{service_date}` (`?lanes=&call_type=&product=&window=`) | Snapshot (K5) | 200 | 404 disabled, 422 bad date/range/filter, 503 store |
| `POST /{service_date}/validate` | Dry run (K3) `{item, candidates[≤60], position?}` | 200 `{results}` | 404, 422, 503 |
| `POST /{service_date}/commands` | One command (K4) | 200 `{draft_version, lanes, checks}` | 404, 409 conflict/read-only/publishing/undo-stale/idempotency, 422 blocked/validation, 503 |
| `POST /{service_date}/publish` | Start publish (K7); with `dry_run: true`, preview for the review dialog (K7.1) | 202 `{publish_id, lanes, groups}`; dry run 200 preview | 404, 409 not-ready/conflict/read-only (a dry run reports not-ready lanes in its 200 body instead), 422, 503 |
| `GET /{service_date}/publish/{publish_id}` | Publish status | 200 | 404 |
| `POST /{service_date}/suggestions/{plan_id}/reject` | Reject suggestion (K9) | 200 | 404, 409 read-only, 422 |
| `GET /{service_date}/history` (`?truck_id=&cursor=&size≤50`) | Command log (R22.3) | 200 `{items, next_cursor}` | 404, 422 |
| `GET /api/ops/admin/feature-flags/{tenant_id}/dispatch-board` and `POST /api/ops/admin/feature-flags/{tenant_id}/dispatch-board/{new_state}` (MODIFY `fuel/api/feature_flag_admin_endpoints.py`, router prefix `/api/ops/admin` L28) | Flag admin, mirroring the order-intake pair (L57, L102) including its scope and role checks | 200 | 403, 422 bad state, 503 |
| `WS /ws/dispatch-board?service_date=` | Events (K10) | — | close on auth/role failure |

Frontend client `services/dispatchBoardApi.ts`: `getBoardStatus`, `getBoard(date, lanes?)`, `validateBoard(date, body, signal)`, `sendBoardCommand(date, cmd)`, `publishBoard(date, body)`, `previewPublish(date, body)` (same endpoint, `dry_run: true`), `getPublish(date, id)`, `rejectSuggestion(date, id, reason)`, `getBoardHistory(date, params)`. Each maps errors through `apiErrorFromResponse` and exposes `code` and `details.reason` for the UI.

### K12. Driver manifest fix (R25, MODIFY `driver/services/work_service.py`)

`_fetch_loading_plan(tenant_id, asset_id, run_id)`: when `run_id` is set, query `mvp_load_plans` with `tenant_id`, `truck_id = asset_id`, `term run_id = run_id` OR `term plan_id = run_id` (the executor's run id is the plan's `run_id` else `plan_id`), `status in [scheduled, dispatched, completed]`, excluding `superseded`; sort `created_at desc`, size 1. When `run_id` is empty, keep today's query but add the same status filter. Callers pass the order's `assigned_run_id` (already used by `_fetch_route_plan`). This lands in Phase 0, before the board can create plans (G9). Agent plans in `proposed` stop leaking into the manifest as a side benefit; plans approved through the MVP Approve path are `scheduled`/`dispatched` and keep working.

### K13. Feature flag and rollout (R1, N6)

- Key `dispatch_board` in the existing overlay-state store (`FeatureFlagService.get_overlay_state[_strict]` / `set_overlay_state`), states `disabled | shadow | active_gated | active_auto`, default `disabled` (no per-tenant value → `disabled`, not `settings.overlay_default_mode`, so a global default change can't switch it on).
- Reads (`GET` snapshot, `/status`, validate, history) use the non-strict read; writes (commands, publish, reject) use the strict read, treat unset as `disabled` (404) and fail closed with 503 only on a store error (K4.2 step 2, K11.1).
- Rollout: staging `demo-tenant` `shadow` (compare board validation with agent outcomes, no writes), then `active_gated` for staging QA with `QA-` fixtures, then per-tenant pilot. The old tabs stay. Turning the flag to `disabled` mid-day leaves drafts in place (they are inert) and published plans untouched; turning it back on restores the board as it was.
- Kill switch: `disabled` hides the tab within one `/status` poll (client polls `/status` every 5 min and on focus; a 404 hides the tab).

### K14. Frontend architecture (R2–R7, R18–R21)

#### K14.1 Composition

`DispatchPage` lazy-loads `DispatchBoard`. `DispatchBoard` reads `date`, `shift`, `zoom`, `density`, filters from the URL (`useSearchParams`) with local-storage defaults (R4.4), fetches the snapshot, opens the socket, and renders: `BoardToolbar` (date nav, shift, zoom, density, filters, search, undo/redo, Generate plan, Publish all ready, presence, help), `OrderTray`/`DriverTray`/`TruckTray` as tabs in the left panel, `BoardGrid`, `DetailDrawer`, `PublishDialog`, and `BoardLiveRegion`.

#### K14.2 State

`boardReducer` (pure) holds `{snapshot, lanesById, pending: Map<commandId, PendingCommand>, queuedLaneEvents, selection: Set<itemKey>, placeMode: PlaceTarget|null, candidateResults, undoStack, redoStack}`. Actions: `snapshotLoaded`, `lanesReceived` (from command responses or socket; keeps the higher version per lane), `commandStarted`, `commandSettled`, `commandFailed`, `selectionChanged`, `placeModeEntered/Exited`, `candidatesReceived`. `useBoardCommands` exposes `assign`, `move`, `unassign`, `pair`, `moveLoad`, `setTerminal`, `setAllocation`, `acknowledge`, `accept`, `revert`, `reapply`, each wrapping `sendBoardCommand` in `startTransition` with `useOptimistic` to render the moved card at its target with a "Checking…" chip (R8.6). Refusal returns the card (the optimistic state is dropped when the transition ends) and triggers an announcement and focus restore. No TanStack Query, no Zustand (F23–F24).

#### K14.3 Drag and drop adapter (`dnd/`)

- Payloads (`dragData.ts`): `{kind: "order"|"stop"|"load"|"driver"|"truck", ids, fromTruckId?, fromLoadId?}` attached with Pragmatic `draggable({ getInitialData })`, typed guards on drop targets (`canDrop` rejects wrong kinds: a driver only drops on driver slots; a truck only on the grid's "new lane" area).
- Drop targets: lane header (best fit), insertion slots between stop cards (exact position, `hitbox` closest-edge for left/right), load terminal chip (move load), driver slot, order tray (unassign), "To reassign" shelf.
- Time position: Timeline zoom maps `location.current.input.clientX` to an insertion index by nearest stop gap (snap), never to a free time; there is no free-time placement because ETAs are computed, not chosen.
- Feedback on the target, not on the preview (F14): lane highlight + `CheckChip` from `candidateResults`, insertion line, ghost card outline. A small custom preview chip ("3 orders · 9,000 gal") via `setCustomNativeDragPreview` keeps Windows dimming irrelevant.
- `useBoardAutoScroll` registers the grid's vertical and horizontal scroll containers with the auto-scroll package.
- On drag start: `validateBoard(candidates = rendered lanes)`. On target change: debounced position validate. On drop: the same `useBoardCommands` call the menu uses, with `input_modality: "drag"`.
- Touch: Pragmatic uses native drag, which on iOS/Android starts on long press; the card body is a tap target for selection, and only the grip starts drags (R20.1). Verified by the T1 spike.

#### K14.4 Non-drag paths (R18, SC 2.5.7)

- **Card menu** (`Button` + menu built on `components/ui` primitives, `aria-haspopup="menu"`): Assign to truck…, Move to…, Move earlier, Move later, Move to first stop, Move to last stop, Unassign, Pair with truck… (driver chips), Accept/Reject (suggestions), Details.
- **`AssignToMenu`**: a `Modal`-based searchable list (reusing `SearchableSelect` patterns) of lanes with outcome icons from one `validateBoard` call; blocked lanes are `aria-disabled="true"` with the reason in their accessible name and visible text; choosing a lane calls the command with best fit, and a second step "Position…" lists insertion points with ETA deltas.
- **Place mode**: Enter/Click on a card selects it (`aria-pressed`), a banner says "Placing Order 1042. Choose a truck or press Escape"; lane headers and slots become buttons; Escape cancels. Multi-select with Shift/Cmd-click or Space on a focused card.
- All three call the same `useBoardCommands` functions; tests assert identical command payloads (except `input_modality`).

#### K14.5 Keyboard model (R18.4–18.6)

The grid is one `role="grid"` with roving `tabindex`: rows are lanes (`role="row"`, header cell + cells for stops), Up/Down move between lanes, Left/Right between cells, Home/End to the lane header/last stop, Enter opens the card menu, Space toggles selection. Trays are `role="listbox"` with the same roving pattern. Shortcuts per R18.5 are registered on the board root with `keydown`, ignored when the event target is an input, textarea, select or `contenteditable`; a settings toggle "Single-key shortcuts" (default on) disables the letter keys (SC 2.1.4). Focus after an action moves to the card at its new position (lookup by `itemKey` after the reducer applies the lane), else back to the origin cell. Focus indicators use the existing focus ring tokens and are never clipped by sticky headers (SC 2.4.11: the grid applies `scroll-margin` equal to the sticky header sizes).

#### K14.6 Announcements (R19)

`BoardLiveRegion` renders two visually hidden regions (`aria-live="polite"` and `"assertive"`, `aria-atomic="true"`) at mount. `announce.ts` builds strings from command results: `"{item} {verb} {target}, load {n}, stop {k}. {fill sentence}. {worst check sentence}."`, conflicts: `"{truck} was changed by {name}. Your change was not applied."`. During drag with a pointer, Pragmatic's `live-region` package is not used for hover chatter (too noisy); only results are announced. Consecutive identical messages append a zero-width space so screen readers re-announce.

#### K14.7 Visual language

`CheckChip`: pass = `--rs-color-success-*` + check icon + "OK", warn = warning tokens + triangle + short reason, block = error tokens + octagon + short reason, info = neutral + info icon; text is always present (R19.4). `CompartmentGauge`: one segment per compartment by position, width proportional to capacity, fill height = planned litres / capacity, product code label, hatch for `needs_cleaning`, red outline for blocked during hover; text alternative "Compartment 2, diesel, 92% full". Lane states use a left border colour and a text badge (Draft/Published/Modified/Publishing/Failed/Recovering). "Recovering" is a lane in recovery (K2.1): read-only, with Retry as its only action, and for a forward recovery the assertive "drivers can't see their routes" banner (K8.4, Q16).

#### K14.8 Responsive and tablet (R20)

≥ 1024 px: three-panel layout. < 1024 px: stacked layout, Sequence zoom forced, tray as a bottom sheet (`Modal` variant with `useDialogA11y`), drawer full screen, Place mode banner pinned to the bottom. Drag handles get `min-w-11 min-h-11` (44 px) under `@media (pointer: coarse)`; all other targets ≥ 24 px in every density.

#### K14.9 States (R21)

Skeleton lanes (6 grey rows) and tray skeletons while loading; `EmptyState` for no trucks (link to Truck Compartments), no orders (link to Orders), all planned; `LoadErrorState` from `classifyLoadError` for snapshot failures (module disabled maps `DISPATCH_BOARD_DISABLED` via `MODULE_DISABLED_NAMES` — add `DISPATCH_BOARD_DISABLED: "Dispatch Board"`); degraded-source banner from `degraded_sources`; toast via `useToasts` for conflicts, publish results and reject failures.

#### K14.10 Map (R17)

`LaneRouteMap` renders terminal, ordered stop markers with numbers, polyline in stop order, and the truck position from `useFleetWebSocket`. Selecting a marker sets the board selection (shared reducer), and vice versa. Split view renders all visible lanes' polylines with per-lane colour from a fixed 12-colour palette with AA contrast on the map tiles; lanes beyond 12 reuse colours with a dashed line. The map is not a drop target. Without a Google Maps key (the existing env var), the Map tab shows an `EmptyState`.

### K15. Performance (N1)

- **Backend:** snapshot does ≤ 9 parallel reads (including the one-doc priority read, K5) plus one validation context; the priority join is an in-process dict lookup over ≤ 1,000 orders; per-lane validation is in-memory. Draft document size bound: 60 lanes × 30 stops × ~1 KB ≈ 1.8 MB worst case; lanes beyond 60 or stops beyond 30 per load are refused (`VALIDATION_ERROR`, `limit_exceeded`), keeping `atomic_update` payloads bounded. Command log is separate so the draft does not grow with history.
- **Validation cost:** context TTL cache (30 s) makes drag-start batch calls mostly cache hits; `validate_candidates` reuses one context across candidates.
- **Frontend:** vertical windowing by fixed lane height (`Math.floor(scrollTop / laneHeight)` ± 5 overscan), horizontal content limited to the shift window; `Lane` and `StopCard` are `memo` keyed by `lane.version`; drag-over state is kept in a per-lane ref + `useSyncExternalStore` store so a hover re-renders only the hovered lane; no layout reflow during drag (only an absolutely positioned indicator moves). Pragmatic tolerates targets mounting/unmounting mid-drag (F11).
- **Measurement:** backend latency metrics (K16); a Playwright perf check with a 60-lane fixture records drag frame times through `performance.measure` marks and fails above 20 ms p95 frame time on CI hardware (advisory on CI, enforced on the reference laptop run before rollout).

### K16. Telemetry (R24)

Through `telemetry/service.py` `record_metric(name, value, tags)` and structured logs, tags `tenant_id`, `endpoint`, `outcome`:
`board.snapshot.ms`, `board.validate.ms` (tag `kind=batch|position`), `board.command.ms` + `board.command.count` (tags `type`, `result`, `input_modality`), `board.conflict.count`, `board.override.count` (tag `reason_code`), `board.publish.lane.ms` + `.count` (tags `stage`, `result`), `board.redispatch.count`, `board.suggestion.count` (tag `action=accept|partial|reject`), `board.undo.count` (tag `type=revert|reapply`, `result`). No customer names, addresses or free-text reasons in metrics or logs (R24.3). Client-only events (cancelled drags) are not measured; backlog B5.

### K17. Audit trail (R22)

- Command log (K2.2) for every committed and refused command.
- Publish: `ActivityLogService.log` entries (`action_type: "dispatch_board_publish"`, per lane `{truck_id, plans, orders, driver_id, result}`) and `TelemetryService.log_audit_event("dispatch_board", user_id, "board_lane", truck_id, "publish", details)`; orders get the executor's `order_assigned` and status events and dispatch's events; re-publish adds `order_reassigned` events (K8.3).
- History tab reads `GET /history` filtered by `truck_id`, newest first, showing actor name, time, command summary, check outcome and override reasons (escaped text).
- Actor ids always come from `TenantContext.user_id` (HTTP) or `claims["sub"]` (socket).

#### K17.1 Actor display names (review finding 7)

Session claims carry no display name. `DispatchBoardService.resolve_actor_name(tenant_id, user_id) -> str` (NEW, in `dispatch_board_service.py`):

- Query `SELECT email FROM auth_users WHERE st_user_id = :user_id AND tenant_id = :tenant_id` through `persistence.database.session_scope` and `sqlalchemy.text` (the pattern in `auth/password_admin.py` L116–123), bound parameters only.
- Return the email local part (text before `@`), trimmed to 64 characters. No row, a different tenant, a DB error or dormant persistence → `"Another dispatcher"` (DB errors log WARNING with the exception type only).
- Cache per `(tenant_id, user_id)` for 5 minutes in an in-process `TTLCache` (the K3.1 helper); the tenant id is part of the key, so a user from another tenant is never resolved.
- Called once per command (stored as `actor_name` on the command log at write time, so History doesn't re-resolve), once per socket connection (presence), and on publish (activity log).

Names are only returned to the same tenant's `admin` and `dispatcher` users (the board's own guards). Proposed default, reversible: when a profile display name exists later, the helper switches to it with no API change. Tests: name resolved from the email; fallback when no row; a user id bound to another tenant returns the fallback; second call within 5 minutes does no query.

### K18. Accessibility conformance approach (N5)

The design targets WCAG 2.2 AA. Automated checks: `jest-axe` is not installed, so the plan uses Playwright's accessibility snapshot plus explicit RTL role/name assertions; adding `@axe-core/playwright` is optional and, if added, pinned (plan task 40). Manual: VoiceOver + Safari (macOS and iPadOS), NVDA + Firefox, keyboard-only pass, 200 % zoom, Windows High Contrast. Full conformance needs this manual testing and expert review; the plan records results in the task evidence file.

## External input validation

All validation is in Pydantic models (`extra="forbid"`), but the models are **not** FastAPI body parameters, because FastAPI's default `RequestValidationError` handler echoes `input` (for a missing field, the whole body), and board bodies carry free-text reasons (review finding 5). Every board POST takes `body: Any = Body(...)` and validates inside the handler with one shared helper:

```python
_IDEMPOTENCY_FIELDS = {("client_command_id",), ("client_request_id",)}

def parse_body(model: type[T], body: Any) -> T:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        fields = [".".join(map(str, e["loc"])) for e in errors]
        missing_key = any(e["type"] == "missing" and tuple(e["loc"]) in _IDEMPOTENCY_FIELDS for e in errors)
        raise AppException(
            ErrorCode.MISSING_IDEMPOTENCY_KEY if missing_key else ErrorCode.VALIDATION_ERROR,
            "The request is not valid.", status_code=422,   # both codes default to 400
            details={"fields": fields, "reason": errors[0]["type"] if errors else "invalid"},
        ) from None
```

A missing `client_command_id` or `client_request_id` therefore maps to 422 `MISSING_IDEMPOTENCY_KEY`; a present but malformed one (not a UUID) stays 422 `VALIDATION_ERROR`. Both carry field paths only.

The response carries field paths and Pydantic error types only, never `input`, `ctx` or `msg` (messages can quote values). `VALIDATION_ERROR` defaults to 400, so `status_code=422` is explicit. Path and query parameters stay as FastAPI parameters (they carry ids and dates, not free text). Test (task 13): a 501-character `reason` returns 422 `VALIDATION_ERROR`; a body missing `client_command_id` returns 422 `MISSING_IDEMPOTENCY_KEY`; a publish body missing `client_request_id` (not a dry run) returns 422 `MISSING_IDEMPOTENCY_KEY`; in every case the response bytes contain neither the reason text nor any body value.

| Input | Rules | On failure |
|---|---|---|
| `service_date` (path) | ISO date; within [today − 7, today + 14] in tenant time zone | 422 `reason: date_out_of_range` |
| `lanes` (query) | ≤ 60 comma-separated ids, each 1–128 chars `[A-Za-z0-9_.:-]` | 422 |
| `call_type` (query) | optional; one of `keep_full`, `auto_fill`, `will_call`, `one_off` | 422 `reason: invalid_filter` |
| `product` (query) | optional; 1–32 chars `[A-Za-z0-9_-]` | 422 `reason: invalid_filter` |
| `window` (query) | optional; one of `overdue`, `today`, `later` | 422 `reason: invalid_filter` |
| `client_command_id`, `client_request_id` | required (`client_request_id` is optional and ignored when `dry_run: true`, K7.1), UUID string | absent → 422 `MISSING_IDEMPOTENCY_KEY` (existing code, status set by `parse_body`); not a UUID → 422 `VALIDATION_ERROR` |
| `dry_run` (publish) | optional bool, default `false` | 422 |
| `expected_lane_versions` | required for every touched lane; int ≥ 0; ≤ 60 entries | 422 `reason: missing_expected_version` |
| `truck_id`, `driver_id`, `order_id`, `load_id`, `terminal_id`, `compartment_id` | 1–128 chars, same charset; existence and tenant ownership checked in the service (not found → 422 `reason: unknown_<kind>`; foreign tenant indistinguishable) | 422 |
| `order_ids` | 1–25 unique | 422 |
| `target.index` | null or 0 ≤ index ≤ stops length | 422 `reason: index_out_of_range` |
| `shares` | ≤ 12 entries, `liters > 0`, total ≤ order planned litres + 1 % | 422 `reason: invalid_shares` (rule violations beyond that are checks, not 422) |
| `reason`, `warning_reasons[*]` | trimmed 3–500 chars, no control characters except newline | 422 |
| `candidates` | 1–60 truck ids | 422 |
| `input_modality` | enum; optional, default `menu` | 422 |
| `shift_id` | one of snapshot shifts | 422 |
| Flag `state` (path) | one of the four states | 422 (as existing) |
| WS `service_date` | as path rule | close 1008 |
| WS client messages | JSON ≤ 1 KB, `type == "presence"`, `focus_truck_id` null or id charset | ignored + DEBUG log |

## Error handling per operation

| Operation | Failure | Recoverable? | Caller receives | Log |
|---|---|---|---|---|
| Snapshot | flag read error | yes | non-strict read → defaults `disabled` → 404 | WARNING (logged by `FeatureFlagService`) |
| Snapshot | actor/suggested-driver lookup fails | yes | 200; `suggested_driver: null`, names fall back to "Another dispatcher" | WARNING |
| Snapshot | document store down | yes | 503 `ELASTICSEARCH_UNAVAILABLE` (existing) | ERROR |
| Snapshot | a validator source down | yes | 200 with `degraded_sources` and `check_unavailable` checks | WARNING once per source per request |
| Snapshot | check write-back conflict | yes | 200 with fresh checks (not persisted) | DEBUG |
| Validate | source down | yes | 200 with `check_unavailable` | WARNING |
| Validate | client aborted | n/a | — | none |
| Command | stale version / cross-lane rule | yes | 409 `BOARD_LANE_CONFLICT` + current lanes | INFO |
| Command | block | yes | 422 `BOARD_COMMAND_BLOCKED` + checks | INFO (audit doc) |
| Command | publishing lane | yes | 409 `BOARD_PUBLISH_IN_PROGRESS` | INFO |
| Command | flag unset | yes | 404 `DISPATCH_BOARD_DISABLED` | none |
| Command | flag store error | yes | 503 `DISPATCH_BOARD_MODE_UNAVAILABLE` | WARNING |
| Command | duplicate `client_command_id` (concurrent or retry) | yes | 200 with the committed response, or `already_applied: true` | DEBUG |
| Command | same id, different payload | no | 409 `IDEMPOTENCY_CONFLICT` | INFO |
| Command | invalid body | no | 422 `VALIDATION_ERROR` with field paths only | INFO (no values) |
| Command | store write fails | yes | 503; draft unchanged (atomic) | ERROR |
| Command | log write fails after commit | yes | 200 `audit_degraded: true` | ERROR |
| Command | broadcast fails | yes | 200 (others catch up by poll/refetch) | WARNING |
| Undo/redo | stale | yes | 409 `BOARD_UNDO_STALE` + reason | INFO |
| Publish preflight | lane not ready | yes | 409 `BOARD_PUBLISH_NOT_READY` per-lane reasons | INFO |
| Publish dry run | lane not ready, or a validator source down | yes | 200 with `ready: false` and per-lane reasons, or `check_unavailable` reasons; nothing claimed | INFO / WARNING per source |
| Snapshot | priority read fails or times out | yes | 200, tray in window order, `degraded_sources: ["delivery_priorities"]` | WARNING |
| Publish worker | executor failure | per `retryable` | lane `failed {stage: apply, reason, writes_made}` + event | WARNING (ERROR if `writes_made`) |
| Publish worker | dispatch 409 | yes | lane `failed {stage: dispatch, reason}`; orders stay `scheduled` and linked to the board plan | WARNING |
| Publish worker | plan/route doc write fails | yes | lane `failed {stage: plan_write, writes_made: false}` | ERROR |
| Publish worker | process crash | yes | lane shows `failed {reason: interrupted}` after lease expiry | (none at crash) |
| Publish worker | finalize CAS refused (lease taken) | yes | worker stops that lane silently; the new attempt owns it | WARNING |
| Re-publish | relink refused (phase 2) | yes | rollback, then `failed {stage: relink, order_id, observed_status, rolled_back: true, writes_made: false}`; lanes editable; Retry is a fresh publish | WARNING |
| Re-publish | retire refused (driver checked in first, phase 1) | yes | rollback, then `failed {stage: retire, reason: load_started_concurrently, rolled_back: true}`; Retry reclassifies the load as an amend | WARNING |
| Re-publish | any error in phases 1–2 (store, worker exception) | yes | rollback, as above | ERROR if it's an exception |
| Re-publish | rollback step fails | yes | `failed {stage: restore, recovery: rollback, retryable: true}`; lanes read-only; Retry runs rollback first | ERROR |
| Re-publish | rollback finds an order in a state the guards exclude | yes | `failed {stage: restore, reason: recovery_inconsistent}`; metric `dispatch_board.recovery_inconsistent` | ERROR |
| Re-publish | executor or dispatch failure in phase 3, amend write failure in phase 4 | per reason | 3 in-place retries; then `failed {stage, reason, recovery: forward, writes_made: true}`; lanes read-only; Retry resumes at `attempt.phase` | WARNING; ERROR after the retries |
| Re-publish | order became terminal before dispatch, or tray order refused by executor | yes | drop rule: order removed from the staged load, publish continues; `last_result.dropped_orders` | INFO |
| Re-publish | forward recovery pending | yes | lane banner "{n} drivers can't see their routes until this publish completes" | WARNING every 5 min |
| Check-in | execution superseded concurrently (CAS refusal) | yes | same `ValueError` → response as "plan not dispatched" today | as today |
| Check-in | stop renumbered or gone concurrently | yes | 404 `RESOURCE_NOT_FOUND`, as today | as today |
| Driver transition | order changed since the driver's read | yes | 409 `ORDER_CHANGED_CONCURRENTLY`; offline queue retries | INFO |
| Driver transition | `in_transit` on a board run whose plan isn't `dispatched`, or plan read fails | yes | 409 `BOARD_ROUTE_UPDATING`; offline queue retries | DEBUG (WARNING on read failure) |
| Re-publish | cache invalidation fails | yes | success; stale bundle expires within 60 s | WARNING (inside `invalidate`) |
| MVP approve/reject/replan on a board plan | — | no | 409 `BOARD_OWNED_PLAN` | INFO |
| Re-publish | notification fails | yes | success with `notifications_failed: [driver_id]` | WARNING |
| Reject suggestion | approval reject fails | yes | 200 `{dismissed: true, approval_rejected: false, reason}` | WARNING |
| Socket | auth/role failure | no | close via `_reject` | INFO |
| Socket | send failure | yes | connection dropped by base manager | DEBUG |
| Order listener | any exception | yes | none (order processing unaffected) | WARNING |
| Client | network/timeout on command | yes | card returns to origin; toast "Not saved, retry"; same `client_command_id` reused on retry | console only |

User-facing messages are templates over ids and numbers; exception text never reaches responses.

## Invariants and their owners

| # | Invariant | Owner | Why there |
|---|---|---|---|
| I1 | An order is in at most one lane (stops or shelf) of a draft | Engine (pure) + `atomic_update` transform re-check | Only the transform sees the latest stored draft under the row lock |
| I2 | No commit uses validation computed on a different lane version | Transform version CAS | Validation is async and outside the lock |
| I3 | No `block` check exists on a committed lane | Command handler (validate before commit) | Backend is authoritative (D2); exception: blocks that appear later from outside changes (R10.5), surfaced not prevented |
| I4 | Order status and links change only at Publish/re-publish | Board services never call order writes outside `dispatch_board_publish.py` | D4; reviewed by a grep test (T-B10) |
| I5 | A dispatched order is never moved back to an earlier status | `order_state_machine` (existing) + relink primitive never writes status | State machine is the single owner today |
| I6 | A dispatched order's links change only through a full from-link CAS | `FuelOrderRepository.relink_dispatched_assignment` | Repository owns order document writes |
| I7 | Publish content equals the claimed lane version | Lane `publishing` claim + command refusal | Single draft doc gives atomic claims |
| I8 | One driver per lane per overlapping window; one lane per driver | Engine check `driver_double_booked` + transform re-check | Cross-lane rule |
| I9 | Tenant isolation | Router tenant context + doc ids prefixed by tenant + every query filtered by `tenant_id` | Existing pattern |
| I10 | In a redispatch group, drivers are notified only after every operational write of the group succeeds and the work cache is invalidated. In a first-publish group, each `send_assignment` follows that load's own committed writes | `BoardRedispatchService` phases (K8.4) + `dispatch(notify=False)` (K7.4); freeze rules 4, 7, 8 | Avoids telling a driver about a state that then fails; only first-publish groups, which revoke nothing, let dispatch notify |
| I17 | Every dispatched order that changes lane or load is relinked before the executor sees its new plan | `BoardRedispatchService` phase 2 (the only path for groups holding dispatched orders, freeze rule 7) | The executor and dispatch refuse an order linked to another run |
| I11 | Board never changes `drivers_current.assigned_truck_id` | Board services never call driver writes | D6 |
| I12 | Plans written by the board are invisible to drivers until applied, and superseded plans are invisible after | R25 status filter in `work_service` | Manifest resolution owner |
| I13 | A command id commits at most once, and its committed audit record is never replaced | `applied_commands` in the draft transform + `create_document` for the log | Only the draft row lock can serialize duplicates; insert-if-absent can't overwrite |
| I14 | Board plans are acted on only by the board | `mvp_endpoints` 409 `BOARD_OWNED_PLAN` guard + `source` filters in list and exception replanning | Each existing surface owns its own write path |
| I15 | An order is on at most one service day's draft (best effort) and published for at most one day (strict) | K5.1 lookup in the engine (best effort) + executor `order_committed_elsewhere` (strict) | Drafts are separate documents; only the order document is shared |
| I16 | Route stop sequence numbers on a route are never reused | `BoardRedispatchService` amend (fresh numbers above max, computed inside the execution transform) + new revision for every not-started change + `record_checkin` re-locating its stop inside its own transform (freeze rule 9) | Stale driver views must 404, not hit a different stop; only a CAS on both sides makes that hold under concurrency |
| I18 | Every write to a plan execution is a compare-and-set on the stored document | `atomic_update` in `record_checkin` (K8.6) and in the board's retire and amend | The execution document is the only place both the driver and the board write stop state |
| I19 | No driver can start an order while it is linked to a board plan that isn't `dispatched`, and no driver write based on a stale read is applied | Board-plan gate (`order_transition_service`) + guarded transition (`transition_endpoints`, K5a) | Driver transitions are the only order writes the board doesn't make itself; the gate stack is where they are already refused |
| I20 | After any failed redispatch attempt and its recovery, every non-terminal order of the group is linked to a plan in `dispatched`, `scheduled` or `completed` | `BoardRedispatchService` Recovery (rollback before apply, forward from apply) keyed on `publish.attempt.phase` | Only the attempt record knows which writes were made (P12) |

## Correctness properties (property-based, `hypothesis`)

- **P1 Exclusivity.** For any sequence of valid commands applied by the engine to any draft, every order id appears at most once, and `order_ids == keys(order_index)`.
- **P2 Undo round-trip.** For any committed command C on a draft D, `revert(C)` applied to `apply(D, C)` yields lanes whose `content_hash` equals D's; `reapply` then yields `apply(D, C)`'s hashes.
- **P3 Validation determinism.** `validate_lane(ctx, lane)` is a pure function: same context and lane give the same checks in the same order.
- **P4 Monotone tiers.** Adding a stop to a lane never turns a `block` from `compartment_fit` into `pass` (capacity and segregation are monotone in added demand).
- **P5 Best fit soundness.** `best_insertion_index` returns an index whose added time is minimal among indices with no `eta_after_window`, when such an index exists.
- **P6 Version CAS.** For any two commands built from the same draft version that touch a common lane, at most one commits (simulated with a fake store implementing `atomic_update` semantics).
- **P7 Content hash.** `content_hash` ignores checks, versions, ETAs and publish fields, and changes when any content field changes.
- **P8 Change classification.** For any published content, draft content and set of pinned stops, `plan_changes` classifies each load into exactly one of unchanged/new/new-revision/amend/removed; "new revision" iff the load is published, not started and changed; "amend" iff it is published, started and changed; "started" iff some order is `in_transit`, `delivered` or `failed` or `completed_stops > 0`; `driver_may_be_loading` appears iff the load is a new revision with `planned_start < now`.
- **P11 Publish groups.** For any set of lanes and dispatched-order moves, the group partition covers every claimed lane exactly once, every lane holding a dispatched order is in a redispatch group, and the source lane of every moved dispatched order is in the same group as its destination.
- **P9 Amend sequences.** For any started route and any reorder or removal of its unpinned stops, the amended stops keep every pinned stop's sequence, all sequences are unique, and no new sequence is ≤ the old maximum.
- **P12 Recovery (pass 3 finding 2).** For any redispatch group, any failure point (after any write of phases 1–4, or a crash), and any interleaving of driver `in_transit`/`failed` transitions and check-ins that the gate and guards admit (simulated with a fake store implementing `atomic_update` and the K5a guard), running Recovery until it reports done leaves:
  - every non-terminal order of the group linked to a plan whose status is `dispatched`, `scheduled` or `completed`, whose route and execution (if any) contain that order's stop;
  - no plan, route or execution with `superseded_by_attempt` set and a status other than `superseded`;
  - no relinked order whose links equal neither its `from` nor its `to` link.

  Running Recovery a second time changes nothing.
- **P10 Route shape.** For any load, the route document built by the publish service validates against `MVP_ROUTES_MAPPING` (every key path declared) and has `station_id == customer_tank_id or customer_id` and one `order_ids` entry per stop.

Frontend reducer properties are covered with table-driven Jest tests (no `fast-check` is installed and none is added): higher version wins, pending lanes hold events, refusal restores origin.

## Test plan

Backend (`Runsheet-backend/tests/unit/` unless noted):

- `test_dispatch_board_engine.py`: every command type, best fit, exclusivity, limits, P1, P2, P5, P7 (hypothesis).
- `test_dispatch_validation.py`: each catalogue row with fakes for every validator; unavailable sources (block vs warn split); P3, P4; extracted `build_route_requirements`/`estimate_route_hours` equal the agent's prior outputs (golden).
- `test_dispatch_board_service.py`: command sequence K4.2 steps (idempotent replay, same id different payload, concurrent duplicate ids → one committed log doc and a second response that equals the committed one or is `already_applied` with the same lane versions, crash between commit and log → `already_applied`, conflict early exit, transform refusal, block refusal, audit-degraded path, read-only and past-day refusals), context built before `engine.apply` (spy order), snapshot assembly, tray filters only after truncation, `on_hold` orders in `trays.orders` with `draggable: false` and `block_reason: "on_hold"` and an `assign_orders` for one blocked with `order_state on_hold`, tray order with and without a priority doc (scored before unscored, then window, then id; a doc older than 24 h ignored; priority read failure → window order and `degraded_sources`), the truncation boundary (1,001 matches keep the 1,000 earliest windows, no-window orders drop first), cross-day exclusion (K5.1), `suggested_driver` (K5.2), HOS future-day rule, stale re-validation, history pagination, `resolve_actor_name` (K17.1), tenant time zone default (K2.5), P6 with a fake atomic store.
- `test_dispatch_board_publish.py`: first publish happy path with fakes for executor and dispatch; plan and route documents equal the K7.3a shapes (P10); snapshot refresh (changed gallons publishes with new allocations, changed product blocks); each failure stage; idempotent retry; lease expiry → interrupted; multi-load lane; claim all-or-none.
- `test_dispatch_board_redispatch.py`: classification P8 (including the started definition and `driver_may_be_loading`); amend sequences P9; group partitioning (connected components, added source lanes, never-published lane in a redispatch group); the K8.4 tests (a)–(o), including the A → B move into a never-published lane, the call-order spy (writes, then invalidations, then notifications, `notify=False` on every dispatch), zero notifications on an injected amend failure, rollback after a relink or retire refusal, forward completion after an apply failure, crash recovery by recorded phase, the drop rule and P12 (hypothesis over failure points and admitted interleavings); relink and retire refusals; driver change on a not-started load; driver change blocked on a started load.
- `test_dispatch_board_publish.py` also covers: a dry run returns groups, classes, `info` and notifications and writes nothing (store spy); a first-publish group of lanes with no dispatched orders never calls `BoardRedispatchService`.
- `test_mvp_board_plan_guards.py`: K7.6 list filter, approve/reject/replan 409, exception replanning snapshot ignores board plans.
- `test_mvp_mapping_board_fields.py`: board fields in both mapping dicts; P10.
- `test_order_relink.py`: `relink_dispatched_assignment` CAS outcomes (relinked, already, refused per mismatched field, non-dispatched status), never writes status, always advances `last_event_timestamp`.
- `test_plan_execution_checkin_cas.py` (freeze rule 9): with an interleaving fake store, a check-in that reads, then the board supersedes the execution, then the check-in commits, is refused with the "not dispatched" `ValueError`, and the status stays `superseded`. A check-in that reads, then the board renumbers, then the check-in commits, gets 404, and the renumbered `stops` survive. A double check-in gets 409 `STOP_ALREADY_COMPLETED`. The existing check-in suites pass unchanged.
- `tests/postgres/test_checkin_amend_race.py`: a concurrent check-in and amend on one execution leave either the check-in applied and the amend keeping it as completed, or the amend applied and the check-in refused with 404. Never both lost.
- `test_driver_transition_guards.py` (freeze rule 10): a stale-read driver transition after a relink → 409 `ORDER_CHANGED_CONCURRENTLY` and the relink survives. `in_transit` on a `bp-` run whose plan is `draft`, `scheduled` or `superseded` → 409 `BOARD_ROUTE_UPDATING`, and on a `dispatched` one it succeeds. An agent run is never read. A plan read error on a `bp-` run → 409. `failed` and `delivered` are never gated. The idempotency store isn't written on a 409. The existing driver transition suites pass unchanged.
- `test_plan_dispatch_explicit_driver.py`: `driver_id` path (active, inactive, foreign tenant), `notify=False` (WS manager never called, payload fields on the result), and the unchanged default path (one `send_assignment` with today's payload).
- `test_dispatch_board_mappings.py`: `DISPATCH_BOARD_INDEX_MAPPINGS` is listed in `_REGISTRIES`; `unsearchable_fields` covers `applied_commands`, `response`, `content_before`, `content_after`; the timestamp contract test passes with the new indices.
- `test_work_service_manifest_by_run.py`: R25 (proposed plan ignored, superseded ignored, run match wins over newer plan).
- `test_dispatch_board_suggestions.py`: diff producer against `ReplanDiff` fixtures; shadow hidden; reject calls approval reject.
- `test_dispatch_board_endpoints.py`: roles (admin, dispatcher allowed; driver, platform_admin-only denied), guard order (driver in a disabled tenant → 404), tenant isolation, input validation table, no echo of body values in 422s, flag states (unset → 404 on reads and commands, Redis error → 404 reads / 503 commands), flag admin paths under `/api/ops/admin`.
- `test_dispatch_board_ws.py`: auth (`sub` missing → rejected), role, disabled tenant rejected, per service-day fan-out, presence expiry, presence names.
- `tests/postgres/test_dispatch_board_atomic.py`: real `atomic_update` concurrency (two concurrent commands on one lane: one commits; two concurrent same-id commands: one committed log doc), and the order listener's `term order_ids` lookup on the real translator.
- T-B10 grep test (`tests/unit/test_dispatch_board_write_boundary.py`, plan task 15): no file under `fuel/services/dispatch_board_*.py` except `dispatch_board_publish.py` imports order write APIs.

Frontend (`runsheet/src/components/dispatch-board/**/*.test.tsx`, Jest + RTL):

- Reducer tables; `precheck.ts` never returns pass; `announce.ts` strings.
- Menu path, Place mode and keyboard path each send the same payloads as drag (drag simulated by calling the adapter's drop handler); blocked lanes disabled with reason in accessible name; focus return; live-region text; shortcuts ignored in inputs; single-key toggle.
- States: skeleton, empty, error classification, degraded banner, read-only banner.
- Publish dialog: warnings require reasons; not-ready lanes explained.

E2E (`runsheet/e2e/dispatch-board.spec.ts`, Playwright, against a mocked API route layer like the existing specs): pointer drag tray → lane, reorder, cross-lane move, driver drop, auto-scroll, touch emulation long press (iPad profile), conflict toast from a second browser context, perf check (K15).

Staging (manual, QA- fixtures in `demo-tenant`, flag baseline recorded first): shadow mode read-only, active publish to a QA driver account on the driver app, post-publish move with revocation observed in the driver app, restore flag to baseline.

## Edge cases

- **E1** Order cancelled while on a draft lane → `order_state` block on that stop; Publish not ready until removed (R10.5).
- **E2** Driver deactivated after pairing → `driver_qualification` block on re-validation.
- **E3** Same driver paired to two trucks in different shifts → allowed if load windows don't overlap; else `driver_double_booked`.
- **E4** Truck compartments edited mid-day → solver re-runs at the next validation; allocations may change; `fill_percent` info shows the new fill.
- **E5** More than 1,000 tray orders → truncated with a banner; the client then sends `call_type`, `product` and `window` filters to the snapshot so the server narrows the tray query (K5).
- **E6** Order with no coordinates → `location: null`, ETA unavailable for that load, `eta_unavailable` warn, Sequence layout.
- **E7** Agent approval approved in the queue for orders in a draft lane → `order_committed_elsewhere` block with a link to the plan (K9).
- **E8** Two dispatchers move the same order to different trucks at once → one commits; the other gets a conflict (both source lanes are in `expected_lane_versions`).
- **E9** Command log write fails after commit → board change kept, `audit_degraded`, ERROR log; history shows a gap.
- **E10** Night shift crossing midnight → the load's `service_date` is the day of its planned start; the board's Night view of day D shows 18:00 D to 06:00 D+1; a load starting at 01:00 D+1 belongs to D+1's draft (Q2).
- **E11** Time-zone change for a tenant → drafts keep their captured `timezone`; new drafts use the new zone.
- **E12** DST transition day → time axis built from zone-aware datetimes; 23- or 25-hour days render correctly.
- **E13** Publish while the paired driver's HOS turns blocked → fresh preflight blocks; nothing claimed.
- **E14** Re-publish when the driver already started the moved order (before or during the publish) → relink refused (status `in_transit`), so the group rolls back: every retired document is restored and every relinked order goes back to its published link. The lanes are `failed {stage: relink, rolled_back: true}` and editable, and the driver keeps working the restored route without noticing. The board shows the stop pinned after refetch. Retry reclassifies the load as started, so it amends it (K8.4 Recovery).
- **E15** Flag set to `disabled` during an in-flight publish → the worker finishes (drain semantics like agent-overlay Req 12.6); new requests refused.
- **E16** Suggestion for a truck that has no lane → accepting creates the lane.
- **E17** Browser offline → commands fail fast; cards return; "Not saved" toast; no local queueing (offline editing out of scope).
- **E18** Order whose window spans two service days → it shows in the tray of the first day that doesn't already hold it; once on one day's draft it is hidden from the other days' trays and blocked there with `order_on_other_day` (K5.1). Two simultaneous adds on two days can both commit; publish preflight and the executor stop the second publish.
- **E19** Future service day with a driver who is out of hours today → `info hos_not_projected`; publish is not blocked by today's HOS (K3.2).
- **E20** Order's gallons edited after insert → `info order_quantity_changed`; publish recomputes allocations. Product or destination edited → `block order_identity_changed` until removed and re-added (K7.2).
- **E21** Someone tries to approve or reject a board plan from the Fuel Distribution tab → 409 `BOARD_OWNED_PLAN` (K7.6).
- **E22** Driver checks in at a stop while the dispatcher re-publishes that load. If the check-in commits first, retire refuses and the group rolls back, and the Retry amends in place. If retire commits first, the check-in is refused as "plan not dispatched", the driver app refetches and the driver sees the new revision after notify. For a started load being amended, a concurrent check-in is kept by the amend transform as a completed stop (K8.4 phase 4, freeze rule 9).
- **E27** The driver's app sends `in_transit` based on a read taken before a relink → 409 `ORDER_CHANGED_CONCURRENTLY`. The offline queue retries, and the retry sees the new link. If the plan is staged, it gets 409 `BOARD_ROUTE_UPDATING` until phase 3 dispatches the plan (K8.6).
- **E28** A re-publish fails at apply (store outage) → 3 in-place retries, then `failed {recovery: forward}`. The lanes are read-only, with a banner saying which drivers can't see their routes. Retry resumes at `apply` and completes (K8.4 Recovery).
- **E29** An order on a staged load is cancelled through the cancel flow between executor and dispatch → it is dropped from the staged route, which is renumbered, and dispatch continues. After finalize the lane is Modified with an `order_state` block on the cancelled stop, so the dispatcher removes it (drop rule).
- **E30** A Retry is requested for only some lanes of a group in recovery → 409 `BOARD_PUBLISH_NOT_READY` with `reason: "retry_whole_group"` and the group's truck ids. The UI's Retry always sends the whole group (K7.2).
- **E23** Two requests with the same `client_command_id` at once → one commits. The second returns either the committed response or `already_applied: true` with the same lane versions; the client treats both as success (K4.2, K4.5).
- **E24** A dispatched stop is moved from published lane A to a lane B that was never published, and the dispatcher publishes B only → the preflight adds A; one redispatch group relinks the order to B's run, re-issues A as `r2`, revokes the order from A's driver and assigns it to B's driver after all writes (K8.4, freeze rule 7).
- **E25** The driver is loading at the rack but hasn't marked any order `in_transit` → the load counts as not started; a change re-issues it as a new revision, and the review dialog shows `info driver_may_be_loading` when `planned_start` is past (K8.1).
- **E26** A tenant with more than 1,000 open orders and some with no window → the tray keeps the 1,000 earliest windows and will-call orders with no window drop out first; filters narrow on the server (K5).

## Reuse map

| Need | Reused as-is | Extended | NEW |
|---|---|---|---|
| Driver qualification | `DriverQualificationService.is_dispatch_eligible` | — | — |
| Route requirements, hour estimate | — | extracted from `RoutePlanningAgent._build_route_requirements` / `_estimate_route_hours` into `dispatch_validation.py` | — |
| HOS | `HOSAdvisoryService.resolve`, `gate_verdict`; `HOSChecker.is_eligible`; HOS override endpoint | — | — |
| Certification | `AssetCertificationService.is_dispatch_eligible` | — | — |
| Compartments | `compartment_accepts`, `segregation_key`, `check_feasibility`, `optimize_loading_plan`, `check_compatibility`, `load_tenant_compatibility_rules` | — | — |
| Dyed diesel | `DyedDieselEnforcer.validate_load_plan` | — | — |
| Status/window guards | `assert_window_present_for_transition`, `is_terminal_status` | — | — |
| ETAs | `build_distance_matrix`, `compute_distance` | `recompute_etas` public alias | `dispatch_board_eta.py` |
| Stop locations | `_resolve_stop_locations`, `_query_station_locations` logic | extracted | — |
| Terminal/supply | `terminal_wait_resolver`, `contract_lift_service`, sourcing recommendations | — | — |
| Apply | `LoadingPlanExecutor.execute` (direct, `action_id=None`, explicit mode) | — | — |
| Dispatch + driver notify | `FuelPlanDispatchService.dispatch`, `send_assignment`, `order.dispatched` push | `driver_id` and `notify` params | — |
| Tray priority | `mvp_delivery_priorities` docs (`priorities[].order_id`, `priority_score`, `priority_bucket`) | — | in-process join (K5) |
| Field policy | `persistence/document_field_policy.py` `_REGISTRIES`, `unsearchable_fields` | one registry entry | — |
| Review dialog data | K7.2 preflight | `dry_run` on the publish endpoint | — |
| Post-publish | `send_assignment_revoked`, `send_new_route`, `append_event` | — | `relink_dispatched_assignment`, `BoardRedispatchService` |
| Concurrency | `atomic_update`, `create_document` (insert-if-absent) | — | lane versions, `applied_commands`, draft doc |
| Driver work cache | `DriverWorkService.invalidate` | `work_endpoints.get_work_service()` accessor | — |
| Executions | `PlanExecutionService.create_execution` via dispatch; `atomic_update` | `record_checkin` persisted by `atomic_update` (freeze rule 9) | guarded rewrite for started-load amends and retire/restore markers (K8.4) |
| Driver transitions | K5a guarded order write (`guard_stored_state`, `_guarded_upsert`), gate stack, offline-queue 409 retry | `guard_stored_state=True` on the driver endpoint; gate 0 for board runs (K8.6) | — |
| Publish recovery | deterministic ids, executor replay, dispatch idempotency | — | `RedispatchAttempt` record, rollback, forward completion (K8.4) |
| Display names | `auth_users` table (`email`, `tenant_id`, `st_user_id`) | — | `resolve_actor_name` |
| Suggested pairing | `driver_repository.search(assigned_truck_id=…)` | — | — |
| Time zone | `_get_tenant_timezone`, `TenantContext.settings` | public alias | — |
| Board plan isolation | `mvp_endpoints` plan routes, exception replanning snapshot | `source` filter, `BOARD_OWNED_PLAN` guard | — |
| Diff | `replan_diff_models.py`; `ReplanDiffBody` UI | `ReplanDiffBody` extracted to its own file | diff producer |
| Approvals | `ApprovalQueueService.list_pending`, `reject` | — | — |
| Flags | `FeatureFlagService` overlay-state store, admin endpoint pattern | new key + endpoints | — |
| Realtime | `BaseWSManager`, `_authenticate_tenant`, `_ws_loop`, `useWebSocket`, `useOrdersWebSocket`, `usePlanExecutionSocket`, `useFleetWebSocket` | — | `DispatchBoardWSManager`, `useDispatchBoardSocket` |
| Audit/telemetry | `ActivityLogService.log`, `TelemetryService.record_metric`/`log_audit_event` | — | command log index |
| UI | `components/ui` (`Badge`, `Button`, `Card`, `EmptyState`, `Modal`, `SearchableSelect`, `FilterBar`, `LoadErrorState`, `TabNavigation`), `useToasts`, `useDialogA11y`, `classifyLoadError`, `fetchWithTimeout`, `litersToGallons`, maps | `MODULE_DISABLED_NAMES` entry | board components |
| Roles | `roles_dependency`, `config/modules.ts` `Role` | — | — |

## Out of scope / backlog

- **B1** Dropping on the map and hand-drawn routes (R17.4).
- **B2** Trailers as separate assets (Q3).
- **B3** Cross-instance presence and lane fan-out (needs a shared pub/sub; staging runs one task; correctness is version-based).
- **B4** Compliance-change subscriber hooks (qualification/certification/HOS push); covered today by TTL and 60 s re-validation.
- **B5** Client-side interaction telemetry (cancelled drags, hover dwell).
- **B6** Tenant-configurable shifts, late threshold, wait threshold (presets for now).
- **B7** Customer ETA notifications on board edits.
- **B8** Offline editing.
- **B9** HOS projection for future service days (34-hour restart, off-duty recovery); today the future-day check is `info hos_not_projected` (K3.2).
- **B10** Exception-replanning suggestions for board-published lanes; today the agent ignores board plans (K7.6).
- **B11** Strict cross-day order exclusivity (a shared per-order claim); today it is best effort with publish-time enforcement (K5.1, I15).

## Open-question resolutions used by this design

Q1 → R1.2 (Board default when active). Q2 → presets, E10. Q3 → B2. Q4 → past days read-only. Q5 → K3.4. Q6 → K9 hidden in shadow. Q7 → same roles. Q8 → 15 min client-side badge. Q9 → shelf (K8, R13.5). Q10 → reasons at publish review or inline. Q11 → own commands, session, 50 (K6). Q12 → driver change blocked on a lane with a started load (K8.1). Q13 → email local part as display name (K17.1). Q14 → `info driver_may_be_loading` (K8.1). Q15 → earliest windows kept on truncation (K5). Q16 → failed re-publish: rollback when possible, otherwise read-only forward recovery with a driver-impact banner; drivers briefly refused `in_transit` on a board run that is being updated (K8.4, K8.6). Research OQ1 → T1 spike decides Pragmatic vs React Aria; OQ2 → shared draft (D5); OQ3 → direct executor entry (K7.3); OQ4 → explicit `driver_id` on dispatch (K7.4); OQ5 → K3.4; OQ6 → K9; OQ7 → multi-load lanes after R25 (K12); OQ8 → D8.

## Responses to review findings (pass 1)

All 19 findings are addressed; none is backlogged or ignored. None changes the product concept or the scope the user decided.

| # | Sev | Response | Where |
|---|---|---|---|
| 1 | HIGH | Addressed. Exact plan, route and execution shapes, with the two different stop identifiers, ETA omission, windows kept off the route, a driver-work assertion test and a mapping parity test. | K7.3a, K2.4, P10, task 5, task 17 |
| 2 | MEDIUM | Addressed by simplification (freeze rule 6) rather than per-path patches: every change to a not-started published load is a new revision that retires the old plan, route and execution; amend in place is only for started loads, with stable pinned sequences, fresh numbers above the max, and a guarded execution rewrite (`PlanExecutionService` has no update path, verified). Cache invalidation through a NEW `get_work_service()` accessor runs before notifications. | K8.2, K8.4, K8.5, I16, P9, task 16 |
| 3 | MEDIUM | Addressed. HOS gate and figures apply to today only; future days get `info hos_not_projected`; projection is B9. | K3.2, K5, E19, B9 |
| 4 | MEDIUM | Addressed with the reviewer's code; unset → `disabled` → 404, store error → 503. | K4.2 step 2, K11.1, K13 |
| 5 | MEDIUM | Addressed. `Body(Any)` + `parse_body` with `include_input=False`, explicit 422, test asserting no echo. | External input validation |
| 6 | MEDIUM | Addressed. `lane.suggested_driver` from `driver_repository.search(assigned_truck_id=…)`, exactly one active unpaired match. | K5.2, tasks 11 and 27 |
| 7 | MEDIUM | Addressed with the proposed default (email local part from `auth_users`, tenant-keyed 5-minute cache, "Another dispatcher" fallback); recorded as Q13. | K17.1, requirements Q13 |
| 8 | MEDIUM | Addressed. Optional `call_type`, `product`, `window` on the snapshot, tray only, sent only after truncation. | K5, validation table, K11 |
| 9 | MEDIUM | Addressed and extended: `applied_commands` inside the draft transform makes duplicates commit at most once; the log uses `create_document`; refused attempts use a separate id form. This also closes the crash-between-commit-and-log gap. | K2.2, K4.2, K4.5, I13 |
| 10 | MEDIUM | Addressed. Tray exclusion and `block order_on_other_day` via one `terms order_ids` search; stated as best effort with strict publish-time enforcement (I15); strict claim is B11. | K5.1, E18 |
| 11 | MEDIUM | Addressed. List filter, `BOARD_OWNED_PLAN` 409 on approve/reject/replan, exception-replanning filter; B10 for board-lane exception suggestions. | K7.6, I14, task 5b |
| 12 | MEDIUM | Addressed. Preflight rebuilds snapshots and allocations from fresh orders; identity changes (`product_code`, `customer_id`, `customer_tank_id`) block, quantity changes are info. `customer_tank_id` is classed as identity (not quantity) because it changes the route stop and coordinates. | K3.2 `order_state`, K7.2, E20 |
| 13 | MEDIUM | Addressed. Context built first, then `engine.apply(draft, command, ctx)` with no I/O, then validation with the same context. | K4.2 steps 7–9 |
| 14 | MEDIUM | Addressed. R7.5 rewritten; `remove_lane` refused for any lane ever published; UI disabled state with reason. | requirements R7.5, K4.1, task 27 |
| 15 | MEDIUM | Addressed. `/api/ops/admin/feature-flags/{tenant_id}/dispatch-board[/{new_state}]`. | K11, task 12 |
| 16 | NIT | Addressed. Repo-root `docs/endpoint-registry.md`; plan check uses `../docs/endpoint-registry.md`. | file tables, plan Commands |
| 17 | NIT | Addressed. Per-route `_guards(strict)` lists the flag guard before the role guard. | K11.1 |
| 18 | NIT | Addressed. `claims["sub"]`, reject when missing. | K10.1 |
| 19 | NIT | Addressed. `TenantContext.settings` via a public `get_tenant_timezone` alias; `TenantSettings` has no `timezone` today, so `America/Chicago` for every tenant. | K2.5, D8 |

Unverified items from the review, now checked: the Postgres translator does match `term` inside a keyword array (`document_query.py` `_containment`), and `PlanExecutionService` has no execution update path, so the board uses a guarded `atomic_update` on `mvp_plan_executions` (K8.4). Pragmatic DnD on iPad Safari stays gated on the task 1 spike.

Convergence note: this is pass 1 with one HIGH, so the convergence rule doesn't apply yet. Freeze rule 6 (K8.5) was chosen pre-emptively because post-publish editing was the area where findings 1, 2 and 12 clustered.

## Responses to review findings (pass 2)

All 8 findings are addressed; none is backlogged or ignored. None changes the product concept or the scope the user decided. Post-publish produced MEDIUMs in both passes, so the reviewer's fixes for findings 1 and 2 are recorded as freeze rules 7 and 8 (K8.5), with named tests, rather than as more per-path patches.

| # | Sev | Response | Where |
|---|---|---|---|
| 1 | MEDIUM | Addressed as freeze rule 7. Publish groups are the connected components of "a dispatched order moved from X to Y". Any group that holds a dispatched order or a Modified lane runs whole in `BoardRedispatchService`, phase by phase across its lanes. The New load path relinks dispatched orders exactly like a new revision (one Create phase for both). `BoardPublishService` only publishes lanes with no dispatched order. Test (f) is the reviewer's A → B scenario; (g) covers a new load on a published lane; P11 covers partitioning. | K7.3, K8.2, K8.4, K8.5, I17, P11, E24, task 16 |
| 2 | MEDIUM | Addressed as freeze rule 8. `dispatch(..., notify: bool = True)`; redispatch always passes `False` and sends the assignment from the existing `PlanDispatchResult` fields in its notify phase, after invalidation. First-publish groups keep `notify=True` (they revoke nothing). I10 is reworded to say exactly this. Tests: task 3 (`notify=False` never calls the WS manager; default unchanged), task 16 (d) call-order spy and (e) zero notifications on an injected amend failure. | K7.3 step 5, K7.4, K8.4 phases 2 and 5, I10, task 3, task 16 |
| 3 | MEDIUM | Addressed with the reviewer's definition: started = an order `in_transit`/`delivered`/`failed`, or `completed_stops > 0`. Rack loading before `in_transit` counts as not started, and the review dialog shows `info driver_may_be_loading` for a past `planned_start`. That dialog had no defined data source, so the publish endpoint gains `dry_run: true`, which runs the existing preflight and classification and writes nothing. This reuses the preflight instead of adding an endpoint. | K8.1, K8.2, K7.1, K11, R13.3, R13.7, P8, E25, tasks 15, 16, 34 |
| 4 | MEDIUM | Addressed. `on_hold` is in the tray query, returned with `draggable: false`, `block_reason: "on_hold"`, counted in the cap; commands are still blocked by `order_state on_hold`. | K5, R3.6, task 11 |
| 5 | MEDIUM | Addressed. The store sorts by window then id (NULLS LAST, verified in `build_order_by`). Priority comes from the newest `mvp_delivery_priorities` doc (≤ 24 h old; the age bound is my addition, so a stale run can't order today's tray), joined by `order_id` in process; the page is sorted by score, then window, then id. Truncation keeps the earliest windows, and the banner says so. A failed priority read degrades to window order. | K5, K15, R3.3, E26, task 11 |
| 6 | NIT | Addressed. Rationale reworded ("declared by convention and for the field policy"). `DISPATCH_BOARD_INDEX_MAPPINGS` is registered in `persistence/document_field_policy.py` `_REGISTRIES`, not `bootstrap/fuel.py`. The `enabled: false` fields are tested through `unsearchable_fields`. Both new indices declare `created_at`/`updated_at` for the timestamp contract test. | Evidence, K2.4, K7.3a, file table, task 5 |
| 7 | NIT | Addressed. The test asserts one committed log doc and one commit; the second response equals the committed one or is `already_applied` with the same lane versions. E23 reworded. | K4.2, E23, test plan, task 11 |
| 8 | NIT | Addressed. Header says Q1–Q15 (Q14 and Q15 are new in revision 3 for the `driver_may_be_loading` and truncation defaults). K18 points to task 40. T-B10 is assigned to task 15 with a file name. R14.2 now says "Your change was not applied.", matching K14.6. `parse_body` maps a missing `client_command_id`/`client_request_id` to 422 `MISSING_IDEMPOTENCY_KEY` (explicit status, because the code defaults to 400); a malformed one stays `VALIDATION_ERROR`. | header, K18, test plan, R14.2, External input validation, tasks 13, 15 |

The reviewer's unverified item on SuperTokens `sub` stays covered by task 18's missing-`sub` test. The Pragmatic DnD iPad item stays gated on the task 1 spike.

## Responses to review findings (pass 3)

All 7 findings are addressed. None is backlogged or ignored, and none changes the product concept or the scope the user decided. Findings 1 and 2 are recorded as freeze rules 9 and 10 (K8.5), as the reviewer asked.

Finding 2's fix was taken further than the review text, for two reasons found while checking the code:
- Driver status transitions are unguarded full-document writes (`transition_endpoints.py` L265 without `guard_stored_state`). A relink could be silently overwritten by a driver write based on an earlier read, so a fresh-state Restore couldn't trust what it read.
- The proposed Restore retires r{n+1}. Once r{n+1} has been applied, tray orders on it are `scheduled`/`dispatched`, and retiring it would orphan them.

So rule 10 serializes instead of racing. It reuses the K5a guarded write and the existing gate stack so that a driver can't start an order on a non-dispatched board plan. It orders the phases so that every reversible write comes first. Recovery is then chosen only by the recorded phase: rollback before apply (the reviewer's Restore, applied to the whole group), forward from apply. Both are plain consequences of the phase order, not per-interleaving patches.

| # | Sev | Response | Where |
|---|---|---|---|
| 1 | MEDIUM | Addressed as freeze rule 9, with the reviewer's design. `record_checkin` persists through `atomic_update`. Its transform re-locates the stop by `station_id`+`sequence` and refuses on superseded, missing or completed, mapped to today's `ValueError` / 404 / 409. The amend transform also now recomputes from the stored document instead of refusing, so a concurrent check-in is kept, not fought. The file table, N7, I16 and the new I18 are updated. Tests: interleaving units, the Postgres race, and the unchanged existing suites. | Evidence, file table, K8.4 phases 1 and 4, K8.5 rule 9, K8.6, I16, I18, E22, tasks 4b, 16 (h) |
| 2 | MEDIUM | Addressed as freeze rule 10. Retired and staged documents carry `superseded_by_attempt`, `status_before_retire` and `created_by_attempt` (K2.4). The phases are reordered: retire, then stage and relink (reversible), then apply (irreversible). A failure before apply runs the reviewer's Restore across the whole group, in the worker or first thing on Retry after a crash; the Retry then classifies fresh and amends a started load. A failure from apply on completes forward from the `RedispatchAttempt` recorded at claim. Lanes in recovery are read-only. The board-plan gate and the guarded driver transition make rollback always possible. E14 is reworded. Tests (j)–(o) and P12 are added. | K2.1, K2.4, K7.2, K7.5, K8.4, K8.5 rule 10, K8.6, I19, I20, P12, E14, E27–E30, R13.10, Q16, tasks 4b, 16 |
| 3 | NIT | Addressed. `from` is the recorded published link (run from `publish.plans` of the lane whose `published_content` held the order, that lane's truck and driver). In rollback, `from` is the recorded `to` link. Freeze rule 2 is reworded. Test (o). | K8.4 phase 2, K8.5 rule 2, task 16 |
| 4 | NIT | Addressed. Both responses use `groups: [{truck_ids, kind, added_lanes}]`. | K7.1, task 22 |
| 5 | NIT | Addressed. The dry run runs on open and on lane-selection change, debounced 400 ms, with an `AbortController`. Reason completeness is computed client-side. | K7.1, task 34 |
| 6 | NIT | Addressed. `dry_run = isinstance(body, dict) and body.get("dry_run") is True`; a non-object body returns 422 `VALIDATION_ERROR`. | K7.1, task 13 |
| 7 | NIT | Addressed. `configure_work_endpoints` L78 and `_load_plan_snapshot` L309, both re-checked. | Evidence |

Unverified items carried forward:
- The SuperTokens `sub` question stays covered by task 18.
- Pragmatic DnD on iPad stays gated on the task 1 spike.
- The driver app's handling of the check-in `ValueError` is unchanged by rule 9, because the same exception is raised. Task 17 asserts the driver work read after a refused check-in.
- Checked: the driver app sends `in_transit` only through the offline queue (`driver-app/lib/work-api.ts` `queueOrderStatus` → `enqueueMutation`, except in demo preview), and the queue retries any 409 other than `INVALID_STATUS_TRANSITION` with the same idempotency key (`offline-queue.ts` L676–677). Not verified: that `check_idempotency` lets a same-key request through when no response was stored. Task 4b asserts it, because the endpoint deliberately doesn't store the 409.
