# Design: Customer portal, v1 (OI-06)

## Overview

The portal adds one new identity type, the `customer` role, bound to exactly one commerce `customer_id`. It also adds one new API surface (`/api/portal/*`) and one new UI route group (`runsheet/src/app/portal/`). Every other part of the system stays closed to that identity. Isolation has three layers, each with its own owner:

1. A **central default-deny** in the shared auth layer. Any session holding `customer` is refused on every HTTP route outside a short allowlist, and on every WebSocket handshake, before any handler runs.
2. A **portal guard dependency** that every portal route uses. It checks the feature flag, then the exact `customer` role, then that the binding is still live. It produces an immutable `PortalScope(tenant_id, customer_id, user_id)` from the verified session only.
3. **Scoped readers.** These are the only code that portal handlers call to reach data. Each takes a `PortalScope` and passes both `tenant_id` and `customer_id` into the data-store query. A scope with an empty `customer_id` cannot be constructed.

Portal writes reuse existing mechanisms:

- Order requests go through `OrderIntakePipeline` on the reserved `web_portal` channel. They land in the existing `on_hold` state with a new hold reason, and dispatchers confirm them with the existing `release-hold` route.
- Payments are Stripe `us_bank_account` PaymentIntents. They are recorded in commerce only on `payment_intent.succeeded`, through the existing idempotent `payment_service.ingest`, from a new metadata-routed branch of the existing Stripe webhook.
- Invoice CSV uses `services/csv_export.py`. Invoice PDF uses reportlab, following the BOL renderer precedent.

The design adds two tables and one column in a single Alembic revision on top of the current head.

**Technology stack (locked):**

- Backend: Python / FastAPI, SQLAlchemy async + Alembic (Postgres), the existing document store (`ElasticsearchService` facade, served from `es_documents` / ES), SuperTokens Python SDK 0.31.3, slowapi, reportlab 4.5.0, and the `stripe` SDK already in `requirements.txt` (range `>=10.0.0,<13.0.0`, unchanged).
- Frontend: Next.js 15.5 App Router, React 19, and `supertokens-auth-react` 0.51.2. Stripe.js is loaded from `https://js.stripe.com/v3` with `next/script`, with no npm package. Tests use Jest + Testing Library, plus Playwright with `@axe-core/playwright@4.13.0` (exact pin, dev only).
- No new backend dependencies. No new runtime frontend dependencies.

## Codebase facts this design relies on

All of these were checked in worktree `.worktrees/customer-portal` at `9e95e56`, and re-checked at `77449ed` for the revision (the last nine bullets were added by the revision):

- `ops/middleware/tenant_guard.py`: `TenantContext` is a dataclass. Its last field, `driver_id`, has a default. `_context_from_session_claims` builds it from signed claims. `_SuperTokensSessionVerifier` memoizes `VerifiedSession` on `request.state`, so the middleware and the dependency share one verification.
- `middleware/auth_enforcement.py`: `AuthEnforcementMiddleware` (a `BaseHTTPMiddleware`) verifies a session on every non-public path and builds its own JSON 401, because it runs outside FastAPI's exception handlers. Public paths and the test-auth bypass return early.
- `bootstrap/websockets.py`: all 10 WebSocket routes authenticate through `_resolve_ws_claims` (one choke point).
- `auth/supertokens_init.py`: `CANONICAL_ROLES`, `CUSTOMER_ASSIGNABLE_ROLES` and `_lookup_auth_user_claims`, which selects `tenant_id, roles, has_pii_access, driver_id` by `st_user_id`.
- `fuel/api/driver_endpoints.py` `AppAccessService` is the precedent for grant and revoke. It works as an ordered commit with compensation: a unit of work over `auth_users`, the provisioner running inside the transaction, audit on every outcome, and revoke that removes the role and calls `revoke_all_sessions_for_user`.
- `auth/password_admin.create_password_set_link(email, tenant_id=...)` mints a SuperTokens reset link, scoped by tenant.
- There is no `/api/auth/me` route. The real routes are `GET /api/auth/account/me` and `POST /api/auth/account/change-password` (`auth/api/account_endpoints.py`). The PD4 allowlist uses these real paths.
- `OrderIntakePipeline._ingest_common` checks the overlay flag first and returns `legacy_passthrough` when it is `disabled`. It then checks idempotency on `event_id = client_event_id`, then runs the adapter. `_complete_order_doc` sets `status = "placed"` and mints `order_id`; CSV derives a deterministic id instead. `_verify_customer_tank` and the `before_accept` hooks run next (credit can set `on_hold` / `credit_limit_exceeded`), then `FuelOrder.model_validate`, then the upsert. No `web_portal` adapter is registered (`bootstrap/fuel.py:115`).
- `FuelOrder` requires `customer_name`, `ship_to_address` and `call_type`. `CustomerTank` has no address and no name. It has `location_lat/lon`, `zip_code`, `external_tank_id`, `fuel_product_code`, `capacity_gallons`, `current_level_gallons`, `last_reading_at`, `status` and `last_refill_order_id`.
- `FuelOrderRepository` has `search(...)` (single `status`, `customer_id`, no `customer_tank_id` filter) and `atomic_update(index, id, transform)` used by `claim_assignment` (CAS under a row lock).
- The `release-hold` and `cancel` endpoints in `fuel/api/order_endpoints.py` read, check and then write with `_apply_order_update`. That write is not a compare-and-set.
- `InvoiceService.list/count/get` take a single `status`. `get` is tenant-scoped only. Lists filter `customer_id` only when it is truthy (`if customer_id:`).
- `PaymentService.ingest` is idempotent on `(tenant, source, external_id)`: there is an idempotency-service check, and when payments are authoritative there is also a Postgres unique constraint. It handles overpayment through credit-balance accrual.
- The `POST /webhooks/stripe/{tenant_id}` handler resolves the connector (404 if none) and verifies the signature. It then always calls `connector.handle_webhook_event`, which only handles `metadata.reconciliation_id`. `StripeConnector.sync_push` is gated by autocharge and a ceiling, so it doesn't fit customer-initiated payments.
- The forecast documents (`mvp_tank_forecasts`) carry `customer_id`, `customer_tank_id`, `hours_to_runout_p50/p90` and `timestamp`. There is no reorder-point field.
- `TenantSettings` has `region`, `measurement_units` and `default_depot_id`. There is **no tenant display name and no time zone**. BOL rendering falls back to `tenant_name = tenant_id` (`driver/services/pod_bol_finalizer.py`).
- The Alembic head is `0010_acct_override_audit`. CI fails when there is more than one head (`.github/workflows/ci.yml` "alembic heads" step). Playwright does not run in CI today.
- main.py registers middleware in this order at import time: auth enforcement, then RequestID / rate limiter / security headers, then CORS. Each later registration wraps the earlier ones, so it sits outside them.
- `before_accept` hooks are registered from three bootstrap modules (`bootstrap/core.py:498-499` Pricing and Credit, `bootstrap/compliance.py:724+` DyedDiesel, `bootstrap/fuel.py:219` VoiceReviewHold), so their relative order is a bootstrap-ordering detail. `register_release_hold_hook` has no production caller: `release-hold` does **not** re-run credit.
- `PricingError` (`commerce/services/pricing_engine.py:51`) and `DyedDieselOrderRejected` (`compliance/hooks/dyed_diesel_intake_hook.py:27`) are plain `Exception` subclasses. The pipeline re-raises hook exceptions unchanged.
- `StripeConnector` assigns the process-global `stripe_sdk.api_key` before each `asyncio.to_thread` SDK call (`integrations/stripe_connector.py:447,617,741`).
- `_stripe_connector_factory` is a closure in `bootstrap/agents.py:1900`. It lists instances with `enabled=None` and falls back to a disabled instance so webhooks keep verifying.
- `CustomerService` is built only when `commerce_backbone_enabled` is on (`bootstrap/core.py:380-391`).
- `PaymentService.ingest` opens and commits its own sessions. `PaymentService.find_by_external_id(*, tenant_id, source, external_id)` is already public (`commerce/services/payment_service.py:805`).
- `supertokens_python.asyncio.delete_user` exists in the pinned SDK.
- `.env.staging` is gitignored. The staging task env is set in `scripts/staging_aws.sh` (`COMMERCE_BACKBONE_ENABLED` at line 1592).

## 1. Identity: the `customer` role and account linkage

### 1.1 Role constants (`auth/supertokens_init.py`)

- Add `"customer"` to `CANONICAL_ROLES`, so the provisioning script creates the role in the core.
- Add `CUSTOMER_PORTAL_ROLE: str = "customer"`.
- Leave `CUSTOMER_ASSIGNABLE_ROLES` unchanged. `customer` is deliberately absent (PD1).
- Add `STAFF_ROLES = ("admin", "dispatcher", "driver", "platform_admin")` for the staff-deny test inventory.
- Update the docstrings that enumerate roles.

### 1.2 Claims

`_lookup_auth_user_claims` also selects `customer_id`. When it is non-empty, `claims["customer_id"] = customer_id`.

`TenantContext` gains `customer_id: Optional[str] = None`, appended last with a default so every existing construction keeps working. `_context_from_session_claims` coerces it exactly like `driver_id`: a non-empty string or `None`.

`auth/test_auth.issue_test_context` gains an optional `customer_id` keyword argument.

### 1.3 Storage (`auth_users.customer_id`, plus a DB invariant)

The new nullable column is `auth_users.customer_id VARCHAR(64)`. The DB **owns** the role-exclusivity invariant (PD1) through one CHECK constraint, so no code path, script or manual SQL can produce a mixed row:

```sql
CONSTRAINT ck_auth_users_customer_binding CHECK (
  (customer_id IS NULL AND NOT ('customer' = ANY(roles)))
  OR
  (customer_id IS NOT NULL AND roles = ARRAY['customer']::text[]
     AND driver_id IS NULL AND has_pii_access = false)
)
```

There is also an index `ix_auth_users_tenant_customer (tenant_id, customer_id) WHERE customer_id IS NOT NULL`. No FK to `customers`: the commerce `customers` table isn't authoritative in every deployment (ES or Postgres read cutover). The binding is validated through `CustomerService.get` at invite time, and re-checked on every request (§2.3).

### 1.4 Grants table (`portal_user_grants`)

`auth_users` holds only the *current* binding, which the claims need. Listing invited, active and revoked users (R1.8) and the 10-user cap (R1.5) need history, so they get their own table:

| column | type | notes |
|---|---|---|
| `grant_id` | `VARCHAR(64)` PK | `pug_<uuid4>` |
| `tenant_id` | `VARCHAR(128)` NOT NULL | |
| `customer_id` | `VARCHAR(64)` NOT NULL | |
| `email` | `CITEXT` NOT NULL | |
| `status` | `VARCHAR(16)` NOT NULL, CHECK in (`active`, `revoked`) | |
| `first_seen_at` | `TIMESTAMPTZ` NULL | Set by the first successful portal request. "invited" means `active` with this column null. |
| `created_by`, `created_at` | | actor user id, `now()` |
| `revoked_by`, `revoked_at` | NULL | |

Indexes:

- `uq_portal_grant_active_email (tenant_id, email) WHERE status = 'active'`
- `ix_portal_grant_customer (tenant_id, customer_id, status)`

Displayed status: `invited` when `active` and `first_seen_at IS NULL`, `active` when `active` and the column is set, and `revoked`.

### 1.5 Provisioner guard (`auth/provisioner.py`)

- `AuthUserRow` gains `customer_id: Optional[str] = None`.
- `_build_metadata` adds `"customer_id"`, keeping the metadata an exact image of the row.
- `provision_user` raises `ValueError("invalid_customer_binding")` before any SuperTokens write when the row violates the §1.3 invariant. This puts the invariant in a second layer for rows that never touch Postgres (tests, the CLI).

This, plus the CHECK constraint, satisfies R1.10. `scripts/provision_auth_users.py` reads rows that the constraint already guarantees, and the driver grant path is closed by §1.6.

### 1.6 Other grant paths refuse customer rows

In `AppAccessService.grant` (driver app access), after `read_row`, add one more rejection: an existing row holding `customer` (or a non-null `customer_id`) gets the same indistinguishable 409 `APP_ACCESS_ALREADY_LINKED`, with audit outcome `rejected:customer_identity`. Without this, `_roles_with_driver` would append `driver` and the CHECK would turn the request into a 500. `PostgresAppAccessUnitOfWork.read_row` selects `customer_id` too.

### 1.7 Portal access service (`portal/services/portal_access_service.py`)

This follows `AppAccessService` method for method. Routes are in `portal/api/admin_endpoints.py`, with router prefix `/api/commerce/customers/{customer_id}/portal-users` so it is clearly staff and never matches the customer allowlist:

| Route | Body / params | Success |
|---|---|---|
| `GET ""` | | 200 `{data: [{grant_id, email, status, created_at, revoked_at}]}`, newest first |
| `POST ""` | `{email}` (`extra="forbid"`, 3–320 chars, the same shape check as `AppAccessGrantRequest`) | 201 `{grant_id, email, status, password_set_link, link_error, email_sent}`, or 200 with the same shape and `already_invited: true` |
| `POST "/{grant_id}/resend"` | | 200 `{password_set_link, link_error, email_sent}` |
| `DELETE "/{grant_id}"` | | 200 `{grant_id, status: "revoked"}` |

The gate dependency `require_portal_admin`, in order:

1. `portal_enabled(settings)` (§2.3 step 1: portal flag AND commerce backbone), else 404 `PORTAL_DISABLED`.
2. `get_tenant_context`.
3. `require_role(tenant, "admin")`, else 403 `INSUFFICIENT_ROLE`. This covers dispatcher, driver and platform_admin-only callers. A `customer` session never reaches it, because the central deny refuses it first.
4. `CustomerService.get(tenant_id, customer_id)`. Not found means 404 `RESOURCE_NOT_FOUND`, and nothing is written (R1.3).

**Invite.** All of steps 1–7 run in one Postgres transaction through `default_portal_access_uow()` (a `session_scope`):

1. If the customer is `archived`, return 409 `PORTAL_CUSTOMER_ARCHIVED`.
2. `SELECT pg_advisory_xact_lock(hashtext('portal-grants:' || :tenant || ':' || :customer))`. **FREEZE F1**: invites and revokes for one customer are serialized, which makes the count-then-insert cap exact without SERIALIZABLE isolation.
3. Read the `auth_users` row by email, unscoped on purpose (same reasoning as the driver cross-tenant guard). It is acceptable only when all of these hold:
   - `tenant_id` equals the caller's tenant
   - `roles` is `[]` or `["customer"]`
   - `customer_id` is null or this customer
   - `driver_id` is null

   Otherwise return 409 `PORTAL_EMAIL_IN_USE` with one fixed message, so the route can't be used to enumerate accounts. The real reason (`cross_tenant`, `staff_role`, `other_customer`) is logged at WARN and goes in the audit outcome (R1.4, PD21).
4. If an `active` grant already exists for (tenant, email, this customer), skip to step 7 and return 200 `already_invited`.
5. Count `active` grants for the customer. If there are already `portal_max_users_per_customer` (10) or more, return 409 `PORTAL_USER_LIMIT_REACHED` (R1.5).
6. Upsert `auth_users` (`roles = {customer}`, `customer_id`, `has_pii_access = false`, `driver_id = NULL`), then insert the grant row.
7. Call `provision_user(AuthUserRow(...), admin=..., store=uow)` inside the transaction, so `st_user_id` is written back inside it. A `ProvisioningConflictError` (an unbound SuperTokens user) gives 409 `PORTAL_EMAIL_IN_USE`.

The transaction commits here. If anything after the provisioner fails, compensate as the driver flow does: remove `customer` from the SuperTokens user's roles unless it already held them.

After the commit:

- `create_password_set_link(email, tenant_id=tenant)`. On `PasswordAdminError`, the grant stays in place. Respond with `password_set_link: null` and `link_error: true`, and log WARN. The admin uses resend.
- When `settings.smtp_configured`, or the SuperTokens built-in email is in use (it always is when SMTP is unset), call `supertokens_python.recipe.emailpassword.asyncio.send_reset_password_email(DEFAULT_ST_TENANT_ID, st_user_id, email)`. If it raises, log WARN and set `email_sent: false`. A second token is harmless: both are single-use and expire on the core's schedule.

**Role existence.** `SDKSuperTokensAdmin.set_user_roles` needs the role to exist in the core. Startup (`init_supertokens`) does not call the core. So `PortalAccessService.invite` calls `userroles.asyncio.create_new_role_or_add_permissions("customer", [])` once per process, before the first provisioning. The call is idempotent and memoized in a module flag.

**Revoke removes the identity; it never leaves an empty one** (review H1). An emptied row (`roles = '{}'`, still bound to `st_user_id`, password still valid) would sign in again as a role-less tenant session, which the central deny treats as staff and which reaches tenant-wide routes such as `GET /api/fuel/mvp/forecasts`. Because the §1.3 CHECK guarantees a bound customer row holds nothing but the portal binding, deleting it loses nothing. Revoke runs in one transaction, under the same advisory lock:

1. Load the grant by `(tenant, customer, grant_id)`. If it is missing or already revoked, return 404.
2. Load `st_user_id` from `auth_users` by `(email, tenant, customer_id)`. When a row exists:
   1. `set_user_roles(st_user_id, [])`
   2. `revoke_all_sessions_for_user(st_user_id)`
   3. `supertokens_python.asyncio.delete_user(st_user_id)`. An unknown user is treated as success, so a retry is idempotent.
3. `DELETE FROM auth_users WHERE email = :email AND tenant_id = :tenant AND customer_id = :customer AND roles = ARRAY['customer']::text[]`. The predicate can only match a portal-only row, so a staff or driver row can never be deleted here. Then set the grant to `revoked` with `revoked_by` / `revoked_at`. The grant row keeps the history (D2).
4. Invalidate the local principal cache entries for that user (§2.3).

Failure handling: any SuperTokens error propagates as 500 `INTERNAL_ERROR`, logged at ERROR with `grant_id`. The DB transaction rolls back and the admin retries. If SuperTokens succeeded but the DB commit failed, the SuperTokens user is already gone, so the person can't sign in; the retry finds no SuperTokens user, treats that as success, and finishes the DB part. Even when session revocation partly fails, the per-request principal check (§2.3) denies within 60 s, because the grant is no longer `active` (R1.7, R2.11).

A re-invite of the same email after revoke finds no `auth_users` row and provisions a fresh SuperTokens user, so no orphaned SuperTokens user causes a `ProvisioningConflictError`.

**Resend:** the grant must be `active`. Then run the same link mint and email as invite.

**Customer archive hook:** after `CustomerService.archive` succeeds, the existing archive handler (`commerce/api/customer_endpoints.py`) calls `portal_access_service.revoke_sessions_for_customer(tenant, customer_id)`. That revokes the SuperTokens sessions of every `active` grant, leaving the grants active: un-archiving restores access, as PD22 intends. The call is best effort and logs ERROR on failure. The per-request check (§2.3) is the guarantee (R2.12).

**Audit:** each operation and each rejection emits one `portal_audit` line (§8) with `action` = `portal_user_invite`, `portal_user_resend` or `portal_user_revoke`, `target_ids = {grant_id, customer_id}`, and the outcome. It also logs through `telemetry.log_audit_event` like the driver flow does, with no email in the `portal_audit` line.

## 2. Authorization and data scoping

### 2.1 Where each check lives

| # | Check | Enforced in | Why there |
|---|---|---|---|
| E1 | A `customer` session on any non-allowlisted HTTP route gets 403 `PORTAL_ROUTE_FORBIDDEN` | `AuthEnforcementMiddleware.dispatch`, right after a session verifies | Runs before routing and before any handler or dependency. It covers every current and future route regardless of its own role checks (PD4). |
| E1b | The same decision, again | `get_tenant_context`, after building the context | Defense in depth, in case the middleware is mis-registered or bypassed by a non-HTTP mount. It costs one set lookup. |
| E2 | A malformed customer session (customer plus another role, or no `customer_id`) gets 403 `PORTAL_IDENTITY_INVALID` on every authenticated route | The same two places as E1 | R2.2. The DB constraint makes it unreachable from real data. This catches forged or legacy claims. |
| E3 | A `customer` WebSocket handshake is refused (close 4001) | `bootstrap/websockets._resolve_ws_claims` returns `None` | It is the one choke point all 10 sockets use (R2.4). |
| E4 | `portal_enabled(settings)` is false (portal flag off, **or** commerce backbone off): 404 `PORTAL_DISABLED` | `require_portal_customer` / `require_portal_admin`, first step | Flag before role, the commerce precedent (R2.6). The portal needs `CustomerService`, which exists only with the backbone (review M3). |
| E5 | A non-customer session on a portal route gets 403 `INSUFFICIENT_ROLE` | `require_portal_customer`, using `require_role(tenant, "customer")` | PD5. It reuses the shared exact-match mechanism. |
| E6 | Binding revoked or customer archived: 403 `PORTAL_ACCESS_SUSPENDED` | `require_portal_customer`, using `PortalPrincipalChecker` (cache of at most 60 s) | R2.11. |
| E7 | `tenant_id` / `customer_id` in query or body: 422 | Router dependency `reject_scope_params` (query) and `extra="forbid"` models (body) | R2.7. |
| E8 | Every data read is filtered by both `tenant_id` and `customer_id` in the store query | `portal/services/scoped_readers.py` | R2.9. These readers are the only module portal handlers may import for data. A lint test enforces that (§13, T-SCOPE-IMPORT). |
| E9 | An out-of-scope id gives 404 `RESOURCE_NOT_FOUND` with the not-exists body | Scoped readers' `get_*` methods | PD6. The single-id lookup fetches by (tenant, id), then compares `customer_id` and raises the *same* `resource_not_found(...)` call as the missing case. |
| E10 | Responses contain only allowlisted fields | `portal/models.py` response models (`extra="forbid"`) built only from projection functions | R2.10. |

### 2.2 Central deny (`portal/scope.py`, used by E1/E1b/E2/E3)

```python
PORTAL_ROLE = "customer"
PORTAL_PATH_PREFIX = "/api/portal/"
CUSTOMER_ALLOWED_EXACT = frozenset({
    "/api/auth/account/me",
    "/api/auth/account/change-password",
})

def customer_session_verdict(claims: Mapping[str, Any], path: str) -> Optional[str]:
    """None = allow; else the ErrorCode value to refuse with."""
    roles = [r for r in (claims.get("roles") or []) if isinstance(r, str)]
    if PORTAL_ROLE not in roles:
        return None                                   # staff: unchanged behavior
    if roles != [PORTAL_ROLE] or not _nonempty_str(claims.get("customer_id")):
        return "PORTAL_IDENTITY_INVALID"
    if path.startswith(PORTAL_PATH_PREFIX) or path in CUSTOMER_ALLOWED_EXACT:
        return None
    return "PORTAL_ROUTE_FORBIDDEN"
```

Public routes (`is_public_route`), including `/auth/*` for sign-in, sign-out and refresh, never reach the check, because the middleware returns early for them. That is the PD4 "SuperTokens recipe routes and public routes".

In `AuthEnforcementMiddleware.dispatch`, after `_has_verifiable_session` returns `True`:

1. Read the memoized `VerifiedSession` from `request.state._runsheet_verified_session`. The fake verifiers in tests return claims directly. The helper `_verified_claims(request)` falls back to calling the verifier again when nothing is memoized.
2. Get the verdict.
3. For a refusal, return `_forbidden_response(request, code)`. It has the same JSON shape as `_unauthorized_response`, with status 403 and message "This account can't use this part of Runsheet". It emits WARN `portal_audit` with `outcome="forbidden_route"`, `path_template` = the matched route template when one resolves (`request.scope.get("route")` is not set yet in middleware, so the raw path is logged with digits/uuids collapsed to `{id}`), `tenant_id`, `actor_user_id` and `customer_id` (R8.3).

In `get_tenant_context`, the same verdict comes from the built context. Raise `AppException(code)`, which is 403 via `ERROR_CODE_STATUS_MAP`.

In `_resolve_ws_claims`, when the claims carry `customer`, log WARN `portal_audit` (`outcome="forbidden_route"`, `channel="websocket"`) and return `None`. Every caller already closes with 4001 on `None`.

**Actor stamp for refusals (review M5).** `_context_from_session_claims` in `ops/middleware/tenant_guard.py` also sets `request.state.auth_user_id = resolved_user_id`, next to the existing `tenant_id` / `driver_id` stamps. It is additive and changes nothing for staff. `PortalAuditMiddleware` (§8.1) reads `portal_user_id or auth_user_id`, so a staff 403 or a 404 `PORTAL_DISABLED` on a portal route, which fail before the guard's step 5, still carries `actor_user_id`.

The test-auth bypass (`override_auth`) skips the middleware and replaces `get_tenant_context`. The isolation suites therefore must **not** use `override_auth` for customer sessions. They install a fake verifier with `configure_session_verifier` and run with `auth_provider="supertokens"` (§11). Portal handler unit tests may use `override_auth` with a customer context.

### 2.3 Portal guard (`portal/api/_authz.py`)

```python
@dataclass(frozen=True)
class PortalScope:
    tenant_id: str
    customer_id: str
    user_id: str
    tenant: TenantContext          # for helpers that take a context (csv_export, pipeline)
    def __post_init__(self):       # an empty id here would silently widen a store filter
        for name in ("tenant_id", "customer_id", "user_id"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"PortalScope.{name} must be non-empty")
```

`async def require_portal_customer(request, tenant=Depends(get_tenant_context)) -> PortalScope`:

1. If `portal_enabled(settings)` is false, return 404 `PORTAL_DISABLED`. `portal_enabled(s) = s.customer_portal_enabled and s.commerce_backbone_enabled`, defined once in `portal/scope.py` and used by both guards and by `/me`. A `model_validator` on `Settings` logs WARN `customer_portal_enabled without commerce_backbone_enabled; portal stays off` when the first is on and the second is off (it does not raise, so a misconfigured env still boots). Note on ordering: a dependency's sub-dependencies resolve before its own body, so `get_tenant_context` runs first and an unauthenticated caller gets 401. That matches the commerce gates. The flag check comes before the *role* check, which is what R2.6 requires.
2. `require_role(tenant, "customer")`, else 403 `INSUFFICIENT_ROLE` (E5).
3. If `tenant.roles != ["customer"]` or `tenant.customer_id` is empty, return 403 `PORTAL_IDENTITY_INVALID`. This is normally unreachable after E2.
4. `await principal_checker.check(tenant.tenant_id, tenant.user_id, tenant.customer_id)` returns `ok`, `revoked` or `customer_archived`. Anything but `ok` gives 403 `PORTAL_ACCESS_SUSPENDED`. The checker:
   - reads the `active` grant through `auth_users.st_user_id = :user_id` joined to `portal_user_grants`, requiring the same tenant, the same customer and `status = 'active'`
   - reads the customer through `CustomerService.get`
   - caches `(tenant, user, customer_id) → (verdict, expires_at)` in-process (the customer id is in the key so a re-bind within the TTL can't reuse a stale `ok`; revoke invalidates every key for the user) for `portal_principal_cache_seconds` (60)
   - sets `first_seen_at` on the grant once, when it is null
   - fails closed: a store error gives 503 `PORTAL_UNAVAILABLE`, logged at ERROR and never cached
5. Stamp `request.state.portal_tenant_id`, `portal_user_id` and `portal_customer_id`. These feed the rate-limit key and the audit middleware. Also stamp `export_tenant_id` / `export_user_id`, so `stream_csv_export`'s existing audit line works.
6. Return the `PortalScope`.

`reject_scope_params(request)` is a router-level dependency on every portal router. It returns 422 `VALIDATION_ERROR` with `details.fields` when `tenant_id` or `customer_id` appears in `request.query_params`.

### 2.4 Scoped readers (`portal/services/scoped_readers.py`)

There is one class per aggregate. Each method's first parameter is `scope: PortalScope`.

| Reader | Store call (both ids in the query) | Single-id lookup |
|---|---|---|
| `PortalInvoiceReader.list/count` | `InvoiceService.list(tenant_id=scope.tenant_id, customer_id=scope.customer_id, statuses=..., ...)` | `get`: `InvoiceService.get(tenant, id)`. If `inv.customer_id != scope.customer_id` or `inv.status == "draft"`, raise `resource_not_found(f"Invoice '{id}' not found", details={"invoice_id": id})`. That is the same constructor and message the service uses for a miss, so the bodies match (E9). |
| `PortalOrderReader.list` | `FuelOrderRepository.search(tenant, customer_id=scope.customer_id, ...)` | `get`: `repo.get(tenant, id)`, then a customer check, then 404 |
| `PortalTankReader.list` | `CustomerTankRepository.list_for_tenant(tenant, customer_id=..., status="active")` | `get`: `repo.get` plus customer and `active` checks, then 404 |
| `PortalForecastReader.latest_by_tank` | `search_documents("mvp_tank_forecasts", {must: [term tenant_id, term customer_id]}, sort timestamp desc, size 500)`, keeping the first hit per `customer_tank_id` | n/a |
| `PortalPaymentAttemptStore` | SQL `WHERE tenant_id = :t AND customer_id = :c AND ...` | by id, with the same two predicates, else 404 |

Each reader also asserts at runtime that the kwargs it sends include a non-empty `customer_id`. A unit test spies on the underlying service to pin this (T-SCOPE-ARGS).

Additions to existing services, all backward compatible:

- `InvoiceService.list/count`, `_invoice_must_clauses`, `InvoiceReadRepository.list/count`, and the Postgres bridge reads `read_invoice_list` / `read_invoice_count` in `commerce/services/commerce_persistence_bridge.py` (which `list/count` call first on the read-cutover path) gain `statuses: Optional[Sequence[str]] = None`, which becomes ES `terms` or SQL `IN`. It is applied together with `status`. INV-1 runs the draft-exclusion case against both the ES fake and the bridge path.
- `FuelOrderRepository.search` gains `customer_tank_id`, `statuses` (a terms / `in_filters` list) and `hold_reason` term filters on both the ES and Postgres (`read_hybrid_search` `term_filters` / `in_filters`) paths.
- `FuelOrderRepository.transition_if(tenant_id, order_id, *, expected_status, expected_hold_reason=_ANY, update_fields) -> Optional[dict]` runs `atomic_update`, applies `update_fields` only when the current status and hold reason match, and returns the new doc or `None`. The caller then mirrors to Postgres with `mirror_current_state_upsert`, as `_apply_order_update` does. See FREEZE F3.

## 3. Portal API

Every route below sits under a router created with `dependencies=[Depends(reject_scope_params)]`, and every handler takes `request: Request` (slowapi's decorators require it) and `scope: PortalScope = Depends(require_portal_customer)`. The routers are mounted unconditionally in the `main.py` router tuple. The flag answers 404 per request, so route inventories are stable whatever the flag says.

Responses use the existing envelope conventions:

- `{data, next_cursor, limit, request_id}` for lists
- `{data, request_id}` for a single item
- the `AppException` error envelope for errors

Every portal response gets `Cache-Control: no-store` (§8).

| Method & path | Module | Limit (PD19) | Purpose |
|---|---|---|---|
| `GET /api/portal/me` | `me_endpoints` | read | Account overview and capabilities (R3) |
| `GET /api/portal/orders` | `order_endpoints` | read | List the customer's orders (R4.11) |
| `GET /api/portal/orders/{order_id}` | `order_endpoints` | read | Order detail |
| `POST /api/portal/orders` | `order_endpoints` | order submit | Request a delivery (R4.1–R4.5) |
| `POST /api/portal/orders/{order_id}/cancel` | `order_endpoints` | order cancel | Cancel while awaiting confirmation (R4.10) |
| `GET /api/portal/invoices` | `invoice_endpoints` | read | List (R5.1) |
| `GET /api/portal/invoices/export` | `invoice_endpoints` | download | CSV (R5.4) |
| `GET /api/portal/invoices/{invoice_id}` | `invoice_endpoints` | read | Detail plus latest payment attempt (R5.2, R6.13) |
| `GET /api/portal/invoices/{invoice_id}/pdf` | `invoice_endpoints` | download | PDF (R5.3) |
| `POST /api/portal/invoices/{invoice_id}/payments` | `payment_endpoints` | payment create | Start an ACH payment (R6.1–R6.6) |
| `GET /api/portal/payment-attempts/{payment_attempt_id}` | `payment_endpoints` | read | Attempt status, for polling after confirm |
| `GET /api/portal/tanks` | `tank_endpoints` | read | Tank list with forecast and next delivery (R7.1–R7.3) |
| `GET /api/portal/tanks/{customer_tank_id}` | `tank_endpoints` | read | One tank |
| `GET /api/portal/tanks/{customer_tank_id}/deliveries` | `tank_endpoints` | read | Delivery history (R7.4) |

`/invoices/export` is declared before `/invoices/{invoice_id}`, as in the commerce router.

### 3.1 `GET /api/portal/me`

The response is `PortalMe`:

```
{ email, customer_display_name, supplier_name,
  ordering_available: bool, invoices_available: bool, payments_available: bool,
  measurement_units: {volume, distance} }
```

- `email` comes from `password_admin._email_for_st_user_id(scope.user_id)`. On failure it is `""`, as in `/api/auth/account/me`.
- `customer_display_name` comes from `CustomerService.get`.
- `supplier_name` falls back to `tenant_id`, the BOL precedent. A tenant display name doesn't exist yet: see Backlog B1 and Deviation DV1.
- `ordering_available` is `pipeline.get_ordering_state(tenant_id) != "disabled"`. That is a new public wrapper over `_get_overlay_state`, which already fails closed to `disabled`.
- `invoices_available` is `commerce_backbone_enabled and commerce_invoicing_enabled`, and also requires the invoice service to be configured.
- `payments_available` is `invoices_available and await portal_connector_factory(tenant_id) is not None`. A factory exception counts as `False` and is logged at WARN.

**Portal Stripe factory (review M2).** The existing `_stripe_connector_factory` is a closure in `bootstrap/agents.py` that falls back to a *disabled* instance so webhooks keep verifying, so it can't answer "may the portal take payments". `bootstrap/agents.py` builds a second closure, `_portal_stripe_connector_factory`, right after it, using the same repository and vault. It lists instances with `enabled=None`, picks the first one with `enabled` true, and returns `None` when there is none (or when the repository or vault is missing, or the lookup raises, logged WARN). It constructs the connector with the same arguments. It is registered with `portal.services.portal_payment_service.configure_portal_payments(connector_factory=..., payment_service=...)`, called next to `configure_stripe_endpoints`. `/me`, payment create, replay `retrieve`, and `GET /payment-attempts/{id}` use only this factory. The webhook keeps the existing factory, so events for a since-disabled instance still verify and reconcile. When `configure_portal_payments` was never called, the factory is `None` and payments are unavailable.

### 3.2 Common validation

| Input | Rule | On failure |
|---|---|---|
| Path ids (`order_id`, `invoice_id`, `customer_tank_id`, `payment_attempt_id`) | Required, 1–128 chars, regex `^[A-Za-z0-9_.:-]+$` | 404 `RESOURCE_NOT_FOUND`, the same as out of scope. Malformed ids are never treated as different from unknown ids. |
| `cursor` | Optional opaque string, ≤ 256 chars | 422 |
| `limit` | Optional int, 1–50, default 25 | 422 |
| `status` (invoice list) | Optional; one of `open`, `partial`, `paid`, `overdue`, `void` (enum, `draft` excluded) | 422 |
| `start_date` / `end_date` | Optional; parsed by `services.date_range.parse_date_range` on invoice `created_at` (D8, DV9) | 422 `VALIDATION_ERROR` (the existing parser error) |
| Unknown query params | Ignored, except `tenant_id` / `customer_id`, which give 422 (E7) | |
| Body | Pydantic model with `extra="forbid"` | 422 naming the fields |

## 4. Order requests

### 4.1 `POST /api/portal/orders`

The body is `PortalOrderRequest`, with `extra="forbid"`:

| Field | Rule |
|---|---|
| `client_event_id` | Required UUID (any version), as a string |
| `customer_tank_id` | Required. Path-id regex. |
| `quantity` | Required, a discriminated union. Either `{"mode": "fill_to_full"}` or `{"mode": "gallons", "gallons": number}` with 0 < gallons ≤ the tank's `capacity_gallons`. The capacity check runs in the service after the tank loads, and failure gives 422 field `quantity.gallons`. |
| `window_start` | Required. ISO-8601 datetime **with offset**. Must be ≥ now − 24 h and ≤ now + 60 days (see FREEZE F6). |
| `window_end` | Required. ISO-8601 with offset. Must be > `window_start` and `window_end − window_start` ≤ 25 h. |
| `po_number` | Optional. Trimmed, 1–64 chars, no control characters. |
| `notes` | Optional. Trimmed, ≤ 500 chars, no control characters except `\n`. |

The UI always sends a window. When the customer picks only a date, the browser sends local midnight to the next local midnight, which is why the server allows 25 h for DST days. The server never asks for a time zone.

`PortalOrderService.submit(scope, body, request_id)`:

1. Get the ordering state. If it is `disabled`, return 409 `ORDER_INTAKE_DISABLED` before any read (R4.5).
2. `tank = PortalTankReader.get(scope, customer_tank_id)`. If it is not the customer's, or not `active`, return 404 (R4.3).
3. Check gallons against capacity, else 422.
4. `customer = CustomerService.get(...)` supplies `customer_name = display_name`.
5. Choose the ship-to (Decision D4):
   - `ship_to_lat` / `ship_to_lon` come from the tank.
   - `ship_to_address` is the `ship_to_address` of the tank's most recent order (`repo.search(tenant, customer_id, customer_tank_id, size=1, sort created_at:desc)`) when there is one.
   - Otherwise it is `f"{customer.display_name} — tank {tank.external_tank_id or tank.customer_tank_id[-8:]}, ZIP {tank.zip_code}"`.
6. Build the adapter payload with `customer_id = scope.customer_id`, `customer_tank_id`, `product_code = tank.fuel_product_code`, `gallons_requested` / `fill_to_full`, `call_type = "will_call"`, `delivery_window_start/end`, `po_number`, and `special_instructions = notes`.
6a. **Replay pre-check (review H3).** `oid = portal_order_id(scope.tenant_id, scope.user_id, body.client_event_id)`. If `await PortalOrderReader.get_or_none(scope, oid)` returns an order, return 200 with its projection and do not call the pipeline (R4.2). This does not depend on the Redis idempotency marker, which can be missing (72 h TTL, a flush, or a crash between the upsert and `mark_processed`). Step 6a runs after steps 2–5 on purpose, so a replay still gets the same 404 / 422 answers for an invalid body. `get_or_none` is the reader's `get` without the 404: it returns `None` for missing or out-of-scope.
7. `result = await pipeline.ingest_portal(scope=scope, payload=..., request_id=..., client_event_id=body.client_event_id)`. Exceptions from the pipeline are caught as `except (AppException, PricingError, DyedDieselOrderRejected)`, because the pricing and dyed-diesel hooks raise plain exceptions that the pipeline re-raises unchanged (review M1). Map the results:

| Pipeline result | Portal response |
|---|---|
| `legacy_passthrough` (the flag flipped between step 1 and here) | 409 `ORDER_INTAKE_DISABLED` |
| `processed` | 201, `{order_id, status_code: "awaiting_confirmation", status_label: "Awaiting confirmation"}` plus `Location` |
| `duplicate` (the Redis marker hit, or the §4.2 existing-id guard fired for a concurrent first submit) | Load `portal_order_id(...)` through `PortalOrderReader.get`. Return 200 with the original projection (R4.2). If it isn't found (the first attempt crashed after marking the key), return 409 `IDEMPOTENCY_CONFLICT` with the message "Please submit the request again with a new reference." |
| `queued_for_review` (an adapter error, not expected) | 422 `ORDER_REQUEST_REJECTED`, logged at ERROR |
| `AppException` `ORDER_PAYLOAD_INVALID` | 422, passing `invalid_fields` through. Field names are ours, not internal. |
| `AppException` `INVALID_CUSTOMER_TANK_REF` | 404 `RESOURCE_NOT_FOUND` |
| Any other `AppException`, or `PricingError` (no price rule), or `DyedDieselOrderRejected` (no exemption certificate; OI-02 makes this check blocking) | 422 `ORDER_REQUEST_REJECTED` with the message "We couldn't accept this request online. Please contact your supplier." Logged at WARN with `type(exc).__name__` and `getattr(exc, "error_code", None)`; neither is returned. Nothing is written, because hooks run before the upsert. |
| Anything else | Propagates as a 500 |

### 4.2 Pipeline changes (`fuel/services/order_intake_pipeline.py`, `fuel/intake/web_portal_adapter.py`)

- **`WebPortalIntakeAdapter`** (`channel_type = "web_portal"`, `schema_version = "1.0"`) is registered in `bootstrap/fuel.py` next to the dispatcher adapter. It is shaped like `DispatcherIntakeAdapter`: it requires `customer_id`, `customer_name`, `ship_to_address`, `ship_to_lat`, `ship_to_lon`, `customer_tank_id`, `product_code` and `call_type`.
  - It stamps `intake_channel = "web_portal"`, `intake_channel_id = channel.channel_id`, and `intake_metadata = {"portal_session_id": None}`. It does not carry PII.
  - It emits one `order_placed` event whose payload is `{intake_channel, intake_channel_id, actor_user_id}` (R8.4). It carries no `hold_reason`, because the adapter runs before the hooks and can't know which hold the order ends with; the order document is the source of truth for that.
- **`_PortalChannel`** is an ephemeral dataclass like `_CsvImportChannel`: `channel_id = "web-portal"`, `channel_type = "web_portal"`, `supported_schema_versions = ["1.0"]`.
- **`async def ingest_portal(self, *, scope, payload, request_id, client_event_id) -> IntakeResponse`** calls `_ingest_common(channel=_PortalChannel(tenant_id=scope.tenant_id), payload, request_id, actor_user_id=scope.user_id, client_event_id=portal_event_id(scope.user_id, client_event_id))`.
  - `portal_event_id(user, cid) = f"portal:{user}:{cid}"` namespaces the tenant-scoped idempotency key per user, so two users can't collide.
  - `portal_order_id(tenant, user, cid) = "ord_portal_" + sha256(f"{tenant}|{user}|{cid}").hexdigest()[:32]`.
  - Both helpers live in `fuel/services/order_intake_pipeline.py` and are imported by the portal service.
- **`_complete_order_doc`**: when `channel_type == "web_portal"`, set `order_id = portal_order_id(context.tenant_id, context.actor_user_id, <cid>)`. The client id is recovered from the namespaced event id. Otherwise the code is unchanged.
- **Existing-id guard (review H3).** In `_ingest_common`, right after `_complete_order_doc` and before the hooks, when `channel_type == "web_portal"`: if `await FuelOrderRepository(self._es).get(tenant_id, order_doc["order_id"])` returns a document (the same lookup the CSV path uses at `order_intake_pipeline.py:890`), call `mark_processed` for the event id and return `IntakeResponse(status="duplicate", order_id=...)` without running hooks or writing. This mirrors the CSV `stale` branch and covers two concurrent first submits that both pass step 6a. A repository error here propagates (500); it is not treated as "missing", because that would reopen the overwrite.
- **Hold stamp (review H2):** a new step (i3) runs right after the `before_accept` hooks loop and before `FuelOrder.model_validate`. It lives in the pipeline, not in a hook, so it doesn't depend on hook registration order across three bootstrap modules:

  ```python
  if channel_type == "web_portal" and order_doc.get("status") == "placed":
      order_doc["status"] = "on_hold"
      order_doc["hold_reason"] = PORTAL_REVIEW_HOLD_REASON
  # A hold set by a hook (credit_limit_exceeded) is left untouched.
  ```

  - `PORTAL_REVIEW_HOLD_REASON = "awaiting_dispatcher_confirmation"` lives in `fuel/order_models.py` next to `LOADABLE_ORDER_STATUSES`.
  - It never overwrites another hold. `release-hold` does not re-run the credit hook (no production code calls `register_release_hold_hook`), so overwriting `credit_limit_exceeded` would let a dispatcher confirm an over-limit order straight to `placed`.
  - A credit-held portal order shows the customer "On hold" (PD11), has `cancellable = false`, and is released by a dispatcher through the existing `release-hold`, which is already a human review. Other hooks (credit, pricing, dyed diesel) still run as usual (PD8).
  - `release-hold` never re-applies the portal reason, because none of its hooks know it (R4.8).

### 4.3 Planner exclusion

`on_hold` is not in `LOADABLE_ORDER_STATUSES`. The test (T-ORD-LOADABLE) runs the candidate queries of `route_planning_agent`, `delivery_prioritization_agent` and `compartment_loading_agent` against a fixture that holds one portal order. It asserts the order is absent, and that it appears after `release-hold` (R4.9).

### 4.4 Cancel and confirm (FREEZE F3)

`POST /api/portal/orders/{order_id}/cancel`:

- Body: `{}`, with `extra="forbid"`.
- Load the order with `PortalOrderReader.get`; out of scope gives 404.
- Call `transition_if(expected_status="on_hold", expected_hold_reason=PORTAL_REVIEW_HOLD_REASON, update_fields={status: "cancelled", hold_reason: None, updated_at, last_event_timestamp})`.
- If it returns `None`, return 409 `ORDER_NOT_CANCELLABLE` (R4.10).
- On success, mirror to Postgres and append an `order_cancelled` event with `{old_status: "on_hold", reason: "cancelled_by_customer", actor_user_id: scope.user_id}`. Return 200 with the projection.

The cancel and event-append code that the staff `cancel` endpoint has inline is extracted into `fuel/services/order_actions.py`. `cancel_order(repo, tenant_id, order_id, *, actor_user_id, reason, notes, expected_status, expected_hold_reason=_ANY, counter_service)` is called by the staff endpoint (unchanged behavior and response) and by the portal service. The staff endpoint passes `expected_status = order.status` as it read it.

**Staff confirm and decline** reuse `POST /api/orders/{id}/release-hold` and `POST /api/orders/{id}/cancel`. The final status write in `release_hold_order` becomes `transition_if(expected_status="on_hold", expected_hold_reason=order.hold_reason, ...)` instead of `_apply_order_update`. When it returns `None`, the endpoint returns 409 `INVALID_STATUS_TRANSITION` (the existing code), which closes the confirm-vs-customer-cancel race. Only that final write changes. The hook re-run and the event shape stay as they are.

### 4.5 `GET /api/portal/orders` and `/{order_id}`

The list calls `repo.search(tenant, customer_id=scope.customer_id, sort="created_at:desc", keyset=True, after=decoded cursor, size=limit)`. The cursor is `base64url(json([created_at, order_id]))`, and it is validated on decode.

The projection is `PortalOrder` (`extra="forbid"`):

```
order_id, status_code, status_label, product_code, gallons_requested, fill_to_full,
window_start, window_end, po_number, tank: {customer_tank_id, label},
created_at, delivered_at, delivered_gallons, ticket_number, cancellable: bool
```

The status mapping (PD11) is `portal/services/projection.ORDER_STATUS_MAP`. It covers every literal in `fuel.order_models.OrderStatus`, and test T-ORD-MAP iterates `typing.get_args(OrderStatus)` so a new status can't fall through unmapped:

| Internal status | Portal status code |
|---|---|
| `on_hold` with `PORTAL_REVIEW_HOLD_REASON` | `awaiting_confirmation` |
| `on_hold` with any other reason | `on_hold`, reason not shown |
| `placed`, `confirmed`, `scheduled` | `confirmed` |
| `dispatched`, `in_transit` | `out_for_delivery` |
| `delivered` | `delivered` |
| `failed` | `not_delivered` |
| `cancelled` | `cancelled` |

Other fields:

- `cancellable` is true only for `awaiting_confirmation` on a `web_portal` order.
- `delivered_*` and `ticket_number` come only from `delivery_result.{delivered_at, actual_gallons, ticket_number}`.
- Tank `label` is `external_tank_id` if set, else `"Tank …" + last 6 chars of the id`.
- Never projected: driver, truck, run, claim, photos, recipient, geotag, signature, OTP, customer phone and email, special instructions, `hold_reason`, `intake_metadata`, prices.

## 5. Invoices

The routes depend on `require_portal_invoicing(scope=Depends(require_portal_customer))`, which returns 404 `INVOICING_DISABLED` when commerce backbone or invoicing is off, or when the invoice service isn't configured (R5.6).

**Visible statuses:** `PORTAL_INVOICE_STATUSES = ("open", "partial", "paid", "overdue", "void")`. The list passes `statuses=PORTAL_INVOICE_STATUSES`, or `statuses=[status]` when a filter is given, so `draft` is excluded in the query itself (R5.1, R5.5).

**Projection `PortalInvoice`** (`extra="forbid"`):

```
invoice_id, invoice_number, status_code, status_label, issued_at, due_date, created_at,
account_display_name, subtotal_cents, tax_cents, total_cents, amount_paid_cents, remaining_cents,
line_items: [{product_code, quantity_gallons, unit_price_cents, subtotal_cents}],
delivery: {delivered_at, actual_gallons, ticket_number} | null,
payment_attempt: {payment_attempt_id, status_code, status_label, amount_cents, created_at} | null   # detail only
payable: bool
```

- `account_display_name` is resolved once per request from `AccountService.list(tenant, customer_id=scope.customer_id)` into an `{account_id: display_name}` map. Unknown ids show `"Account"`.
- `unit_price_cents` uses `services.money.legacy_unit_price_cents` on the stored micros, as the staff invoice view does.
- `payable` is true when the status is in (`open`, `partial`, `overdue`), `remaining_cents ≥ 100` and `payments_available`.
- `payment_attempt` is the newest attempt for the invoice from `PortalPaymentAttemptStore`. Labels: `creating`/`created`/`pending` show "Payment processing", `succeeded` shows "Paid", and `failed`/`canceled` show "Payment failed, try again" (R6.13).

**PDF (`portal/services/invoice_pdf.py`).** `render_invoice_pdf(invoice: PortalInvoice, *, supplier_name, customer_display_name) -> bytes` uses the reportlab canvas API, as `bol_service._render_pdf` does. It is letter size and paginates line items at 30 per page. Its input type is the projection model, so a field that isn't projected can't be rendered.

The endpoint returns a `Response(content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="invoice_{safe}.pdf"'})`, where `safe = re.sub(r"[^A-Za-z0-9_-]", "_", invoice_number or invoice_id)[:64]`.

Rendering errors are logged at ERROR with `invoice_id`, and the caller gets 500 `INTERNAL_ERROR` with the message "Invoice PDF could not be generated". This is not retried.

**CSV.** `GET /api/portal/invoices/export` accepts the list filters and calls `stream_csv_export(request=..., tenant=scope.tenant, export_type="portal_invoices", columns=PORTAL_INVOICE_EXPORT_COLUMNS, source=..., filters={status, start_date, end_date})`.

- The source is `_invoice_export_source`, adapted to call `PortalInvoiceReader` (both ids, `statuses`).
- The columns are `invoice_number, issued_at, due_date, status, account, total_gallons, subtotal, tax, total, paid, remaining`, with money as decimal dollars formatted by the helper.
- `ExportType` gains `"portal_invoices"`.
- The helper owns the BOM, injection escaping, the 50,000-row cap (413 `EXPORT_TOO_LARGE`, as now) and the audit line.

## 6. ACH payments

### 6.1 Storage (`portal_payment_attempts`)

| column | type | notes |
|---|---|---|
| `payment_attempt_id` | `VARCHAR(64)` PK | `ppa_<uuid4>` |
| `tenant_id` | `VARCHAR(128)` NOT NULL | |
| `customer_id`, `invoice_id`, `account_id` | `VARCHAR(64)` NOT NULL | No FKs (invoices may live in ES) |
| `actor_user_id` | `TEXT` NOT NULL | R8.5 |
| `idempotency_key` | `VARCHAR(255)` NOT NULL | |
| `amount_cents` | `BIGINT` NOT NULL, CHECK `> 0` | |
| `status` | `VARCHAR(16)` NOT NULL, CHECK in (`creating`, `created`, `pending`, `succeeded`, `failed`, `canceled`) | |
| `stripe_payment_intent_id` | `VARCHAR(128)` NULL, UNIQUE | |
| `payment_id` | `VARCHAR(64)` NULL | The commerce Payment, once recorded |
| `failure_code` | `VARCHAR(64)` NULL | Stripe `last_payment_error.code`, or `apply_rejected:<code>`. Never a message. |
| `created_at`, `updated_at`, `terminal_at` | `TIMESTAMPTZ` | |

Constraints:

- `uq_ppa_idem UNIQUE (tenant_id, actor_user_id, idempotency_key)`
- `uq_ppa_inflight UNIQUE (tenant_id, invoice_id) WHERE status IN ('creating','created','pending')`, the database backstop for "one in flight per invoice"
- `ix_ppa_customer (tenant_id, customer_id, invoice_id, created_at DESC)`

No bank details are stored. R6.14's optional last-4 and bank name are not shown in v1 (Backlog B3).

### 6.2 `POST /api/portal/invoices/{invoice_id}/payments`

- Header `Idempotency-Key`: required, 8–255 chars, `[A-Za-z0-9_-]`. A missing key gives 400 `MISSING_IDEMPOTENCY_KEY`.
- Body: `{amount_cents?: int}` with `extra="forbid"`. The default is `remaining_cents`.

The response is 201 `{payment_attempt_id, status_code, amount_cents, client_secret, publishable_key}`, or 200 for a replay.

`PortalPaymentService.create(scope, invoice_id, key, amount)`:

1. Gates: invoicing (404), then the connector, `connector = await portal_connector_factory(tenant)` (§3.1, enabled instances only). If it is `None`, return 409 `PORTAL_PAYMENTS_UNAVAILABLE` (R6.5).
2. **Replay check.** In its own short transaction, select by (tenant, user, key) and apply the replay rules:
   - Not found: continue.
   - Found with a different `invoice_id` or `amount_cents`: 409 `IDEMPOTENCY_CONFLICT`.
   - Found in `creating`: 200 with the attempt, no `client_secret`, and `Retry-After: 2`. The client polls `GET /payment-attempts/{id}` until it is `created` (review M6; R6.2 says the same key returns the existing attempt).
   - Found in `created`: `retrieve_intent(pi_id)`, then 200 with its `client_secret`.
   - Found in any other status: 200 without `client_secret` (R6.2).
3. **Transaction A** (FREEZE F2, serialized per invoice):
   1. `pg_advisory_xact_lock(hashtext('portal-pay:'||tenant||':'||invoice_id))`.
   1b. Repeat the step 2 lookup by (tenant, user, key) and apply the same replay rules. Two requests with the same key (double-click, two tabs) both miss step 2; the second waits on the lock and then finds the first's row here, so it returns that attempt instead of a 409. One extra rule applies here only, under the lock: a same-key row still `creating` after 120 s is re-driven with its own Stripe idempotency key (the F4 mechanism), then moved to `created` (200 with `client_secret`) or `failed` (502). Without this, a crashed first request would leave the client polling a row that never moves.
   2. `invoice = PortalInvoiceReader.get(scope, invoice_id)`, read fresh and never from the client.
   3. If the status is not in (`open`, `partial`, `overdue`), return 409 `INVOICE_NOT_PAYABLE`.
   4. If `amount < 100` or `amount > remaining_cents`, return 422 `PAYMENT_AMOUNT_INVALID` with `details.max_cents` (R6.4).
   5. `SELECT … FROM portal_payment_attempts WHERE tenant_id = :t AND invoice_id = :i AND status IN ('creating','created','pending') FOR UPDATE`:
      - `pending` gives 409 `PAYMENT_IN_PROGRESS` (R6.3).
      - `creating` younger than 120 s gives 409 `PAYMENT_IN_PROGRESS`.
      - `created`, or `creating` at least 120 s old, is **superseded** (FREEZE F4): call `connector.cancel_intent(pi_id)` (per-request key, F7). For a stale `creating` with no PI id, first re-drive `PaymentIntent.create` with the old Stripe idempotency key to obtain it. If Stripe refuses because the PI is `processing` or `succeeded`, set the old attempt to `pending`, commit, and return 409 `PAYMENT_IN_PROGRESS`. Otherwise set it to `canceled`.
   6. `INSERT` the new attempt with status `creating`, then commit.
4. Call Stripe, bounded by `asyncio.wait_for(..., 10)`: `connector.create_portal_ach_intent(amount_cents, idempotency_key=f"portal_pa_{attempt_id}", metadata={source: "runsheet_portal", tenant_id, customer_id, invoice_id, payment_attempt_id}, description=f"Invoice {invoice_number}")`. This is a new connector method that bypasses autocharge and the ceiling, because the payment is customer-initiated. It sets `payment_method_types=["us_bank_account"]` and `payment_method_options={"us_bank_account": {"verification_method": "automatic"}}`, then returns `{id, client_secret, status}`.

   **Per-request Stripe key (review H4, FREEZE F7).** The three new connector methods (`create_portal_ach_intent`, `cancel_intent`, `retrieve_intent`) never assign `stripe_sdk.api_key`. Each decrypts the tenant envelope, then passes the key as a per-request option inside the thread:

   ```python
   key = envelope["secret_key"]
   await asyncio.to_thread(lambda: stripe_sdk.PaymentIntent.create(
       **body, api_key=key, idempotency_key=f"portal_pa_{attempt_id}"))
   await asyncio.to_thread(lambda: stripe_sdk.PaymentIntent.cancel(pi_id, api_key=key))
   await asyncio.to_thread(lambda: stripe_sdk.PaymentIntent.retrieve(pi_id, api_key=key))
   ```

   The `api_key=` request option exists on every SDK version in the `>=10,<13` range, which is why this is chosen over `StripeClient` (whose service accessors changed within that range, and the SDK isn't installed locally to confirm). The existing connector methods keep their global assignment; changing them is out of scope (Backlog B6). A key is never logged.

   **Timeout (review N12).** `wait_for` stops waiting at 10 s but doesn't stop the thread. The orphaned SDK call is bounded by the SDK's own HTTP timeout (80 s default) with `max_network_retries` left at its default of 0. Changing `stripe.default_http_client` would also change the timeouts of the existing connector calls, so it isn't done. The orphan is harmless: its attempt is already `failed`, no client secret was issued, and an intent it creates is never confirmed and is superseded or expires at Stripe.
   - On success: update the attempt to `created` with `stripe_payment_intent_id`, and return 201 with `client_secret` and `connector.get_publishable_key()`.
   - On any Stripe error or timeout: set the attempt to `failed` with `failure_code = "provider_error"`, log ERROR `portal_payment_provider_error` with `payment_attempt_id` (no Stripe message), and return 502 `PAYMENT_PROVIDER_ERROR` (R6.6).

**Client secret handling.** It is returned only in this response, in a replay to the same user, and from `GET /payment-attempts/{id}` when the caller is the attempt's `actor_user_id` and the status is `created`. It is never logged and never persisted.

### 6.3 Webhook reconciliation (`portal/services/portal_payment_reconciler.py`)

`integrations/api/stripe_endpoints.receive_stripe_webhook` changes only between "signature verified" and "dispatch". It inspects `obj = event.data.object`:

- `event.type` starts with `payment_intent.` and `obj.metadata.source == "runsheet_portal"`: call `await _portal_payment_handler(tenant_id, event)`.
- `event.type in {"charge.refunded", "charge.dispute.created"}`: call the portal handler, which looks up `obj.payment_intent` and does nothing when it isn't a portal attempt.
- Anything else: the existing `connector.handle_webhook_event(event)`, unchanged (R6.12).

**Placement (review N3).** The portal branch sits after signature verification and **before**, not inside, the existing `try/except` around `handle_webhook_event` (`stripe_endpoints.py:562-583`) that turns any error into 200 `handler_error`. A DB error in the portal handler therefore propagates to a real 500 and Stripe retries. The two `charge.*` events reach the portal handler first; when it reports "not a portal attempt", the endpoint continues into the existing block unchanged.

`configure_stripe_endpoints` gains `portal_payment_handler: Optional[...] = None`. When it is `None`, portal events fall through to the existing path, which ignores them as `missing_reconciliation_id`.

`PortalPaymentReconciler.handle(path_tenant_id, event)` holds the attempt row lock for the whole handling (FREEZE F2). The attempt-row reads and writes share one transaction. `payment_service.ingest` does **not** join it: it opens and commits its own sessions (Payment write, ES index, `invoice_service.apply_payment`, then the Redis marker last). The row lock only serializes deliveries; it doesn't make `ingest` atomic with the attempt update (review M4).

1. `SELECT … WHERE payment_attempt_id = :meta_attempt FOR UPDATE`.
2. Verify all of these: the row exists, `row.tenant_id == path_tenant_id == metadata.tenant_id`, metadata `customer_id` and `invoice_id` equal the row's, and `row.stripe_payment_intent_id` is null or equals `obj.id`. Any mismatch: log WARN `portal_audit outcome=webhook_mismatch` (ids only), write nothing, and return `{handled: false}`. The endpoint answers 200 (R6.11).
3. Apply the transition. Terminal states never move back, except as noted below.

   | Event | From | To / action |
   |---|---|---|
   | `processing` | `creating`, `created` | `pending` (R6.7) |
   | `succeeded` | any except `succeeded` | First `existing = await payment_service.find_by_external_id(tenant_id=..., source="stripe", external_id=obj.id)`. If found, set `succeeded` with `payment_id = existing["payment_id"]` and skip `ingest` (an earlier delivery recorded the Payment but its attempt update rolled back). Otherwise call `payment_service.ingest(tenant_id, invoice_id, account_id, amount_cents=obj.amount_received or obj.amount, source="stripe", method="ach", external_id=obj.id, received_at=now, actor=f"portal:{actor_user_id}")`, then `succeeded` with `payment_id` (R6.8, R6.9) |
   | `payment_failed` | not `succeeded` | `failed`, with `failure_code = obj.last_payment_error.code` (R6.10) |
   | `canceled` | not `succeeded` | `canceled` (R6.10) |
   | any | `succeeded` | No-op. A duplicate delivery means at most one Payment, because `ingest` is also idempotent on `external_id`. |

   **Succeeded is authoritative.** Money has moved, so a `succeeded` event records the payment even when the local row says `failed` or `canceled`. That can happen after a supersede race. It logs WARN `portal_payment_late_success`.
4. If `ingest` raises an `AppException` (for example, the invoice was voided meanwhile), set `succeeded`, keep `payment_id` NULL and set `failure_code = f"apply_rejected:{code}"`. Log ERROR `portal_payment_unapplied`, and return handled. A Stripe retry wouldn't help, and staff resolve it manually (refund in Stripe or apply by hand). Any other exception rolls back and propagates, so the endpoint returns 500 and Stripe retries.
5. `charge.refunded` / `charge.dispute.created`: when the attempt is found by `stripe_payment_intent_id`, log WARN `portal_audit action=payment_refund_or_dispute`. No state changes (R6.15, PD14).

A second concurrent delivery waits on the row lock, then sees `succeeded` and does nothing. If the attempt update fails after `ingest` committed, the transaction rolls back to the pre-event state and Stripe retries; the retry finds the Payment through `find_by_external_id` and only links it. So a duplicate Payment needs both the lookup and `ingest`'s own dedupe (Redis marker, plus the Postgres unique when payments are write-authoritative) to miss, which the lookup rules out once the first Payment is readable. A `find_by_external_id` error propagates (500, Stripe retries); it is never read as "not found".

**Runbook note.** The tenant's Stripe webhook endpoint must subscribe to `payment_intent.processing`, `succeeded`, `payment_failed`, `canceled`, `charge.refunded` and `charge.dispute.created`. This goes in `docs/runbooks/` as part of implementation (O1).

### 6.4 UI flow (summary; detail in §10)

1. The pay page posts `create` with a fresh UUID `Idempotency-Key`, stored in `sessionStorage` per invoice until a terminal result. That makes a reload replay the same key.
2. It loads Stripe.js and mounts the Payment Element with `clientSecret`.
3. It runs `stripe.confirmPayment({elements, redirect: "if_required", confirmParams: {return_url: <origin>/portal/invoices/{id}?attempt={attempt_id}}})`.
4. It polls `GET /payment-attempts/{id}` every 3 s for at most 60 s, then shows the status label. ACH stays "Payment processing" for days, which is expected.

## 7. Tanks

- **`GET /api/portal/tanks`:**
  - Reads `PortalTankReader.list(scope)` (active only), then `PortalForecastReader.latest_by_tank(scope)`.
  - Makes one order query for next deliveries: `repo.search(tenant, customer_id, statuses=["placed","confirmed","scheduled","dispatched","in_transit"], size=200, sort="delivery_window_start:asc")`, grouped by `customer_tank_id`, first per tank (PD17).
- **Projection `PortalTank`:**

  ```
  customer_tank_id, label, product_code, capacity_gallons, current_level_gallons, percent_full (0–100, 1 dp),
  last_reading_at, reading_stale: bool,
  forecast: {runout_at, days_to_runout, generated_at} | null,
  next_delivery: {order_id, status_label, window_start, window_end} | null
  ```

  - `reading_stale` is `last_reading_at is None or now − last_reading_at > portal_stale_reading_days` (7). It is computed on the server so the UI has one source (R7.3).
  - `runout_at` is `timestamp + hours_to_runout_p50`, and `days_to_runout` is `floor(hours/24)`. There is no reorder point in forecast data, so the portal shows days to runout, not days to reorder (Deviation DV2).
  - The model excludes `location_lat/lon`, `k_factor`, `source_system` and the other internal fields.
- **`GET /tanks/{id}`** returns the same projection for one tank, and 404 when it is out of scope.
- **`GET /tanks/{id}/deliveries`:**
  - The tank must be in scope (404 otherwise, R7.5). Archived tanks are also 404, because the list only shows active tanks.
  - It queries `repo.search(tenant, customer_id, customer_tank_id, status="delivered", start_date=now−730 days, sort="created_at:desc", keyset, size ≤ 50)`.
  - Each item is `{order_id, delivered_at, delivered_gallons, product_code, ticket_number}` (R7.4).
  - The window uses order `created_at`, the indexed field. An order created more than 24 months ago but delivered later falls outside it, which is acceptable.

## 8. Audit logging and rate limiting

### 8.1 `portal_audit` (`portal/audit.py`)

`emit_portal_audit(*, level, tenant_id, actor_user_id, customer_id, action, target_ids: dict[str,str], outcome, request_id, **extra_ids)` writes one log record through `logging.getLogger("portal_audit")`:

- The message is `"portal_audit"`, and `extra={"extra_data": {"portal_audit": True, ...fields, "timestamp": iso_utc}}`, the same structured-log convention as `csv_export` and the driver audit.
- Field values are restricted to ids, enums and the path template. A unit test asserts the field-name allowlist and that no value contains `@` or whitespace, which rules out email and free text (R8.2).

**`PortalAuditMiddleware`** (a `BaseHTTPMiddleware`) applies to paths starting `/api/portal/` or matching `^/api/commerce/customers/[^/]+/portal-users`. After `call_next`:

- `action` is `request.scope["route"].name` (the endpoint function name) when it resolves, else `"unmatched"`.
- `target_ids` comes from `request.path_params`, keeping only `*_id` keys.
- `outcome` maps from status: 2xx `ok`, 401 `unauthenticated`, 403 `forbidden`, 404 `not_found`, 409 `conflict`, 422 `invalid`, 429 `rate_limited`, 5xx `error`.
- The identity fields come from the request-state stamps: `tenant_id` from `portal_tenant_id or tenant_id`, `actor_user_id` from `portal_user_id or auth_user_id` (§2.2, M5), and `customer_id` from `portal_customer_id`, else `null`.
- Level is INFO for 2xx, else WARN, and ERROR for 5xx.
- It sets `Cache-Control: no-store` on every matched response.

It is registered in `main.py` **immediately before** `_register_auth_enforcement`, which puts it innermost: inside the auth gate and inside RequestID, so `request_id` is present. Each matched request gets exactly one line (R8.1). Handlers don't log `portal_audit` themselves, except for the extra domain events listed below.

Extra lines for events that have no request of their own:

- the central-deny WARN (§2.2: HTTP from the middleware, WS from `_resolve_ws_claims`)
- `webhook_mismatch`, `portal_payment_late_success`, `portal_payment_unapplied` and `payment_refund_or_dispute` from the reconciler

The durable records are the order events (with actor), `portal_payment_attempts.actor_user_id`, and `portal_user_grants.created_by` / `revoked_by` (R8.4, R8.5).

### 8.2 Rate limits (`portal/api/_authz.py`)

- `portal_rate_key(request)` returns `f"portal:{tenant}:{user}"` from the guard stamps, else `get_client_ip(request)`, matching `export_rate_key`.
- Limit strings are built from settings at import, like `EXPORT_RATE_LIMIT`.
- Reads are decorated with `@limiter.shared_limit(PORTAL_READ_LIMIT, scope="portal_read", key_func=portal_rate_key)`, so all read routes share one 120/min bucket per user.
- Each write route uses `@limiter.limit(<its limit>, key_func=portal_rate_key)`.
- Downloads use `@limiter.limit(EXPORT_RATE_LIMIT, key_func=portal_rate_key)`.
- The existing `RateLimitExceeded` handler returns 429 with `Retry-After` (R9.2). The per-IP default limits still apply.

Settings, added to `config/settings.py`:

| Setting | Default |
|---|---|
| `customer_portal_enabled` | `False`. Staging sets `{"name": "CUSTOMER_PORTAL_ENABLED", "value": "true"}` in the task env of `scripts/staging_aws.sh`, next to `COMMERCE_BACKBONE_ENABLED` (about line 1592). `.env.staging` is gitignored and isn't touched. Effective only with `commerce_backbone_enabled` (§2.3). |
| `portal_read_rate_limit` | `120` |
| `portal_order_rate_limit` | `10` |
| `portal_order_cancel_rate_limit` | `10` |
| `portal_payment_rate_limit` | `5` |
| `portal_max_users_per_customer` | `10` |
| `portal_principal_cache_seconds` | `60` |
| `portal_stale_reading_days` | `7` |

All ints are `ge=1`.

Test T-RL-COVERAGE asserts that every `/api/portal/*` route function appears in `limiter._route_limits` or in a shared-limit scope, and that its signature has a `request: Request` parameter (slowapi needs it), so a new route can't ship unlimited.

## 9. Migration (single head)

The new file is `alembic/versions/20261008_0001_customer_portal.py`, with `revision = "0011_customer_portal"` and `down_revision = "0010_acct_override_audit"`.

`upgrade()`:

1. Add the `auth_users.customer_id` column, the `ck_auth_users_customer_binding` constraint and the partial index. Existing rows all satisfy the first branch, because no row holds `customer` today.
2. Create `portal_user_grants` and its indexes.
3. Create `portal_payment_attempts`, its constraints and indexes.

`downgrade()` drops them in reverse order.

ORM mirrors `PortalUserGrantORM` and `PortalPaymentAttemptORM` go in `persistence/models.py`, so the existing ORM-vs-migration parity tests see them.

**Single-head rule.** `production-readiness/margin-feed` is being specced in parallel and may add a revision on `0010` too. Before merging, the implementer runs `alembic heads` on the rebased branch. If another `0011_*` already landed, change this file's `down_revision` to that head and bump the id to the next number. Never create a merge revision. CI's head-count step enforces this. `margin-feed`'s design also claims `0011_margin_feed` on `0010` with a `20261008_0001_*` filename (review N10). Whichever branch merges second re-parents, renames its file to the next free `2026MMDD_000N` slot, and names the new `down_revision` in its PR description.

## 10. Frontend

### 10.1 Routing and role awareness

- `config/modules.ts`: `Role` gains `"customer"`. `canSee` and `hasAnyRole`-based module visibility return `false` for every module when `roles` includes `customer`, so customers see no staff modules. That includes modules with no `requiredRoles`.
- `components/AudienceGuard.tsx` (new, client) is mounted once in `app/layout.tsx` around `{children}` inside `SuperTokensProvider`.
  - On pathname change, when a session exists, it reads `getCurrentUserRoles()`.
  - If the roles include `customer` and the path starts with `/dashboard`, `/admin`, `/commerce`, `/compliance`, `/ops` or `/orders`, it calls `router.replace("/portal")`.
  - If the roles don't include `customer` and the path starts with `/portal`, it calls `router.replace("/dashboard")`.
  - While a session exists, the path has one of the staff prefixes above or `/portal`, and roles haven't resolved yet, it renders `null` (review N9). That stops a customer from briefly mounting the staff shell and opening refused WebSockets. `getCurrentUserRoles` reads the local access-token payload, so the delay is a microtask. Other paths, signed-out visitors, and resolved roles render `children` as before. The guard only routes; the backend enforces.
- `app/signin/page.tsx`: after `OK`, it routes with `router.replace((await getCurrentUserRoles()).includes("customer") ? "/portal" : "/dashboard")`.

### 10.2 Portal route group (`runsheet/src/app/portal/`)

```
portal/layout.tsx              client; session gate → /signin; non-customer → /dashboard; PortalShell
portal/page.tsx                Overview: account name, capability notices, tank summary, open balance, recent requests
portal/orders/page.tsx         list + "Request delivery" link
portal/orders/new/page.tsx     request form
portal/invoices/page.tsx       list, filters, "Download CSV"
portal/invoices/[invoiceId]/page.tsx       detail, "Download PDF", "Pay"
portal/invoices/[invoiceId]/pay/page.tsx   ACH payment
portal/tanks/page.tsx          tank cards
portal/tanks/[tankId]/page.tsx tank detail + delivery history
```

Each segment has a `layout.tsx` with `metadata.title`, following the existing pattern.

`components/portal/` holds `PortalShell`, `PortalNav`, `StatusText`, `LiveRegion`, `OrderRequestForm`, `InvoiceTable`, `PaymentForm`, `TankCard` and `DeliveryHistoryTable`. `PortalShell` renders:

- a skip link (`<a href="#main">Skip to content</a>`)
- `<header>` with the supplier and customer names, and Sign out (`utils/auth.signOut`, then `/signin`)
- `<nav aria-label="Portal">` with `aria-current="page"`
- `<main id="main" tabIndex={-1}>`

It imports nothing from `Sidebar`, `Header`, `AIChat`, `GlobalSearch`, `NotificationBell`, `ws`/WebSocket hooks or `dashboard/shell-context`. Test T-UI-SHELL enforces this with a Jest module-graph check: it mocks those modules to throw on import, then renders the portal layout.

`services/portalApi.ts` uses `fetchWithSession` and `apiErrorFromResponse`, like `commerceApi.ts`. Downloads use the `exportApi.ts` blob pattern. It exposes typed functions for every §3 route and no WebSocket.

**Behavior that maps to requirements:**

- **Order form (R4.6, R4.7):**
  - Every input has a `<label>`. Errors use `aria-describedby` and are summarized in an `role="alert"` region on submit, with focus moved to the summary.
  - Tank is a select of the customer's tanks, showing the label, product and capacity.
  - Quantity is a radio group (Fill to full / Gallons) with a number input.
  - The date is a native `type="date"` with `min` = today and `max` = today + 60 days, both in local time (PD9, F6). The server stays authoritative. The optional window uses two `type="time"` inputs.
  - When `ordering_available` is false, or a submit returns 409 `ORDER_INTAKE_DISABLED`, it shows the PD10 message in a `role="status"` live region, sets `aria-disabled="true"` on submit, and keeps all values. `aria-disabled` doesn't block activation, so the submit handler returns early while disabled (OID-4 asserts no second `fetch`). It doesn't retry, and `client_event_id` is kept, so a later manual retry is idempotent.
- **429 (R9.2):** "Too many requests. Try again in N seconds.", with N taken from `Retry-After`, shown in the same live region.
- **Pay page:**
  - It loads `https://js.stripe.com/v3` with `<Script strategy="afterInteractive">`. A minimal `types/stripe-js.d.ts` declares the subset used: `Stripe(pk)`, `elements({clientSecret})`, `create("payment")`, `mount`, `confirmPayment`.
  - It has a heading and an amount field (default remaining, `inputmode="decimal"`, validated $1.00 to remaining).
  - The Payment Element container is labelled by a visible heading "Bank account". The Element itself is Stripe's iframe, with its own accessibility.
  - When payments are unavailable it shows the PD15 text and no form (R6.5).
- **Tanks:** level is shown as text ("1,240 of 2,000 gal (62%)") plus a `<meter>` with `aria-label`. Stale is text, never color only. A missing forecast shows "Forecast not available yet" (R7.2).
- **Formatting (PD23):** `Intl.DateTimeFormat` local time with `timeZoneName: "short"`, dates as calendar dates, money from cents with `Intl.NumberFormat("en-US", {style: "currency", currency: "USD"})`, and volumes in `measurement_units.volume`.
- **Layout:** existing Tailwind tokens, single column below 640 px, tables become stacked definition lists at narrow widths, no fixed widths, and buttons and links at least 24×24 px (`min-h-6 min-w-6`, and actual primary controls are 40 px).

### 10.3 Staff UI additions (PD24)

- **`components/commerce/PortalAccessPanel.tsx`**, rendered on `CustomerDetailPage` only when `hasAnyRole(roles, ["admin"])`.
  - It calls `GET portal-users` and hides itself on 404 (flag off).
  - Invite takes an email. A successful invite shows the link in a read-only field with a Copy button.
  - Each row offers Resend and Revoke, with a confirm dialog using the existing `Modal`.
- **Orders list:** an "Awaiting confirmation" quick filter sends `status=on_hold&hold_reason=awaiting_dispatcher_confirmation`. The staff `GET /api/orders` gains an optional `hold_reason` query param passed through to `search`.
- **`OrderDetailView`:** for that hold reason, it shows Confirm (`release-hold` with `{notes: "confirmed"}`) and Decline (`cancel` with `{reason: "declined_by_dispatcher"}`) to `admin`/`dispatcher`. On a 409 `INVALID_STATUS_TRANSITION` it reloads the order and shows "This request changed. Reloaded the latest version."

### 10.4 CSP (`config/securityHeaders.ts`, PD25)

The policy stays Report-Only. Add:

- `script-src https://js.stripe.com`
- `frame-src https://js.stripe.com https://hooks.stripe.com https://*.stripe.com`
- `connect-src https://api.stripe.com https://*.stripe.com`

`securityHeaders.test.ts` asserts these are present.

### 10.5 Accessibility test dependency

`@axe-core/playwright` is pinned exactly at `4.13.0` in `devDependencies`, compatible with the installed `@playwright/test` 1.58.

## 11. Error codes

These are added to `errors/codes.py` (`ErrorCode` and `ERROR_CODE_STATUS_MAP`), with factory helpers in `errors/exceptions.py`:

| Code | HTTP | Raised by |
|---|---|---|
| `PORTAL_DISABLED` | 404 | E4 |
| `PORTAL_ROUTE_FORBIDDEN` | 403 | E1 |
| `PORTAL_IDENTITY_INVALID` | 403 | E2 |
| `PORTAL_ACCESS_SUSPENDED` | 403 | E6 |
| `PORTAL_UNAVAILABLE` | 503 | principal check store failure |
| `PORTAL_EMAIL_IN_USE` | 409 | invite |
| `PORTAL_USER_LIMIT_REACHED` | 409 | invite |
| `PORTAL_CUSTOMER_ARCHIVED` | 409 | invite |
| `ORDER_NOT_CANCELLABLE` | 409 | portal cancel |
| `ORDER_REQUEST_REJECTED` | 422 | portal submit (hook / adapter rejection) |
| `INVOICE_NOT_PAYABLE` | 409 | payment create |
| `PAYMENT_AMOUNT_INVALID` | 422 | payment create |
| `PAYMENT_IN_PROGRESS` | 409 | payment create |
| `PORTAL_PAYMENTS_UNAVAILABLE` | 409 | payment create |
| `PAYMENT_PROVIDER_ERROR` | 502 | payment create |

Reused codes: `RESOURCE_NOT_FOUND`, `INSUFFICIENT_ROLE`, `ORDER_INTAKE_DISABLED`, `IDEMPOTENCY_CONFLICT`, `MISSING_IDEMPOTENCY_KEY`, `INVOICING_DISABLED`, `VALIDATION_ERROR`, `RATE_LIMITED`, `INVALID_STATUS_TRANSITION`.

**Logging levels:**

- WARN: refusals (403/409 from E1, E2 and E6), hook rejections, webhook mismatches, refunds and disputes.
- ERROR: provider errors, unapplied payments, PDF failures, principal-store failures and archive-hook revoke failures.
- INFO: success audit lines.

Stripe messages, client secrets, emails and bank data are never logged.

## 12. Invariants and owners

| Invariant | Owner | Why that layer |
|---|---|---|
| A `customer` identity holds no other role, no `driver_id` and no PII flag, and has exactly one `customer_id` | Postgres CHECK `ck_auth_users_customer_binding` (with the provisioner guard as the second layer) | The only layer every writer passes through |
| Only the portal grant flow writes `customer_id` | `PortalAccessService` (sole SQL writer). The CHECK plus the §1.6 refusals stop other paths from producing a customer row. | |
| A customer session reaches only the allowlist | `AuthEnforcementMiddleware` (E1), plus `get_tenant_context` (E1b) and `_resolve_ws_claims` (E3) | Default-deny before routing |
| Portal data is scoped to (tenant, customer) | `scoped_readers` (E8). `PortalScope` refuses empty ids. | One module, testable by spying |
| At most one in-flight payment attempt per invoice | Advisory lock (F2) plus partial unique index `uq_ppa_inflight` | Serialize, with a DB backstop |
| At most one commerce Payment per PaymentIntent | The reconciler's `find_by_external_id` pre-check, then `payment_service.ingest`'s own dedupe (Redis marker, plus the unique `(tenant, source, external_id)` when authoritative). The attempt row lock serializes deliveries; `ingest` commits independently of it. | Reuses the existing mechanism; the pre-check closes the gap left by `ingest` committing on its own |
| A Portal_Request isn't loadable before confirmation | Pipeline step (i3) sets `on_hold` when nothing else held it; a hook-set hold also keeps it `on_hold`. `LOADABLE_ORDER_STATUSES` excludes `on_hold`. | Reuses the existing state machine |
| A hook-set hold (credit) is never replaced by the portal hold | Pipeline step (i3) only stamps when `status == "placed"` | In the pipeline, so it doesn't depend on hook registration order |
| A replayed `client_event_id` never rewrites an existing order | `PortalOrderService` step 6a pre-check, plus the pipeline's existing-id guard for `web_portal` | Two layers: the service answers replays; the pipeline guard covers concurrent first submits and any other caller |
| A revoked customer has no identity left | `PortalAccessService.revoke` deletes the SuperTokens user and the portal-only `auth_users` row | No role-less session can exist for an outsider |
| A portal Stripe call uses its own tenant's key | The three new connector methods pass `api_key=` per request and never write module state (F7) | The global key is shared across concurrent requests |
| Confirm and customer-cancel can't both win | `FuelOrderRepository.transition_if` (F3) | CAS at the store |
| The 10-user cap | Advisory lock per customer (F1) | Serialize |

## 13. Test matrix

**Harness rules:**

- Suites marked **[real-auth]** import `main.app`, with the ES module mocked as `tests/smoke/test_route_smoke.py` does. They set `auth_provider="supertokens"` through a settings override and install `configure_session_verifier(FakeVerifier(claims))` (and `configure_ws_session_verifier` for WS). They **never** use `override_auth`, because it bypasses E1 and replaces E1b.
- Data services are in-memory fakes wired through the existing `configure_*` seams.
- Suites marked **[pg]** run in `tests/postgres` (the CI Postgres job) against the migrated schema.

**Fixtures:**

- Tenants `T1 = "demo-tenant"` and `T2 = "qa-tenant-b"`.
- Customers `A`, `B` in T1, and `C` in T2. Each has one active tank, one archived tank, one invoice in each status (including `draft`), one order on each of two channels, one forecast and one payment attempt.
- Staff sessions for each role in `STAFF_ROLES` per tenant. Customer sessions `cA`, `cB` and `cC`.

### 13.1 Required matrix (the five areas named for the coder)

| ID | Area | Test | Assertion |
|---|---|---|---|
| ISO-C-1 | Per-customer isolation | `test_portal_isolation.py::test_customer_a_cannot_read_b` [real-auth] | Session `cA` gets 404 `RESOURCE_NOT_FOUND`, with a body byte-identical (except `request_id`) to a random unknown id, for: B's invoice, invoice PDF, order, order cancel, tank, tank history, payment create on B's invoice, and B's payment attempt (AC4) |
| ISO-C-2 | Per-customer isolation | `…::test_lists_contain_only_own_rows` | `cA`'s invoice list, CSV (parsed), order list, tank list and next-delivery data contain only A's ids. B fixtures exist in the fakes. |
| ISO-C-3 | Per-customer isolation | `test_scoped_readers.py::test_store_calls_carry_customer_id` (T-SCOPE-ARGS) | Spy fakes record that every `InvoiceService.list/count`, `FuelOrderRepository.search`, `CustomerTankRepository.list_for_tenant` and forecast query call carries `customer_id == scope.customer_id` (non-empty) and `tenant_id == scope.tenant_id` |
| ISO-C-4 | Per-customer isolation | `…::test_portal_scope_rejects_empty_ids` | `PortalScope(customer_id="")` raises. A claimless customer session gets 403 `PORTAL_IDENTITY_INVALID` on `/api/portal/me` and on a staff route. |
| ISO-C-5 | Per-customer isolation | `test_portal_scope_import_lint.py` (T-SCOPE-IMPORT) | No module in `portal/api/` imports `InvoiceService`, `FuelOrderRepository`, `CustomerTankRepository` or `ElasticsearchService`, except through `portal.services.scoped_readers` (AST scan) |
| ISO-C-6 | Per-customer isolation | `test_portal_projection.py` | A fixture invoice with full `delivery_result` (driver id, recipient, photos, geotag, signature) and an order with all restricted fields: serialized portal responses (list, detail, PDF text via pypdf, CSV) contain none of the restricted keys or values (AC6) |
| ISO-T-1 | Per-tenant isolation | `test_portal_isolation.py::test_tenant_boundary` [real-auth] | `cA` (T1) gets 404 for C's (T2) ids on every id route in ISO-C-1. `cC`'s lists contain no T1 rows. |
| ISO-T-2 | Per-tenant isolation | `…::test_scope_params_rejected` | `?tenant_id=T2` and `?customer_id=C` on every portal GET give 422. A body with `tenant_id`/`customer_id` on every POST gives 422. No store call is made (spy). (AC5) |
| ISO-T-3 | Per-tenant isolation | `test_portal_isolation.py::test_webhook_tenant_mismatch` | A webhook on `/webhooks/stripe/T2` whose metadata names a T1 attempt gives 200, `handled=false`, no state change and a WARN audit line |
| ISO-T-4 | Per-tenant isolation | `tests/postgres/test_portal_schema.py::test_attempt_queries_scoped` [pg] | `PortalPaymentAttemptStore.get(scope_T1, id_of_T2_row)` returns 404. The grants list for T1/A returns no T2 rows. |
| ISO-T-5 | Per-tenant isolation | `test_portal_access_service.py::test_invite_cross_tenant_customer_404` and `::test_invite_email_of_other_tenant_409` | AC7 (cross-tenant parts). No SuperTokens write happens (admin fake records no calls). |
| XR-1 | Cross-role deny, customer to staff | `test_portal_central_deny.py::test_route_inventory_customer_forbidden` [real-auth] | Iterate every `APIRoute` in `main.app.routes` × its methods, filling path params with `x`. For every path not public and not in the allowlist, `cA` gets 403 `PORTAL_ROUTE_FORBIDDEN`, and the handler spy is never called (AC1). |
| XR-2 | Cross-role deny, customer to staff | `…::test_new_route_is_denied_by_default` | Add a throwaway `APIRoute("/api/zz-new", ...)` to a copy of the app. `cA` gets 403. |
| XR-3 | Cross-role deny, customer to staff | `…::test_allowlist_still_works` | `cA` gets 200 on `/api/auth/account/me`, `/api/portal/me`, and `/health`; `/auth/session/refresh` is not blocked by E1 |
| XR-4 | Cross-role deny, customer to staff | `test_portal_ws_deny.py` | For every `WebSocketRoute` in `main.app.routes`, a `cA` handshake closes with 4001 before accept, and a WARN audit line is emitted (AC2). Staff sessions still connect (regression). |
| XR-5 | Cross-role deny, customer to staff | `…::test_mixed_role_session_refused` | Claims `roles=["customer","admin"]` give 403 `PORTAL_IDENTITY_INVALID` on a staff route and on a portal route (R2.2) |
| XR-6 | Cross-role deny, staff to portal | `test_portal_staff_deny.py::test_staff_roles_forbidden_on_portal` [real-auth] | For every route under `/api/portal/` × each role in `STAFF_ROLES` (single-role sessions plus the `PLATFORM_STAFF_ROLES` bundle): 403 `INSUFFICIENT_ROLE` with the flag on (AC3) |
| XR-7 | Cross-role deny, staff to portal | `…::test_portal_admin_routes_admin_only` | Portal-user admin routes: `dispatcher`, `driver` and `platform_admin` alone get 403. A `customer` gets 403 `PORTAL_ROUTE_FORBIDDEN` (E1). `admin` gets 200. |
| XR-8 | Cross-role deny, both directions | `test_portal_flag_off.py` [real-auth], parametrized over (portal off, backbone on) and (portal on, backbone off) | In both cases every `/api/portal/*` route returns 404 `PORTAL_DISABLED` for `cA` and for every staff role. Every portal-admin route returns 404 for every staff role, and 403 `PORTAL_ROUTE_FORBIDDEN` for `cA`, because the central deny comes before the flag (DV7). The backbone-off case also asserts the settings WARN was logged. (AC8) |
| XR-9 | Cross-role deny, revoked customer | `test_portal_isolation.py::test_revoked_customer_has_no_session` [real-auth] | After revoke: the admin fake records `delete_user(st_user_id)`, the `auth_users` row is gone, and a session whose claims come from `_lookup_auth_user_claims` for that user has no `tenant_id`, so it gets 401 on `/api/fuel/mvp/forecasts` and on `/api/portal/me` (both resolve `get_tenant_context`). A pre-revoke session (claims still cached) gets 403 `PORTAL_ACCESS_SUSPENDED` on `/api/portal/me`. Re-invite of the same email provisions a new SuperTokens user without `ProvisioningConflictError`. |
| PAY-1 | Payment idempotency | `test_portal_payments.py::test_same_key_one_attempt_one_intent`, plus a [pg] concurrent variant in `tests/postgres/test_portal_schema.py::test_concurrent_same_key` | Sequential: two creates with the same key give one row and one `create_portal_ach_intent` call; the second response is 200 with the same id; the Stripe idempotency key is `portal_pa_<attempt_id>`. Concurrent [pg]: two same-key creates in parallel (fake Stripe sleeps inside create) give one row, one Stripe call, and both responses carry the same `payment_attempt_id` (one 201, one 200; the 200 may be the `creating` replay with `Retry-After: 2` and no `client_secret`). A same-key replay of a `creating` row older than 120 s re-drives with the same Stripe key and returns `created`. |
| PAY-2 | Payment idempotency | `…::test_same_key_different_amount_conflict` | 409 `IDEMPOTENCY_CONFLICT`, and no new Stripe call |
| PAY-3 | Payment idempotency | `…::test_second_key_while_pending_409` | An existing `pending` attempt makes a new key return 409 `PAYMENT_IN_PROGRESS` |
| PAY-4 | Payment idempotency | `…::test_second_key_supersedes_created` | An existing `created` attempt is canceled at Stripe (fake records `cancel`), the old row goes to `canceled` and the new attempt is created. When the fake's cancel raises "processing", the old row goes to `pending` and the response is 409. |
| PAY-5 | Payment idempotency | `tests/postgres/test_portal_schema.py::test_concurrent_creates_serialized` [pg] | 10 concurrent creates with distinct keys on one invoice: exactly one `creating`/`created` row. The others get 409. The partial unique index holds. |
| PAY-6 | Payment idempotency | `test_portal_stripe_webhook.py::test_duplicate_succeeded_one_payment` | The same `payment_intent.succeeded` event delivered twice (sequentially, and concurrently in the [pg] variant) gives exactly one `payment_service.ingest` side effect (one Payment row in the fake store). The attempt is `succeeded`. (AC11) |
| PAY-6c | Payment idempotency | `…::test_attempt_update_fails_after_ingest` | `ingest` succeeds (fake store has the Payment), then the attempt update raises: the endpoint returns 500. Redeliver with the Redis-marker fake cleared and payments non-authoritative: `ingest` is not called again, exactly one Payment exists, and the attempt ends `succeeded` with that `payment_id`. |
| PAY-7 | Payment idempotency | `…::test_amount_validation` | `amount_cents` of 99 or `remaining+1` gives 422; a `paid` or `void` invoice gives 409; a `draft` invoice gives 404 (DV8); nothing is created |
| PAY-8 | Payment idempotency | `…::test_webhook_transitions` | `processing` gives `pending`. `succeeded` gives a recorded Payment and the invoice moves to `partial`/`paid` in the fake. `payment_failed`/`canceled` give no Payment. `succeeded` after `canceled` records the payment (late success). An `AppException` from ingest gives `succeeded` with `apply_rejected`. A DB error gives 500. (AC12) |
| PAY-9 | Payment idempotency | `…::test_reconciliation_path_unchanged` | An event with `reconciliation_id` and no portal source still reaches `connector.handle_webhook_event`. The existing `test_stripe_endpoints.py` and `test_stripe_connector.py` pass unmodified. |
| PAY-10 | Payment idempotency | `…::test_no_stripe_409`, plus `test_portal_stripe_factory.py` | Portal factory returns `None`: create returns 409 `PORTAL_PAYMENTS_UNAVAILABLE`, and `/me` returns `payments_available=false` (AC13). Factory unit cases: only a disabled instance gives `None` (while the webhook factory still returns a connector for it); a disabled plus an enabled instance gives the enabled one; a repository error gives `None` and a WARN. |
| PAY-11 | Payment idempotency | `…::test_provider_error_502` | The fake raises or times out: attempt `failed` and 502 `PAYMENT_PROVIDER_ERROR`. The body has no Stripe text. |
| PAY-12 | Payment idempotency (tenant key) | `test_stripe_connector_portal.py::test_concurrent_tenants_use_own_key` | A fake `stripe` module whose `PaymentIntent.create/cancel/retrieve` record the `api_key` kwarg and sleep 50 ms. Concurrent T1 and T2 calls of all three methods each receive their own tenant's key, and a sentinel on the module-level `api_key` is never overwritten. |
| OID-1 | Order intake disabled | `test_portal_orders.py::test_intake_disabled_409` | Overlay state `disabled`: `POST /api/portal/orders` gives 409 `ORDER_INTAKE_DISABLED`, the pipeline repo spy records no write, and `GET /me` gives `ordering_available=false` (AC10) |
| OID-2 | Order intake disabled | `…::test_flag_flips_mid_request` | State is `shadow` at step 1 and `disabled` inside the pipeline (`legacy_passthrough`): 409 `ORDER_INTAKE_DISABLED` |
| OID-3 | Order intake disabled | `…::test_shadow_and_active_write` | `shadow`, `active_gated` and `active_auto` each create an order with `status=on_hold`, `hold_reason=awaiting_dispatcher_confirmation`, `intake_channel=web_portal`, product from the tank, and lat/lon from the tank |
| OID-4 | Order intake disabled | `runsheet/src/components/portal/__tests__/OrderRequestForm.test.tsx::shows_message_keeps_values_no_retry` | The mocked 409 shows the PD10 text in `role="status"`, inputs keep their values, the submit is `aria-disabled`, and `fetch` is called exactly once |

### 13.2 Remaining coverage

| ID | Test | Covers |
|---|---|---|
| ORD-1 | `test_portal_orders.py::test_duplicate_client_event_id` | Same `client_event_id` gives 200 with the same `order_id` and one order. Another user with the same UUID gets a different `order_id` (R4.2). |
| ORD-1b | `…::test_replay_after_marker_loss_does_not_rewrite` | Create, then a dispatcher releases the order to `placed`. Clear the idempotency fake and resubmit the same `client_event_id`: 200 with the same `order_id`, no pipeline call (spy), and the stored status is still `placed`. Pipeline unit case: `ingest_portal` for an id that already exists returns `duplicate`, runs no hooks and makes no upsert. |
| ORD-2 | `…::test_tank_out_of_scope_404`, `::test_pd9_validation_422` (parametrized over every PD9 rule) | AC9 |
| ORD-3 | `test_portal_loadable_exclusion.py` (T-ORD-LOADABLE) | R4.9, AC9 |
| ORD-4 | `test_order_release_hold_portal.py` | (a) A credit-passing portal order lands as `on_hold` / `awaiting_dispatcher_confirmation`, and `release-hold` moves it to `placed` with no re-hold. (b) A credit-failing portal order lands as `on_hold` / `credit_limit_exceeded`, not the portal reason; the portal shows "On hold" with `cancellable=false`. (c) Hook registration order doesn't matter: (a) and (b) pass with the credit hook registered first and last among the pipeline's hooks. (d) CAS: when the order was cancelled between read and write, the response is 409 `INVALID_STATUS_TRANSITION` and the order stays `cancelled` (F3). |
| ORD-5 | `test_portal_orders.py::test_cancel_rules` | Awaiting gives `cancelled`, with the actor on the event. Any other state gives 409 `ORDER_NOT_CANCELLABLE`. |
| ORD-6 | `…::test_hook_rejection_generic_422`, parametrized over `AppException`, `PricingError` and `DyedDieselOrderRejected` raised from a `before_accept` hook | 422 `ORDER_REQUEST_REJECTED` with the fixed message, no order written, a WARN with the exception class name, and neither the class name nor the hook code in the body |
| ORD-7 | `test_projection.py::test_order_status_map_total` (T-ORD-MAP) | PD11 |
| INV-1 | `test_portal_invoices.py` | Drafts never listed and 404 by id. Void listed as "Void". PDF is `application/pdf`, filename sanitized, pypdf text contains only projected values. CSV has BOM, `=` escaped, scope rows only, cap enforced. (AC14) |
| TNK-1 | `test_portal_tanks.py` | Active tanks only, null forecast, stale at 7 d + 1 s and fresh at 7 d − 1 s, next delivery picks the earliest window, history is 24 months and paginated, foreign tank 404 (AC15) |
| PRV-1 | `test_portal_access_service.py` | Admin only; email-in-use for staff, driver and other-customer rows with one identical message; 10-user cap (11th gets 409); revoke deletes the portal-only `auth_users` row (a staff row with the same email in another tenant is untouched), calls `set_user_roles([])`, `revoke_all_sessions` and `delete_user`, treats an already-deleted SuperTokens user as success, rolls back on a SuperTokens error, and the next request on an old session gets 403 `PORTAL_ACCESS_SUSPENDED` after cache invalidation; resend; link-mint failure keeps the grant; compensation on provisioner failure (AC7) |
| PRV-2 | `tests/postgres/test_portal_schema.py::test_customer_binding_check` [pg] | Inserting `roles={customer,admin}`, or `customer` with `driver_id`, or `customer_id` with `roles={dispatcher}`, raises `CheckViolation` |
| PRV-3 | `test_app_access_refuses_customer.py` | The driver grant on a customer email gives 409 `APP_ACCESS_ALREADY_LINKED` and no SuperTokens write |
| PRV-4 | `test_supertokens_claims.py::test_customer_id_claim` | `_lookup_auth_user_claims` emits `customer_id` only when it is set |
| PRV-5 | `test_portal_principal.py` | Archived customer gives 403 `PORTAL_ACCESS_SUSPENDED`. Cache TTL is respected (frozen clock). A cached `ok` for (tenant, user, A) is not reused for (tenant, user, B). Store error gives 503 and is not cached. `first_seen_at` is set once. |
| AUD-1 | `test_portal_audit.py` | Every portal route and admin action gives exactly one line with the PD18 fields, the field-name allowlist, and no `@` or spaces in values (AC16). Central-deny HTTP and WS refusals give WARN lines. A staff 403 `INSUFFICIENT_ROLE` and a 404 `PORTAL_DISABLED` on a portal route both log a non-null `actor_user_id` and `customer_id = null`. |
| RL-1 | `test_portal_rate_limits.py` | Each limit's 429 has `Retry-After`. User 1 exhausting the limit doesn't affect user 2. T-RL-COVERAGE (AC17). |
| MIG-1 | `tests/postgres/test_migrations.py` (existing) plus CI head-count | Upgrade, downgrade, upgrade. Single head. |
| UI-1 | Jest: `AudienceGuard.test.tsx`, `signin/page.test.tsx` (extended), `modules.test.ts` (extended) | Redirects both ways. Customer sign-in lands on `/portal`. `canSee` is false for customers. |
| UI-2 | Jest: `portal/layout.test.tsx` (T-UI-SHELL) | No staff shell modules imported or rendered, and no WebSocket constructed (`global.WebSocket` spy) |
| UI-3 | Jest: form, pay, tank and invoice component tests | Labels, `aria-describedby`, live regions, the PD15 message, stale and no-forecast text |
| UI-4 | Jest: `PortalAccessPanel.test.tsx`, `OrderDetailView.test.tsx` (extended) | Admin-only panel, hidden on 404. Confirm and decline call the right routes. 409 reloads. |
| UI-5 | Jest: `securityHeaders.test.ts` (extended) | Stripe origins present, still Report-Only |
| E2E-1 | Playwright `e2e/portal/*.spec.ts` (FREEZE F5) | Axe scan of every portal page gives zero serious or critical violations. Keyboard-only sign-in, new request, invoice PDF download. 320 px reflow (`scrollWidth <= clientWidth`). (AC18) |

Existing suites must pass unchanged (AC19). `test_tenant_scope_authz.py` and any test that pins `CANONICAL_ROLES` are updated only to add `customer`. A test asserting `customer ∉ CUSTOMER_ASSIGNABLE_ROLES` is added.

## 14. Files

**New, backend:**

- `portal/__init__.py`
- `portal/scope.py`
- `portal/audit.py`
- `portal/models.py`
- `portal/api/{__init__,_authz,me_endpoints,order_endpoints,invoice_endpoints,payment_endpoints,tank_endpoints,admin_endpoints}.py`
- `portal/services/{scoped_readers,projection,portal_access_service,portal_order_service,portal_invoice_service,invoice_pdf,portal_payment_service,portal_payment_reconciler,principal}.py`
- `fuel/intake/web_portal_adapter.py`
- `fuel/services/order_actions.py`
- `alembic/versions/20261008_0001_customer_portal.py`
- the tests in §13

**Modified, backend:**

- `auth/supertokens_init.py`, `auth/provisioner.py`, `auth/test_auth.py`
- `ops/middleware/tenant_guard.py`, `middleware/auth_enforcement.py`, `bootstrap/websockets.py`
- `fuel/api/driver_endpoints.py` (§1.6)
- `fuel/order_models.py` (constant), `fuel/order_repository.py` (filters, `transition_if`), `fuel/services/order_intake_pipeline.py`, `fuel/api/order_endpoints.py` (cancel via `order_actions`, release-hold CAS, `hold_reason` list filter)
- `bootstrap/fuel.py` (adapter), `bootstrap/core.py` and `bootstrap/agents.py` (configure the portal services and pass `portal_payment_handler`)
- `commerce/services/invoice_service.py` and `persistence/read_repositories.py` (`statuses`), `commerce/api/customer_endpoints.py` (archive hook)
- `integrations/stripe_connector.py` (`create_portal_ach_intent`, `cancel_intent`, `retrieve_intent`, all with per-request `api_key`, F7), `integrations/api/stripe_endpoints.py` (routing, placed before the existing try/except)
- `bootstrap/agents.py` also gains `_portal_stripe_connector_factory` and the `configure_portal_payments(...)` call (M2)
- `commerce/services/commerce_persistence_bridge.py` (`statuses` on `read_invoice_list` / `read_invoice_count`)
- `services/csv_export.py` (`ExportType`)
- `errors/codes.py`, `errors/exceptions.py`, `config/settings.py`, `persistence/models.py`
- `main.py` (routers and audit middleware)
- `.env.example` (`CUSTOMER_PORTAL_ENABLED=false`) and `scripts/staging_aws.sh` (task env `CUSTOMER_PORTAL_ENABLED=true`). `.env.staging` is gitignored and not changed.

**New, frontend:**

- `src/app/portal/**` (§10.2)
- `src/components/portal/*`, `src/components/AudienceGuard.tsx`, `src/components/commerce/PortalAccessPanel.tsx`
- `src/services/portalApi.ts`, `src/types/stripe-js.d.ts`
- `e2e/portal/*.spec.ts`

**Modified, frontend:**

- `src/app/layout.tsx`, `src/app/signin/page.tsx`
- `src/config/modules.ts`, `src/config/securityHeaders.ts`
- `src/components/commerce/CustomerDetailPage.tsx`, the orders list filter component, `src/components/orders/OrderDetailView.tsx`, `src/services/ordersApi.ts`
- `package.json` (one exact dev dependency)

**Docs:** `docs/runbooks/customer-portal.md`, covering the Stripe webhook events, the flag, inviting, the unapplied-payment procedure, and `alembic heads` before merge.

## 15. Decisions

### Design decisions (made here)

- **D1. Central deny in the middleware, with a second copy in `get_tenant_context`.** The alternative was a router-level dependency on all 56 modules. That is the exact per-route pattern PD4 rejects, and a new module would be open by default. The middleware already holds the verified session (memoized), so the check costs one list scan.
- **D2. A separate `portal_user_grants` table, rather than overloading `auth_users`.** `auth_users` must stay the claims source of truth with one current binding. Listing revoked users and counting the cap need history. A status column on `auth_users` would break the "revoked row has no customer binding" invariant the CHECK relies on.
- **D3. Portal staff routes live under `/api/commerce/customers/{id}/portal-users`.** That keeps every staff route outside the customer allowlist prefix by construction.
- **D4. Ship-to address** comes from the tank's latest order, otherwise a synthesized label. Coordinates always come from the tank. `CustomerTank` has no address field. Adding one would mean a data migration across ERP and CSV import sources, which is out of scope. Dispatchers see the order before confirming it.
- **D5. Deterministic portal `order_id` plus a namespaced idempotency key** answer R4.2 ("return the original result") with no new table. CSV import already uses the same deterministic-id technique.
- **D6. Stripe.js comes from the CDN, not npm.** Stripe requires loading Stripe.js from `js.stripe.com` anyway, and this keeps N3 (no new runtime frontend dependency).
- **D7. Audit is one middleware, not per-handler calls.** That guarantees exactly one line per request, including rate-limit and validation refusals that never reach the handler.
- **D8. The invoice date filter uses `created_at`,** the existing indexed range in both stores, while `issued_at` is displayed. Adding an `issued_at` range to both stores is backlog B2. This is also recorded as DV9.
- **D9. The portal hold stamp stays in the pipeline (step i3), not in a hook.** The review offered a `PortalReviewHoldHook` registered last. Hooks are registered from three bootstrap modules, so "last" would be an ordering convention nothing enforces. An inline guarded step is order-independent and has the same behavior.
- **D10. Revoke deletes the customer's SuperTokens user and `auth_users` row** instead of emptying them. History lives in `portal_user_grants`. Alternatives considered: teaching the central deny to refuse role-less sessions (would change behavior for existing role-less staff and driver rows, out of scope), or disabling the password (no SDK primitive for it in the pinned version).

### FREEZE decisions (review-loop convergence rule)

Each one removes a class of race or edge case instead of answering each case separately. Each lists the tests it requires.

- **F1. Serialize invites and revokes per customer** with `pg_advisory_xact_lock('portal-grants:<tenant>:<customer>')`. It removes cap-overshoot and invite/revoke interleavings. Tests: PRV-1 (cap), plus a [pg] concurrency variant of 11 concurrent invites that leaves exactly 10 active.
- **F2. Serialize payment work per invoice and per attempt.** Creates take `pg_advisory_xact_lock('portal-pay:<tenant>:<invoice>')`. Webhooks take `SELECT … FOR UPDATE` on the attempt row and run `ingest` under it. `succeeded` is authoritative over local state. This removes every double-attempt and double-Payment ordering. Tests: PAY-5, PAY-6 (sequential and [pg] concurrent), PAY-8 (late success).
- **F3. Portal_Request state changes are compare-and-set** through `FuelOrderRepository.transition_if` (portal cancel, staff release-hold final write, staff cancel through `order_actions`). This removes confirm-vs-cancel resurrection. Tests: ORD-4 (CAS 409), ORD-5.
- **F4. No background sweeper for abandoned payment attempts.** A `created` attempt is superseded by the next create, which cancels the old intent at Stripe first. A stale `creating` row (≥ 120 s) is re-driven with its own Stripe idempotency key and then superseded. If Stripe reports that money is moving, the result is 409. Tests: PAY-3, PAY-4.
- **F5. Playwright axe and keyboard tests run against a deployed environment** (staging, with `PLAYWRIGHT_BASE_URL` and the QA portal account from env), as part of the staging verification step (AC20), not in CI. Playwright doesn't run in CI today, and a real SuperTokens session can't be faked in the browser without a test-only auth bypass in the UI, which would be worse. CI covers the same behaviors at component level (UI-2, UI-3, OID-4). Tests: E2E-1, run and recorded in the task evidence file.
- **F7. Portal Stripe calls are tenant-keyed per request** (added in revision 1). The three new connector methods pass `api_key=` on every SDK call and never write `stripe_sdk.api_key`. No connector refactor, no new client abstraction, and the existing methods stay as they are (B6). This removes the cross-tenant Stripe class for every customer-triggered call. Tests: PAY-12.
- **F6. No tenant time zone exists.** The server validates instants with offsets: start ≥ now − 24 h, start ≤ now + 60 days, window ≤ 25 h. The browser turns local dates into instants. This removes a whole class of "today in which zone" bugs, and A3 resolves to this rule. Tests: ORD-2 boundary cases at −24 h ± 1 s and +60 d ± 1 s.

### Deviations from requirements (for design review)

- **DV1. R3.1 tenant display name.** No such field exists. `supplier_name` falls back to `tenant_id`, the BOL precedent (Backlog B1).
- **DV2. PD17 "days until reorder".** Forecast data has no reorder point, so the portal shows the projected runout date and days to runout.
- **DV3. R6.3 "non-terminal attempt gives 409"** applies to `creating` (< 120 s) and `pending`. A `created` attempt, where the customer never confirmed at Stripe and no money can move once it's canceled, is superseded instead (F4). Without this, a closed tab would block payment of that invoice with no recovery. Double payment is still impossible: the old intent is canceled at Stripe before a new one is created, and a refusal to cancel gives 409.
- **DV4. PD4 allowlist paths** are the real `GET /api/auth/account/me` and `POST /api/auth/account/change-password`. The `/api/auth/me` and `/api/auth/change-password` named in PD4 don't exist.
- **DV5. R6.14 last-4 and bank name** are not shown in v1. The requirement says "may", and storing them would need an extra Stripe expansion.
- **DV6. R8.1 audit for unauthenticated (401) portal requests** isn't written, because the auth gate answers before the portal layer and there is no actor. The existing auth logging covers these.

- **DV7. AC8, customer sessions on portal-admin routes.** These get 403 `PORTAL_ROUTE_FORBIDDEN` instead of 404 when the flag is off. The central deny runs in the middleware before any route-level flag check, and making it flag-aware would put a feature flag inside the isolation boundary. A customer learns nothing from this: they get the same 403 on every staff route.
- **DV8. R6.4, payment create on a `draft` invoice** returns 404 `RESOURCE_NOT_FOUND`, not 409. Drafts are invisible to the portal (R5.5, PD6), so a 409 would confirm the draft exists. Test: PAY-7.
- **DV9. R5.1 "issue-date range"** filters on invoice `created_at` (D8). `issued_at` is displayed. Backlog B2 adds a real `issued_at` range.

### Backlog

- **B1.** A tenant display name setting, for the portal header and the PDF.
- **B2.** An `issued_at` range filter in both invoice stores.
- **B3.** Showing the bank name and last-4 on attempts (expand `payment_method` on the webhook).
- **B4.** Pushing portal payments to QBO (PD16, out of scope).
- **B5.** A service address on `CustomerTank`, which would replace D4's fallback label.
- **B6.** Move the existing `StripeConnector` methods (`sync_push`, list calls at `stripe_connector.py:447,617,741`) off the process-global `stripe_sdk.api_key` to per-request keys, as F7 does for the portal methods. Those calls are admin- or agent-triggered today; the same race applies under concurrency.

## Review responses

### Revision 1 (responds to design review pass 1, verdict CHANGES_REQUESTED: 4 HIGH, 6 MEDIUM, 12 NIT)

All 22 findings are addressed. None is backlogged in place of a fix, and none is ignored. No FREEZE decision was reopened. One freeze was added (F7). The changes fix wrong assumptions about existing code. The design's scope is unchanged.

| ID | Response | Where |
|---|---|---|
| H1 | **Addressed.** Revoke deletes the SuperTokens user (`delete_user`, where an unknown user counts as success) and the portal-only `auth_users` row. The `DELETE` predicate `roles = ARRAY['customer']` keeps it from touching staff or driver rows. Grant history stays in `portal_user_grants`. A re-invite provisions a fresh user. Rationale is in D10. Tests: XR-9 (new) and PRV-1 (updated). | §1.7 Revoke, §12, D10 |
| H2 | **Addressed.** Step i3 stamps the portal hold only when `status == "placed"` after the hooks run, so a credit hold is never overwritten. The false "release-hold re-runs credit" sentence is removed. I kept the stamp inline instead of adding a `PortalReviewHoldHook`, because hooks are registered from three bootstrap modules and nothing enforces which runs last (D9). The `order_placed` event no longer claims a hold reason. ORD-4 is rewritten as cases (a)–(d), including hook-order independence. | §4.2, §12, ORD-4, D9 |
| H3 | **Addressed** with both guards: the service pre-check (step 6a, `get_or_none`, 200 replay with no pipeline call) and the pipeline existing-id guard for `web_portal` (returns `duplicate`, writes nothing, and propagates repo errors as 500). Test: ORD-1b. | §4.1 step 6a, §4.2, §12 |
| H4 | **Addressed** as FREEZE F7. The new methods pass `api_key=` per request inside the thread and never write module state. I chose `api_key=` over `StripeClient` because it works across the whole `>=10,<13` range. The existing methods are B6. Test: PAY-12. | §6.2 step 4, F7, B6, §14 |
| M1 | **Addressed.** The service catches `(AppException, PricingError, DyedDieselOrderRejected)` and maps them to 422 `ORDER_REQUEST_REJECTED`, with a WARN that names the class and code. ORD-6 is parametrized over all three. | §4.1 step 7 + table, ORD-6 |
| M2 | **Addressed.** `_portal_stripe_connector_factory` in `bootstrap/agents.py` returns enabled instances only. It is wired through `configure_portal_payments` and used by `/me`, create, replay and attempt polling. The webhook keeps the existing factory. PAY-10 is extended. | §3.1, §6.2 step 1, §14 |
| M3 | **Addressed.** `portal_enabled = customer_portal_enabled AND commerce_backbone_enabled` gates both guards. A settings validator logs WARN and does not raise. XR-8 is parametrized with the backbone off. | §1.7 gate, §2.1 E4, §2.3 step 1, §8.2, XR-8 |
| M4 | **Addressed within F2.** The reconciler calls `find_by_external_id` before `ingest`, and on a hit it only links the Payment. §6.3 and §12 now say `ingest` commits on its own and the row lock only serializes. Test: PAY-6c. | §6.3, §12 |
| M5 | **Addressed.** `_context_from_session_claims` stamps `request.state.auth_user_id`. The audit middleware falls back to it. AUD-1 is extended. | §2.2, §8.1, AUD-1 |
| M6 | **Addressed.** Step 3.1b repeats the same-key lookup under the advisory lock. A same-key `creating` row returns 200 with `Retry-After: 2` and no `client_secret`. One added rule: a same-key `creating` row older than 120 s is re-driven under the lock using the existing F4 mechanism, so the client never polls a dead row. PAY-1 gains a [pg] concurrent case. | §6.2 steps 2 and 3.1b, PAY-1 |
| N1 | **Addressed.** The staging value is set in `scripts/staging_aws.sh`. `.env.staging` is dropped from §14. | §8.2, §14 |
| N2 | **Addressed.** The wording now says the version is a range. | Tech stack |
| N3 | **Addressed.** The portal branch is placed explicitly before the existing try/except. | §6.3 Placement |
| N4 | **Addressed.** `statuses` is added to `read_invoice_list` / `read_invoice_count`. INV-1 covers both paths. | §2.4, §14 |
| N5 | **Addressed** as DV8. | §15, PAY-7 |
| N6 | **Addressed** as DV9 (cross-referenced from D8 and §3.2). | §15 |
| N7 | **Addressed.** Every handler declares `request: Request`, and T-RL-COVERAGE asserts it. | §3, §8.2 |
| N8 | **Addressed.** The cache is keyed on `(tenant, user, customer_id)`. Revoke invalidates every key for the user. PRV-5 is extended. | §2.3 step 4, PRV-5 |
| N9 | **Addressed.** `AudienceGuard` renders `null` on staff and portal prefixes until roles resolve. | §10.1 |
| N10 | **Addressed.** The branch that merges second re-parents, renames its file, and names the new `down_revision` in its PR. | §9 |
| N11 | **Addressed.** The handler no-ops while disabled. The date `min`/`max` are set per PD9/F6. | §10.2 |
| N12 | **Addressed, with a different mechanism.** I kept the SDK's default HTTP timeout, which is bounded, rather than setting `stripe.default_http_client`, because that global would also change the existing connector calls. The orphaned call is documented as harmless. | §6.2 step 4 |

**Convergence note.** This pass has no new design areas. Every change either corrects a wrong assumption about existing code or tightens an existing mechanism (F2, F4, the CSV stale-branch precedent). If a next pass keeps raising new MEDIUM findings in the payment create and replay flow, the planned simplification is to serialize same-key and same-invoice work completely under the F2 lock, including the Stripe call, and accept the added latency.

