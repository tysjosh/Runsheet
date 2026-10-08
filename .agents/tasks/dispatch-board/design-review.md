# Dispatch Board: design review (pass 4)

Reviewed `requirements.md` (revision 4), `design.md` (revision 4), `plan.md` and `research.md` in this folder against the main checkout at `ad72dde` on `production-readiness/go-live-blockers`. This was a documents-only review: no code changes, builds or test runs.

Verdict: **APPROVED** under the convergence freeze (0 HIGH, 0 MEDIUM, 3 NIT, plus one freeze directive).

All 7 pass-3 findings are addressed (see "Pass-3 fixes checked"). Revision 4 serializes driver writes with board writes (check-in compare-and-set, guarded driver transitions, a board-plan gate) and splits recovery into rollback before apply and forward completion after it. I traced the rollback argument against the real code, and it holds.

## Convergence decision

Passes 2, 3 and 4 had no HIGH findings. Each one still found new MEDIUM-level problems in post-publish editing (K8). In this pass that's five problems, listed under "Freeze directive". The rule applies now, as pass 3 said it would if pass 4 found another post-publish MEDIUM. So:
- These five aren't raised as findings.
- The draft step writes them into `design.md` K8.5 as **freeze rule 11**, with the tests listed below.
- Post-publish design is then closed. Anything else found in this area is handled during implementation, by refusing the change with a reason and adding a test, not by another design pass.

No MEDIUM was found outside post-publish, so nothing else blocks.

## Freeze directive (write into design.md K8.5 as freeze rule 11)

**Freeze rule 11: "Nothing in post-publish depends on a skipped guard, an unlocked write, or in-memory state."** Each item below reuses an existing mechanism or narrows a rule. None of them adds a new path. Add each item's tests to the task shown.

**(a) The board-plan gate can't be skipped by wiring.**

Where: K8.6 "Board-plan gate", task 4b (c).

Problem: `DriverTransitionGateStack.__init__` (`order_transition_service.py` L234–249) takes no store or reader. Its convention is "Absent → the gate is skipped" (docstring L206–231). `configure_*` (L816) rebuilds the stack from its arguments on every call. If someone follows that convention for gate 0, the gate silently does nothing whenever the reader isn't passed. Then the rollback proof in K8.4 ("Why rollback is always possible") fails without any sign.

Rule:
- Add a `board_plan_reader` keyword (the document store's `get_document`) to `DriverTransitionGateStack` and to the configure function, and pass it at the existing bootstrap call site.
- When the reader is `None` and `assigned_run_id` starts with `bp-`, gate 0 fails **closed**: 409 `BOARD_ROUTE_UPDATING` and a WARNING log, never a skip.

Tests (task 4b):
- A stack built without a reader refuses `in_transit` on a `bp-` run and allows it on an agent run.
- A bootstrap wiring test asserts the configured stack has a reader.

**(b) Plan writes on the check-in path use compare-and-set.**

Where: K8.4 phase 4 step 3, K8.6, freeze rule 9.

Problem: `update_document` (`document_store.py` L292–317) does `session.get` → merge → write with no row lock. The check-in endpoint marks the plan `completed` this way (`mvp_endpoints.py` ~L1162) and then calls `compute_actual_cost`, which writes the plan the same way (`plan_execution_service.py` L650; `compute_estimated_cost` L555 too). If the driver's last check-in lands while amend step 3 runs, the unlocked write can revert the board's `revision + 1` and the dropped assignments. Rule 9 covers executions only.

Rule:
- Those three plan writes become `atomic_update` transforms that merge only their own fields into the stored document. The fields and the responses stay the same.
- Add both files to N7's exception list.

Test (task 4b): an interleaving fake store where the endpoint reads, then the board's revisioned plan write commits, then the completion commits. The revision and assignments survive, and the status is `completed`.

**(c) An amend that leaves no pending stop finishes the load.**

Where: K8.4 phase 4.

Problem: a started load can have its last unpinned pending stop removed (K8.1 allows it). The amend transform then leaves the execution at `completed_stops == total_stops` but still `in_progress`. Only a check-in completes a plan (`mvp_endpoints.py` ~L1159), so the plan stays `dispatched` forever and outcomes and actual cost are never computed.

Rule (reuse the endpoint's completion):
- When the amend transform's result has `completed_stops >= total_stops`, it sets `status: "completed"`.
- Step 3 then sets the plan `completed`, guarded on `dispatched` and the revision, and calls `compute_actual_cost`, exactly as `driver_checkin` does after its last check-in.

Test (task 16): remove the only pending stop from a started load. The execution and plan are `completed`, an outcome doc exists, and the driver's work read shows the load finished.

**(d) The notify payload is rebuilt from documents, not from memory.**

Where: K8.4 phase 3 step 2 ("kept in `last_result`") and phase 6. K2.1 says `last_result` is written at finalize.

Problem: after a crash between dispatch and notify, a resumed attempt may not have the `PlanDispatchResult`s. Nothing persists them per load before finalize.

Rule: phase 6 builds each `send_assignment` payload from the stored documents, for every plan where `created_by_attempt == attempt_id` and `status == "dispatched"`:
- `plan_id` and `run_id` come from the plan.
- `truck_id`, `route_ids` and `order_ids` come from its routes' `stops[].order_ids`.
- `driver_id` is the lane driver from `attempt.loads`.

This is the same payload `dispatch()` builds (L384–402). Drop "kept in `last_result`" from phase 3.

Test (task 16 (k)): a crash after dispatch and before notify. The resume sends each assignment once, with a payload equal to the uninterrupted run.

**(e) A pinned order that's sitting on the wrong lane can always go home.**

Where: K8.1 engine rules, K3.2 `order_state`, E14.

Problem: in E14 the moved order became `in_transit` on lane A's run. After rollback, the draft still holds it on lane B. On B it's `block order_committed_elsewhere`, so B can't publish. It's also `stop_pinned`, so the dispatcher can't move it. Neither lane can publish, and the board offers no way out. E14's "Retry … amends" doesn't happen.

Rule (narrow `stop_pinned`): `stop_pinned` blocks moves away from the load that the order's `assigned_run_id` names. A move onto that load is always allowed. On any other lane, the check's message is "Started on {truck}. Move it back." and its fix action is that move. This is just the inverse of the pin, with no new path.

Tests (task 9 and task 16 (j)):
- After rollback, B shows the block with the move-back action.
- The move back commits.
- Publishing A then amends.

## Findings

### 1. NIT: `RedispatchAttempt.phase` has no value for phase 5

Where: K2.1 `phase` Literal, K8.4 ("records the phase it starts"), the forward-completion list.

Fix: state that phase 5 (Invalidate) records `phase = "notify"`, because invalidate and notify are re-run together on resume. Alternatively, add `"invalidate"` to the Literal and to the forward-completion list.

### 2. NIT: Edge cases are out of order

Where: Edge cases. E27–E30 sit between E22 and E23.

Fix: renumber, or move E27–E30 after E26.

### 3. NIT: The unverified idempotency item is now verified

Where: the "Unverified items carried forward" bullet on `check_idempotency`, at the end of the design.

Fix: `check_idempotency` (`driver/middleware/idempotency.py` L191–239) only looks up a stored response and takes no in-flight reservation. A same-key request with nothing stored therefore runs again. Mark it verified. Keep the task 4b assertion as a regression test.

## Scope coverage (user-required list)

| Item | Where | Status |
|---|---|---|
| Undo/redo | R11, K6, P2 | Covered |
| Draft vs published | D4, R10, K2.1 | Covered |
| Concurrency/presence | D5, R14, K4.2, K4.5, K10 | Covered |
| Live telematics / order-intake updates | R15, K10.4–K10.5 | Covered. Compliance push deferred as B4 with a TTL fallback (justified) |
| Map linkage | R17, K14.10 | Covered. Map drop deferred as B1 (justified) |
| Filters/search | R4, R3.8, K5 | Covered |
| Keyboard shortcuts + non-drag alternative (SC 2.5.7) | R18, K14.4–K14.5 | Covered |
| Screen reader announcements | R19, K14.6 | Covered |
| Tablet | R20, K14.8, task 1 spike | Covered |
| Empty/loading/error states | R21, K14.9 | Covered |
| Performance targets | N1, K15, task 39 | Covered |
| Audit trail | R22, K2.2, K17 | Covered |
| Permissions/roles | R23, K11.1 | Covered |
| Feature-flag rollout | R1, K13, task 41 | Covered |
| Telemetry | R24, K16 | Covered. Client telemetry deferred as B5 (justified) |
| Post-publish changes | R13, K8 | Covered. Frozen by rules 1–11 |

Dispatcher UX simplicity is unchanged. The shape stays fixed (tenant → day → lane → load → stop), no dialog opens on a drop, every drag has a menu or Place-mode equivalent, and there's no node graph. The new recovery UI follows Q16: one lane state, one Retry that always covers the whole group, and one banner. That keeps recovery from turning into a dispatcher workflow. Freeze item (e) adds one fix action on an existing check, not a new control.

Research citations: `research.md` defines [S1]–[S40] with 40 URLs, and each one is cited inline at least once. No finding.

Traceability: every plan task cites requirement IDs. The coverage table maps every requirement, including R13.10 and R13.11 (tasks 4b, 16, 17, 34). Pass-3 findings trace to tasks 4b, 5, 7, 11, 15, 16, 17 and 34. Freeze rule 11's tests go into tasks 4b, 9 and 16 as listed above.

## Pass-3 fixes checked

- F1 (check-in compare-and-set): K8.6, freeze rule 9, task 4b (a). It matches `record_checkin` (L142–313): the pre-read checks are kept, the edit at L283–313 is replaced, and the errors map to today's `ValueError`, `resource_not_found` and `stop_already_completed`. **Addressed.**
- F2 (retry divergence): phases reordered so every reversible write comes first, attempt markers, rollback versus forward chosen by the recorded phase, the board-plan gate and guarded transitions, and tests (j)–(o) and P12. I traced the proof against the code:
  - The guarded path compares against the endpoint's own earlier read (`order_service.py` L268–272 snapshot, `_guarded_upsert` L662–707 compares status and `last_event_timestamp`).
  - The relink bumps `last_event_timestamp`.
  - The gate-read, retire, write interleaving leaves the order at its `from` link.

  **Addressed**, apart from the wiring gap in freeze item (a).
- F3 (`from` = recorded published link): K8.4 phase 2, freeze rule 2, test (o). **Addressed.**
- F4 (one `groups` shape), F5 (dry-run debounce), F6 (`dry_run` on a non-dict body), F7 (line numbers): **Addressed.** `configure_work_endpoints` L78 and `_load_plan_snapshot` L309 were confirmed in pass 3.

## Verified assumptions (this pass)

- `transition_endpoints.py`:
  - `transition_order_status` reads the order (`resolve_order`), runs `evaluate`, then calls `apply_status_transition` at L265 without `guard_stored_state`.
  - `_store` runs only on success, so an exception is never stored.
- Guarded transitions:
  - `apply_status_transition(guard_stored_state=True)` snapshots `_GUARD_SNAPSHOT_KEYS` from the passed order before any change.
  - It writes through `upsert_with_last_event_timestamp` (L574) with `expected_status` and `expected_last_event_timestamp`, and re-raises `OrderChangedConcurrentlyError` (L95) or `OrderWriteDiscardedError` (L116).
- `_guarded_upsert` is an `atomic_update` transform that refuses on a changed status, a changed `last_event_timestamp`, or a stale incoming timestamp.
- `check_idempotency` and `IdempotencyMiddleware.check_and_cache` only look up a stored response and take no reservation (`driver/middleware/idempotency.py`).
- Driver app:
  - `offline-queue.ts` retries a 409 other than `INVALID_STATUS_TRANSITION` (disposition matrix around L670–680).
  - `work-api.ts` `queueOrderStatus` goes through `enqueueMutation` (L41–57).
  - `websocket.ts` treats `assignment`, `assignment_revoked` and `new_route` as refetch events (L794–805).
- `DriverTransitionGateStack`:
  - `evaluate` returns early for any status outside `GATED_TARGET_STATUSES = {"in_transit"}` (L106, L282).
  - The four gates don't read plan status.
  - The constructor has no store dependency (L234–249). This is the basis of freeze item (a).
  - The single production construction site is `order_transition_service.py` L816.
- `record_checkin` requires plan `dispatched`, matches on `station_id` and `sequence`, and persists with `update_document` at L312–313. The only execution writers are L124–125 (create) and L312–313.
- `update_document` is a non-locking get → merge → write (`document_store.py` L292–317). This is the basis of freeze item (b).
- The check-in endpoint sets the plan `completed` with `update_document` (~L1162) and calls `compute_actual_cost` (~L1175). That method writes the plan with `update_document` (L650), and `compute_estimated_cost` does the same (L555).
- `plan_dispatch_service.py`: `_ACTIVE_STATUSES = {"dispatched", "in_transit"}` (L75), `order_not_dispatchable` (L582). `loading_plan_executor.py`: `_APPLIED_STATUSES` (L160), `_LINKED_SETTLED_STATUSES` (L162).
- These were verified in pass 3 and are unchanged at `ad72dde`:
  - the `atomic_update` contract;
  - `relink_dispatched_assignment` is correctly marked NEW;
  - `DriverWorkService` accessors and the K7.3a reader keys;
  - the MVP endpoint lines;
  - the flag admin router;
  - the frontend components and hooks listed in pass 3.

## Unverified or wrong assumptions

- Wrong as written: gate 0 is safe as specified. Under the stack's "absent → skip" convention it can be silently disabled (freeze item (a)).
- Wrong: freeze rule 9 makes the post-publish writes safe. The plan document still has unlocked writers on the check-in path (freeze item (b)).
- Wrong: phase 3 results survive a crash for phase 6 (freeze item (d)).
- Wrong: after an E14 rollback, the Retry amends. The draft is deadlocked by `stop_pinned` plus `order_committed_elsewhere` (freeze item (e)).
- Unverified: SuperTokens puts the user id in `sub` in this SDK version. Task 18 covers it.
- Unverified: Pragmatic DnD long-press on iPad Safari. It stays gated on the task 1 spike.
