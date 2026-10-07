# Implementation Plan: Customer portal v1 (OI-06)

Source of truth: `design.md` (revision 3, design review pass 4 APPROVED) and `requirements.md`. This plan sequences the design; it doesn't re-decide it. FREEZE decisions F1–F8 are closed. The payment area (F2, F4, F7, F8) is frozen: accept or backlog non-HIGH edge cases there.

The work is split into six features, executed in order. Their machine-readable form is in `task-customer-portal/` (`task.json`, `context.json`, `features/FEAT-00N.json`). Each FEAT JSON holds the detailed steps; the list below is the dependency order and the verification for each item.

## Conventions for every item

- Worktree: `/Users/olukotunjosh/Downloads/Runsheet/.worktrees/customer-portal`. Use absolute paths and `git -C <worktree>`. Stage files by name. Commit locally. Never push (the release step pushes).
- Python: `/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend/venv/bin/python` (written `<venv>/bin/python` below), with cwd `<worktree>/Runsheet-backend` and env `ENVIRONMENT=test JWT_SECRET=ci-test-jwt-secret JWT_ALGORITHM=HS256 REDIS_URL=redis://localhost:6379`. That venv has no `stripe` package, so tests fake it through `StripeConnector._get_stripe_module` / `sys.modules`.
- The `[pg]` tests use a scratch database. Never use the dev DB. Run `docker exec runsheet-postgres psql -U runsheet -c 'CREATE DATABASE runsheet_portal_ci'`, then `alembic upgrade head` with `DATABASE_URL=postgresql+psycopg://runsheet:runsheet@localhost:5432/runsheet_portal_ci`, then run `POSTGRES_TEST_URL=<same> <venv>/bin/python -m pytest tests/postgres -q --no-cov`. The run must show 0 skipped. Drop the scratch DB afterwards.
- New backend tests go in a new package, `Runsheet-backend/tests/portal/`, with a `conftest.py` real-auth harness. The harness stubs `services.elasticsearch_service` the way `tests/smoke/test_route_smoke.py` does, forces `auth_provider="supertokens"`, and uses `configure_session_verifier` / `configure_ws_session_verifier`. It never uses `override_auth` for customer sessions. The Postgres-only tests go in `tests/postgres/test_portal_schema.py`.
- Any item that adds a route regenerates `docs/endpoint-registry.md` (`<venv>/bin/python scripts/generate_endpoint_registry.py`) and commits it.
- `main.py` must stay at or under 200 effective lines (CI git-hygiene; it is 169 today).
- Scratch files go only in `/Users/olukotunjosh/Downloads/Runsheet/tmp/`. Don't touch the `order_intake_pipeline` flag or any third-party credential.

## FEAT-001: Foundation, identity, central deny, guard, audit, `/me`

- [ ] 1. Rebase onto the current base, then add settings and error codes.
      The branch is 25 commits behind `origin/production-readiness/go-live-blockers`. Those commits include OI-41 `transition_order_guarded`, OI-02 dyed-diesel fail-closed, OI-14 invoice pricing, and `errors/codes.py` additions. Fetch, confirm the branch isn't on origin, then rebase. If it is on origin, merge instead.
      Then add the 8 portal settings plus the WARN validator (§8.2), the 15 error codes and factories (§11), `CUSTOMER_PORTAL_ENABLED=false` in `.env.example`, and `CUSTOMER_PORTAL_ENABLED=true` in the `scripts/staging_aws.sh` task env next to `COMMERCE_BACKBONE_ENABLED`.
      Files: `Runsheet-backend/config/settings.py`, `errors/codes.py`, `errors/exceptions.py`, `.env.example`, `scripts/staging_aws.sh`
      Verify: `<venv>/bin/python -m pytest tests/unit -k "settings or error or codes" -q --no-cov` passes.

- [ ] 2. Migration `0011_customer_portal` and the ORM mirrors.
      Single revision on top of `0010_acct_override_audit` (§9). It adds `auth_users.customer_id`, the CHECK `ck_auth_users_customer_binding`, the partial index, `portal_user_grants` (needs `CREATE EXTENSION IF NOT EXISTS citext`), and `portal_payment_attempts` with `uq_ppa_idem`, `uq_ppa_inflight` and `ix_ppa_customer`. `downgrade()` reverses all of it. Add `PortalUserGrantORM` and `PortalPaymentAttemptORM`. If another `0011_*` has landed (margin-feed), re-parent and renumber. Never create a merge revision.
      Files: `Runsheet-backend/alembic/versions/20261008_0001_customer_portal.py`, `persistence/models.py`
      Verify: on the scratch DB, `alembic upgrade head` succeeds, `python -m scripts.check_migrations` passes, `alembic heads | grep -c '(head)'` prints `1`, and `alembic downgrade -1 && alembic upgrade head` succeeds.

- [ ] 3. The `customer` role, the `customer_id` claim, the provisioner guard, and the driver-grant refusal (§1.1, §1.2, §1.5, §1.6).
      Files: `auth/supertokens_init.py`, `ops/middleware/tenant_guard.py` (`TenantContext.customer_id`, `_build_context`, `_context_from_session_claims`, `request.state.auth_user_id`), `auth/test_auth.py`, `auth/provisioner.py`, `fuel/api/driver_endpoints.py`. Update tests that pin `CANONICAL_ROLES`, and only to add `customer`.
      Verify: the new `tests/unit/test_supertokens_claims.py` (PRV-4, `customer ∉ CUSTOMER_ASSIGNABLE_ROLES`), a provisioner guard test, `tests/portal/test_app_access_refuses_customer.py` (PRV-3), `tests/unit/test_driver_app_access_endpoints.py` and `tests/unit/test_tenant_scope_authz.py` all pass. Then `tests/postgres/test_portal_schema.py::test_customer_binding_check` (PRV-2) passes on the scratch DB.

- [ ] 4. Central default-deny (§2.2): E1 and E2 in the middleware, E1b in `get_tenant_context`, E3 on WebSockets.
      Files: new `portal/__init__.py` and `portal/scope.py`. Modified: `middleware/auth_enforcement.py`, `ops/middleware/tenant_guard.py`, `bootstrap/websockets.py`.
      Verify: `tests/portal/test_portal_central_deny.py` (XR-1 full route inventory with a handler spy, XR-2, XR-3, XR-5) and `tests/portal/test_portal_ws_deny.py` (XR-4, plus the staff regression) pass. Existing `tests/unit/test_ws_supertokens_auth.py`, `tests/integration/test_supertokens_auth_flow.py` and `tests/smoke` pass unchanged.

- [ ] 5. Portal guard, principal checker, audit, rate-limit helpers, CORS header, and `GET /api/portal/me` (§2.3, §3.1, §8).
      Add `PortalScope`, `require_portal_customer`, `reject_scope_params`, `portal_rate_key` and the limit strings. Add `PortalPrincipalChecker`, wired in `bootstrap/core.py` when `CustomerService` exists. Add `emit_portal_audit` and `PortalAuditMiddleware`, registered in `main.py` right before `_register_auth_enforcement`. Add `"Idempotency-Key"` to the CORS `allow_headers`.
      Add a public `OrderIntakePipeline.get_ordering_state`. For `payments_available`, add only the `configure_portal_payments` / `portal_connector` registry; it returns `None` until FEAT-005.
      Mount the portal routers unconditionally. If `main.py` would pass 200 lines, use one router-tuple import.
      Files: `portal/api/{__init__,_authz,me_endpoints}.py`, `portal/services/{__init__,principal,portal_payment_service}.py`, `portal/audit.py`, `portal/models.py`, `fuel/services/order_intake_pipeline.py`, `bootstrap/core.py`, `main.py`
      Verify: `<venv>/bin/python -m pytest tests/portal -q --no-cov` passes. That includes `test_portal_staff_deny.py` (XR-6), `test_portal_flag_off.py` (XR-8, both parametrizations), `test_portal_audit.py` (AUD-1), `test_portal_rate_limits.py` (RL-1, T-RL-COVERAGE), `test_portal_cors.py` (PAY-13), `test_portal_principal.py` (PRV-5) and `test_portal_scope.py` (ISO-C-4, ISO-T-2 query part). Each of these iterates every `/api/portal/*` route dynamically, so later FEATs are covered automatically. The `main.py` line count is ≤ 200. Regenerate the registry, commit, and confirm `git diff --exit-code docs/endpoint-registry.md` is clean.

## FEAT-002: Portal-user provisioning (R1)

- [ ] 6. `PortalAccessService`, the admin routes, and the archive hook (§1.4, §1.7, F1, D10).
      The service handles invite, list, resend and revoke under `pg_advisory_xact_lock('portal-grants:…')`. Revoke deletes the SuperTokens user and the portal-only `auth_users` row. The admin routes live under `/api/commerce/customers/{customer_id}/portal-users`, behind `require_portal_admin`. The archive hook in `commerce/api/customer_endpoints.py` calls `revoke_sessions_for_customer`. Wire it all in `bootstrap/core.py`.
      Files: `portal/services/portal_access_service.py`, `portal/api/admin_endpoints.py`, `commerce/api/customer_endpoints.py`, `bootstrap/core.py`, `main.py` / the portal router tuple
      Verify: `tests/portal/test_portal_access_service.py` (PRV-1, ISO-T-5), `test_portal_admin_routes.py` (XR-7), `test_portal_isolation.py::test_revoked_customer_has_no_session` (XR-9) and the archive-hook test pass. `tests/commerce` and `tests/unit/test_driver_app_access_endpoints.py` still pass. On the scratch DB, `test_concurrent_invites_cap` (11 concurrent invites leave exactly 10) and the ISO-T-4 grants-list case pass. Regenerate and commit the registry.

## FEAT-003: Order requests and tanks (R4, R7)

- [ ] 7. Repository and order-action groundwork (§2.4, §4.4, F3).
      Add `PORTAL_REVIEW_HOLD_REASON`. Add `FuelOrderRepository.search` filters `customer_tank_id`, `statuses` and `hold_reason` on both the ES and Postgres paths. Add `transition_if`. Extract `fuel/services/order_actions.cancel_order`. The staff cancel endpoint calls it with unchanged responses. The final write in staff `release-hold` becomes a `transition_if` (409 `INVALID_STATUS_TRANSITION` on a lost race). Staff `GET /api/orders` gains a `hold_reason` param. Re-read the post-rebase `order_endpoints.py` first, and leave the OI-41 `transition_order_guarded` status route alone.
      Files: `fuel/order_models.py`, `fuel/order_repository.py`, `fuel/services/order_actions.py`, `fuel/api/order_endpoints.py`
      Verify: `tests/portal/test_order_release_hold_portal.py` case (d) (the CAS 409) passes. Existing order endpoint and repository tests pass unchanged: `<venv>/bin/python -m pytest tests/unit -k "order" -q --no-cov`.

- [ ] 8. The `web_portal` intake path (§4.2, D5, D9).
      Add `WebPortalIntakeAdapter`, registered in `bootstrap/fuel.py`. Add `_PortalChannel`, `portal_event_id`, `portal_order_id` and `ingest_portal`. Add the `web_portal` order id in `_complete_order_doc`, the existing-id guard (returns `duplicate`, with no hooks and no write), and the step i3 hold stamp, which runs only when status is `placed`.
      Files: `fuel/intake/web_portal_adapter.py`, `fuel/services/order_intake_pipeline.py`, `bootstrap/fuel.py`
      Verify: pipeline unit cases in `tests/portal/test_portal_orders.py` (ORD-1b pipeline part), `test_order_release_hold_portal.py` cases (a), (a2), (b) and (c), and `test_portal_loadable_exclusion.py` (ORD-3, against the route-planning, prioritization and compartment-loading candidate queries) all pass. `tests/integration/test_order_intake_e2e.py` and `tests/integration/test_dyed_diesel_compliance.py` pass unchanged.

- [ ] 9. Scoped readers, projections, `PortalOrderService`, and the order and tank endpoints (§2.4, §4.1, §4.5, §7, F6, D4).
      Add `PortalOrderReader`, `PortalTankReader` and `PortalForecastReader`, plus `ORDER_STATUS_MAP` and the order and tank projection models. `submit` handles intake-disabled, replay, and the pipeline result mapping, including `except (AppException, PricingError, DyedDieselOrderRejected)`. Add cancel. Add `GET/POST /api/portal/orders…` and `GET /api/portal/tanks…`, with their limits and `reject_scope_params`.
      Files: `portal/services/{scoped_readers,projection,portal_order_service}.py`, `portal/models.py`, `portal/api/{order_endpoints,tank_endpoints}.py`, bootstrap wiring, `main.py` / the router tuple
      Verify: these pass:
      - `tests/portal/test_portal_orders.py`: OID-1 (including replay while disabled), OID-2, OID-3, ORD-1, ORD-1b, ORD-2 with PD9 and F6 boundaries, ORD-5, ORD-6
      - `test_projection.py::test_order_status_map_total` (ORD-7)
      - `test_portal_tanks.py` (TNK-1)
      - the order and tank cases of `test_portal_isolation.py` (ISO-C-1, ISO-C-2, ISO-T-1)
      - `test_scoped_readers.py` (ISO-C-3)
      - `test_portal_scope_import_lint.py` (ISO-C-5)
      - the order part of `test_portal_projection.py` (ISO-C-6)

      The FEAT-001 generic suites (XR-6, XR-8, AUD-1, RL-1) must still pass with the new routes. Regenerate and commit the registry.

## FEAT-004: Invoices (R5)

- [ ] 10. Add a `statuses` filter through invoice reads (§2.4).
      Files: `commerce/services/invoice_service.py` (`list`, `count`, `_invoice_must_clauses`; re-read after the OI-14 rebase), `persistence/read_repositories.py`, `commerce/services/commerce_persistence_bridge.py`
      Verify: extended `tests/commerce/test_invoice_status_filter.py` passes on both the ES-fake and bridge paths, and `tests/commerce` passes unchanged.

- [ ] 11. Invoice reader, projection, PDF, CSV, and the invoice endpoints (§5, D8).
      Add `PortalInvoiceReader` and the `PortalInvoice` projection, which builds the account-name map by following `next_cursor` up to 500 accounts. Add `render_invoice_pdf` (reportlab canvas, 30 lines per page) and `ExportType "portal_invoices"` with its CSV columns. Add `require_portal_invoicing` and the 4 invoice routes; `/export` is declared before `/{invoice_id}`. `payment_attempt` stays null until FEAT-005.
      Files: `portal/services/{scoped_readers,projection,invoice_pdf,portal_invoice_service}.py`, `portal/models.py`, `portal/api/invoice_endpoints.py`, `services/csv_export.py`, `bootstrap/core.py`, `main.py` / the router tuple
      Verify: these pass, along with `tests/unit/test_csv_export_helper.py`:
      - `tests/portal/test_portal_invoices.py` (INV-1)
      - the invoice, PDF and CSV cases of `test_portal_isolation.py` (ISO-C-1, ISO-C-2, ISO-T-1)
      - `test_scoped_readers.py` (ISO-C-3 invoice spy)
      - `test_portal_projection.py` (ISO-C-6 for list, detail, PDF via pypdf, and CSV)

      Regenerate and commit the registry.

## FEAT-005: ACH payments and webhook reconciliation (R6)

- [ ] 12. Stripe connector methods and the portal factory (§3.1, F7).
      Add `create_portal_ach_intent`, `cancel_intent`, `retrieve_intent` and `get_publishable_key`, all with a per-request `api_key=` that never writes `stripe_sdk.api_key`. Add `_portal_stripe_connector_factory` (enabled instances only) and the `configure_portal_payments(...)` call in `bootstrap/agents.py`.
      Files: `integrations/stripe_connector.py`, `bootstrap/agents.py`, `portal/services/portal_payment_service.py`
      Verify: `tests/portal/test_stripe_connector_portal.py` (PAY-12, using a fake stripe module) and `test_portal_stripe_factory.py` (PAY-10 factory cases) pass. `tests/unit/test_stripe_connector.py` passes unmodified.

- [ ] 13. `PortalPaymentAttemptStore`, `PortalPaymentService.create`, and the payment endpoints (§6.1, §6.2, F2, F4, DV3, DV8).
      Implement the replay rules, Transaction A under `pg_advisory_xact_lock('portal-pay:…')`, the step 1b same-key re-check, the supersede and stale rules with `PORTAL_CREATING_STALE_SECONDS = 120` and an injectable clock, `_portal_intent_body` as the only body builder, and the conditional post-Stripe updates. Add `POST /api/portal/invoices/{id}/payments` and `GET /api/portal/payment-attempts/{id}` (`include_client_secret`). Wire the invoice detail's `payment_attempt` and the `payable` rule.
      Files: `portal/services/portal_payment_service.py`, `portal/api/payment_endpoints.py`, `portal/services/projection.py`, `main.py` / the router tuple
      Verify: `tests/portal/test_portal_payments.py` passes: PAY-1 (sequential, plus 119 s / 121 s), PAY-2, PAY-3, PAY-4 (all branches), PAY-7, PAY-10, PAY-11, PAY-14. The payment cases of `test_portal_isolation.py` (ISO-C-1) also pass. On the scratch DB, `test_concurrent_same_key`, `test_concurrent_creates_serialized` (PAY-5) and `test_attempt_queries_scoped` (ISO-T-4) pass.

- [ ] 14. Webhook routing, `PortalPaymentReconciler` with `_ensure_recorded_and_applied` (F8), and the runbook (§6.3).
      The portal branch sits after signature verification and before the existing `try/except`. The `charge.*` events try the portal handler first and fall through when it isn't a portal attempt. `configure_stripe_endpoints(portal_payment_handler=...)`.
      Files: `portal/services/portal_payment_reconciler.py`, `integrations/api/stripe_endpoints.py`, `bootstrap/agents.py`, `docs/runbooks/customer-portal.md`
      Verify: `tests/portal/test_portal_stripe_webhook.py` passes: PAY-6, PAY-6c (1)–(4), PAY-6d, PAY-6e, PAY-8, PAY-9. `test_portal_isolation.py::test_webhook_tenant_mismatch` (ISO-T-3) and the concurrent PAY-6 `[pg]` variant pass. `tests/unit/test_stripe_endpoints.py`, `tests/unit/test_stripe_connector.py`, `tests/commerce/services/test_payment_service.py` and `test_invoice_service.py` pass unmodified. Regenerate and commit the registry.

## FEAT-006: Frontend, staff UI, CSP, a11y tooling

- [ ] 15. Role awareness and routing (§10.1).
      `npm ci` in `<worktree>/runsheet`. Add `Role "customer"` and make `canSee` false for every module when the role is present. Change to `Session.init({ onHandleEvent })` dispatching `runsheet:session-changed`. Add `AudienceGuard`, mounted in `app/layout.tsx`. Sign-in routes customers to `/portal`.
      Files: `runsheet/src/config/modules.ts`, `src/config/supertokens.ts`, `src/components/AudienceGuard.tsx`, `src/app/layout.tsx`, `src/app/signin/page.tsx`
      Verify: `npm test -- --ci AudienceGuard signin modules` passes (UI-1), and `npx tsc --noEmit` is clean.

- [ ] 16. `portalApi.ts`, the Stripe typings, and the portal route group and components (§10.2, §6.4).
      Files: `runsheet/src/services/portalApi.ts`, `src/types/stripe-js.d.ts`, `src/app/portal/**` (the 9 pages, each segment with a `layout.tsx` and `metadata.title`), `src/components/portal/*` (`PortalShell`, `PortalNav`, `StatusText`, `LiveRegion`, `OrderRequestForm`, `InvoiceTable`, `PaymentForm`, `TankCard`, `DeliveryHistoryTable`)
      Verify: Jest passes for `portal/layout.test.tsx` (UI-2, T-UI-SHELL), `components/portal/__tests__/OrderRequestForm.test.tsx` (OID-4 plus UI-3), and the PaymentForm, TankCard and InvoiceTable tests (UI-3, the PD15, stale, no-forecast and 429 messages). `npx tsc --noEmit` and `npm run lint` are clean.

- [ ] 17. Staff UI additions and CSP (§10.3, §10.4).
      Add `PortalAccessPanel` on `CustomerDetailPage`, admin only and hidden on 404. Add the Orders "Awaiting confirmation" filter, with a `hold_reason` param in `ordersApi.ts`. Add Confirm / Decline in `OrderDetailView`, which reloads on a 409. Add the Stripe CSP origins while keeping Report-Only and the existing `/api/csp-report` collector.
      Files: `runsheet/src/components/commerce/PortalAccessPanel.tsx`, `src/components/commerce/CustomerDetailPage.tsx`, `src/components/ops/OrdersPage.tsx`, `src/services/ordersApi.ts`, `src/components/orders/OrderDetailView.tsx`, `src/config/securityHeaders.ts`
      Verify: `PortalAccessPanel.test.tsx`, the extended OrderDetailView test (UI-4) and the extended `securityHeaders.test.ts` (UI-5) pass. All existing staff Jest suites pass unchanged.

- [ ] 18. The a11y dev dependency and the portal Playwright specs (§10.5, F5).
      Run `npm install --save-dev --save-exact @axe-core/playwright@4.13.0`. Add `runsheet/e2e/portal/*.spec.ts`: an axe check on every portal page (zero serious or critical violations), keyboard-only sign-in → new request → PDF download, and 320 px reflow. The specs read `PLAYWRIGHT_BASE_URL` and the QA credentials from env and skip without them. They run on staging in the verify step, not in CI.
      Files: `runsheet/package.json`, `runsheet/package-lock.json`, `runsheet/e2e/portal/*.spec.ts`
      Verify: `npx playwright test e2e/portal --list` discovers the specs. The full UI gate passes: `npx tsc --noEmit && npm run lint && npm test -- --ci && npm run build`.

## Integration: CI-equivalent suites (run after FEAT-006, and again after every review fix)

- [ ] 19. Run every CI-equivalent suite from `.github/workflows/ci.yml` on the final tree. Record each command and its summary line in `/Users/olukotunjosh/Downloads/Runsheet/.agents/tasks/customer-portal/evidence.md` and in the commit message.
      - backend-tests: `cd Runsheet-backend && <venv>/bin/python -m pytest --cov=. --cov-report=xml:coverage.xml --cov-report=term-missing --cov-fail-under=70 -x -q`, then `<venv>/bin/python scripts/check_coverage.py --threshold 0`
      - endpoint-registry: `<venv>/bin/python scripts/generate_endpoint_registry.py`, then `git -C <worktree> diff --exit-code docs/endpoint-registry.md` (clean)
      - migration-check, on the scratch DB `runsheet_portal_ci`:
        - `alembic upgrade head`
        - `python -m scripts.check_migrations`
        - `alembic heads | grep -c '(head)'` = 1
        - `alembic downgrade -1 && alembic upgrade head`
        - `POSTGRES_TEST_URL=… pytest tests/postgres -q --no-cov -p no:cacheprovider` with passed and 0 skipped
      - git-hygiene: the `main.py` effective line count is ≤ 200, and no generated artifacts are tracked
      - ui: `cd runsheet && npm ci && npx tsc --noEmit && npm run lint && npm test -- --ci && npm run build`
      - Not run in CI, and run by the verify step on staging: `runsheet/e2e/portal` (Playwright and axe, F5). Live ACH needs a Stripe test-mode integration for `demo-tenant` (owner dependency O1). Without one, record `blocked: Stripe test keys`, and don't create or rotate any credential.

      Verify: every command above exits 0, and the evidence file lists them with results.

## Requirement → item map

- R1: item 6
- R2: items 3–5, plus the per-aggregate isolation cases in items 9, 11, 13 and 14
- R3: item 5
- R4: items 7–9 and 16–17
- R5: items 10–11
- R6: items 12–14 and 16
- R7: item 9
- R8: items 4–5 (middleware), 8 and 13 (durable actor records)
- R9: item 5 (helpers and coverage test), applied in every route item
- R10: items 15–18
- AC1–AC17: the tests named in each item
- AC18 and AC20: the verify step (F5)
- AC19: item 19

## Gaps and assumptions

- The design's §13 names `tests/postgres/test_migrations.py (existing)`. No such file exists. MIG-1 is covered by the item 2 and item 19 alembic commands, which mirror the CI migration-check job, plus `tests/persistence/test_migration_check.py`.
- `docs/runbooks/` doesn't exist yet. Item 14 creates it.
- The base branch moved after the design was written (25 commits). Item 1 rebases first. Items 7, 10 and 17 re-read the touched files after the rebase (OI-41, OI-14, OI-10) before editing.
