# Dispatch Board: staging rollout runbook (plan task 41)

Status: written, not run. Task 41 was deferred to the owner. No staging or AWS action has been taken for it. Follow the steps in order and record every result in `evidence.md` under "Phase 8: Staging rollout".

Scope: AWS account 224535575204, us-east-2, `runsheet-staging-*` resources, tenant `demo-tenant`. Requirements covered: R1.2–R1.4, R12.7, R12.9, R12.10, R13.4, R13.6, N1, N6.

## Ground rules

- Fixtures use the `QA-` prefix. Never read-modify, move or delete anything named `QA-SWEEP-*`; another agent owns those and they will show up on the board and in plan lists.
- Don't touch `overlay.order_intake_pipeline`. The other agent toggles it.
- Restore flags to the baseline recorded in step 1, not to the last value you read.
- Never use `POST /api/data/cleanup` (it wipes every tenant).

## 0. Preconditions

1. CI is green on the exact commit to deploy, including the `endpoint-registry` and `dispatch-board-e2e` jobs.
2. Deploy from a worktree pinned to that commit: `git worktree add .worktrees/board-deploy <sha>`.
3. If the CodeBuild branch (`production-readiness/codebuild-builds`) has been committed and landed on the base branch, merge it before deploying. Otherwise use the local-build path (`BUILD_MODE=local`) and check that at least about 8 GB of disk is free before the Docker build.
4. Pass `DOMAIN=<staging domain>` to `deploy` and `deploy-ui` (the worktree has no `.env.staging`).
5. Pick a QA service date that has no `QA-SWEEP-*` lanes. The draft is per tenant per day, so open the snapshot for the candidate day first and check that it has no QA-SWEEP lanes. A date about 10 days out usually works. Record it as `$QA_DAY`.

## 1. Record flag baselines

Flags live in Redis as `overlay_ff:<flag_key>:demo-tenant` with a 90-day TTL. "Unset" and "set to `disabled`" behave the same for the board, but they aren't the same baseline. Record both whether the key exists and its TTL for:

- `dispatch_board`
- `overlay.compartment_loading`
- `overlay.route_planning`

Read them with a one-shot ECS task on the backend task definition (same pattern as `cmd_migrate` in `scripts/staging_aws.sh`), with the command overridden to:

```sh
python -c "import os,redis;r=redis.from_url(os.environ['REDIS_URL']);t='demo-tenant';[print(k, r.get(f'overlay_ff:{k}:{t}'), r.ttl(f'overlay_ff:{k}:{t}')) for k in ('dispatch_board','overlay.compartment_loading','overlay.route_planning')]"
```

`GET /api/ops/admin/feature-flags/demo-tenant/dispatch-board` (admin session) is a cross-check, but it reports an unset key as `disabled`.

Record a table of flag, raw value (or `unset`), TTL and time read.

## 2. Deploy, backend first, then the UI

1. `./scripts/staging_aws.sh migrate`. The board stores its data in the document store, so this should be a no-op. Record the output.
2. `./scripts/staging_aws.sh deploy`, then `./scripts/staging_aws.sh verify`. Record the image tag (short SHA).
3. UI ancestor check: read the image tag of the live UI task definition. Then run `git merge-base --is-ancestor <live-ui-sha> <deploy-sha>`. If it isn't an ancestor, stop and don't deploy the UI, because the owner may have deployed newer work.
4. `./scripts/staging_aws.sh deploy-ui`. Record the tag.
5. With `dispatch_board` still at baseline (`disabled`/unset), check that the Dispatch page shows no Board tab and `GET /api/fuel/board/status` returns 404 `DISPATCH_BOARD_DISABLED` (R1.3).

Rollback: re-register the previous task definition revision for the service (backend or UI) and roll the service. Flags need no change, because `disabled` makes the board inert.

## 3. Fixtures (all `QA-`)

Create the following in `demo-tenant`, through the normal app APIs:

- Two trucks: `QA-BOARD-TRUCK-A` and `QA-BOARD-TRUCK-B`, each with compartments.
- Three customer tanks with coordinates: `QA-BOARD-TANK-1..3`.
- Three or four confirmed fuel orders with names or ids prefixed `QA-BOARD-`, with delivery windows on `$QA_DAY`.
- Two QA driver accounts (`QA-BOARD-DRIVER-A/B`), able to sign in to the driver app, with current qualifications.

Record every id. These ids are the cleanup list in step 9.

## 4. Shadow (R1.4)

1. `POST /api/ops/admin/feature-flags/demo-tenant/dispatch-board/shadow`.
2. Within one status poll (or on page focus), the Board tab appears and shows "Preview mode, changes are not saved". Drag sources are disabled.
3. Open `$QA_DAY`. The QA orders appear in the tray and the QA trucks appear as lanes.
4. `POST /api/fuel/board/$QA_DAY/validate` for a QA order against both QA lanes returns checks, so validation works in shadow.
5. `POST /api/fuel/board/$QA_DAY/commands` returns 409 `DISPATCH_BOARD_READ_ONLY`, and so does `POST /api/fuel/board/$QA_DAY/publish`.
6. Optionally compare the board's validation for a QA order with the compartment-loading agent's outcome. Do this only if `overlay.compartment_loading` is already active at baseline. Don't raise overlay flags just for this, because that changes agent behaviour for the whole tenant.

## 5. Active (R1.2, R12.7, R12.9)

1. `POST /api/ops/admin/feature-flags/demo-tenant/dispatch-board/active_gated`. The Board becomes the default Dispatch tab and the preview banner goes away.
2. On `$QA_DAY`, pair `QA-BOARD-DRIVER-A` with lane A and assign two QA orders to it. All checks must be pass or warn. Publish lane A through the review dialog.
3. Poll `GET /api/fuel/board/$QA_DAY/publish/<publish_id>` until lane A is Published.
4. Sign in to the driver app as `QA-BOARD-DRIVER-A`. The assignment arrives, and the load shows its compartment manifest.
5. `GET /api/driver/work` as that driver returns, for every published stop, non-null `lat`/`lon`, `planned_arrival` and `planned_gallons_by_grade`, plus `manifest_available: true`. Save the response, with ids only, in evidence.

## 6. Board plans stay off Fuel Distribution (R12.10)

1. The Fuel Distribution tab's plan list doesn't show the lane A plan.
2. `GET /api/fuel/mvp/plans?source=dispatch_board` does list it.
3. `POST /api/fuel/mvp/plan/<board plan id>/approve` returns 409 `BOARD_OWNED_PLAN`, and nothing changes. Optionally repeat with `reject` and `replan`.

## 7. Move a published stop (R13.4, R13.6)

1. Pair `QA-BOARD-DRIVER-B` with lane B. Move one published, not-started stop from lane A to lane B. Lane A shows Modified.
2. The re-publish dialog lists both drivers and what each will be told. Re-publish.
3. Driver A loses the stop: the app receives the revocation (`assignment_revoked`), and `GET /api/driver/work` for A no longer returns that order.
4. Driver B gets the new assignment, and their work read returns the stop with coordinates, planned arrival and gallons.
5. Optional (R13.6): change lane B's driver pairing to driver A and re-publish. Driver B is revoked and driver A is assigned.

## 8. Latency (N1)

`record_metric` logs `board.*.ms` at DEBUG, so staging logs may not have them. Use client-side timings instead: `curl -w '%{time_total}'` with an admin session, 30 samples each.

| Call | Budget (p95, server) |
|---|---|
| `GET /api/fuel/board/$QA_DAY` (snapshot) | 2.0 s |
| `POST …/validate`, batch (one order across all lanes) | 500 ms |
| `POST …/validate`, single position | 300 ms |
| `POST …/commands` (`move_stops`, followed by the reverse move to undo it) | 600 ms |

Record median, p95 and max for each. Client timings include the network, so a miss of less than the round-trip time is not a server miss. If staging has the DEBUG metric lines, record those as well. This step is also the first Postgres run of the tray query's `must_not terms` clause (Phase 7 open item), so the snapshot call must succeed.

Live updates: with the board open in two browsers, a move in one shows in the other within 2 s. Spot-check this 5 times.

## 9. Cleanup (dry run first)

1. On the board, unassign every QA order on `$QA_DAY` and remove both QA lanes. This retires the published QA plans through the normal path, so the drivers are told and the work caches are cleared.
2. Dry run: list, with ids only, every document in `demo-tenant` that references the step 3 ids:
   - fuel orders and their order events
   - `mvp_load_plans`, `mvp_routes` and plan executions with `source: dispatch_board` that reference QA orders or trucks
   - `dispatch_board_drafts` for `$QA_DAY` (delete only if the draft has no non-QA lanes; otherwise leave it as the board left it)
   - `dispatch_board_commands` for that draft
   - the QA trucks, compartments, tanks and driver records

   Confirm that nothing in the list is `QA-SWEEP-*` or lacks the `QA-BOARD-` provenance.
3. Delete exactly that list, then rerun the dry-run query. It must return nothing.
4. Delete the QA driver login accounts from SuperTokens, unless they are being kept for later QA. If they are kept, note them.

## 10. Restore flags

For each flag in the step 1 table:

- If the baseline was unset, delete the key (`r.delete('overlay_ff:<key>:demo-tenant')`). Setting it to `disabled` doesn't restore an unset baseline.
- If the baseline was a value, set that value. Setting it resets the TTL to 90 days. Note the TTL change, or set it back with `r.set(key, value, ex=<recorded ttl>)`.

Then read all three again with the step 1 command and record the result next to the baseline. The Board tab hides within one status poll if the baseline was `disabled` or unset.

## Evidence to record

Commit, image tags, baseline table, the result of each check (pass/fail with ids), the latency table, the cleanup dry-run list and the post-delete count, and the restored-flag table.
