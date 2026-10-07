# Requirements: Customer portal, v1 (OI-06)

## Summary

B2B fuel customers have no way to see or act on their own account. Product-owner audit 2026-05-08 #15 (High) and recommendation 9 call for a customer-facing portal. On 2026-10-07 the owner confirmed it is in go-live scope (open-issues register OI-06). Audit #24 / rec 10 (the COGS / margin feed) is the other half of OI-06. It is a separate spec (`margin-feed`) and is not covered here.

The portal lets an invited user of one commerce customer do four things, and only for that customer:

- (a) request fuel deliveries, which a dispatcher confirms
- (b) view and download their invoices
- (c) pay an invoice by ACH through Stripe
- (d) view their tanks: level, forecast and delivery history

Portal users are a new, exclusive `customer` role in the existing SuperTokens setup. They are bound to one `customer_id`, and they reach only a new `/api/portal/*` API and a new `/portal` route group in the existing Next.js app.

The owner delegated the detailed product choices. Every choice is recorded below under Decisions, with its reasoning. None of them needed a pause: each has a defensible default.

### Codebase grounding (worktree `.worktrees/customer-portal`, branch `production-readiness/customer-portal`, base `0f82def`)

| Area | Current state | Consequence for the portal |
|---|---|---|
| Roles | `auth/supertokens_init.py` `CANONICAL_ROLES` = `admin`, `dispatcher`, `driver`, `platform_admin`. `require_role` matches exactly and no role implies another (`auth/authorization.py`). Claims come from `auth_users` keyed on `st_user_id` (`_lookup_auth_user_claims`). `driver_id` is an optional claim with one writer (`fuel/api/driver_endpoints.py`, app-access grant). | Add `customer` and a `customer_id` claim the same way `driver_id` works. |
| Route gating | 56 backend modules resolve `Depends(get_tenant_context)`. Some routes have no role check at all, e.g. `GET /api/fuel/mvp/forecasts` (`Agents/support/mvp_endpoints.py:508`), which returns every tenant tank's forecast. There are 10 WebSocket endpoints, and two broadcast managers send to every tenant (OI-01). | Gating route by route can't keep a customer token out. Customer sessions must be denied centrally (R2). |
| Commerce authz | `commerce/api/_authz.py`: customers and invoices are `admin`/`dispatcher`. Payments, accounts and price books are `platform_admin`. Feature flags are global settings that answer 404 (`require_invoicing_enabled`). | Portal endpoints get their own role gate and flag, and keep flag-before-role ordering. |
| Commerce model | A `Customer` (`cust_<uuid>`, `active`/`archived`) has many `Account`s (`active`/`suspended`/`closed`). `Invoice` carries `customer_id`, `account_id`, a status (`draft`/`open`/`partial`/`paid`/`overdue`/`void`), cents fields, and `delivery_result` (driver id, recipient, photos, geotag). `Payment` statuses are `applied`/`reversed` only. `payment_service.ingest` is idempotent on `(tenant, source, external_id)`. | Scope by `customer_id`. Project invoices through an allowlist. Record payments only when settled. |
| Stripe | `integrations/stripe_connector.py` uses per-tenant vault credentials. It creates PaymentIntents (`_build_payment_intent_body`) and maps `payment_intent.{succeeded,payment_failed,processing,canceled}`. Webhook `POST /webhooks/stripe/{tenant_id}` verifies signatures but only updates Reconciliation_Records keyed by `metadata.reconciliation_id`. It never calls `payment_service`. No Stripe integration is configured on staging. | Portal payments need a new metadata route in the webhook into `payment_service`, and a "not configured" state. |
| Order intake | `POST /api/orders` → `OrderIntakePipeline`. The `web_portal` channel type is already reserved (`intake_channel_models.py:37`, `IntakeMetadata.portal_session_id`). When the `order_intake_pipeline` flag is `disabled`, the endpoint returns `legacy_passthrough` → 409 `ORDER_INTAKE_DISABLED`. `shadow` and `active_*` write normally. Statuses `placed`/`confirmed`/`scheduled` are `LOADABLE_ORDER_STATUSES`. `on_hold` needs a `hold_reason`, and `release-hold` re-runs hooks and moves the order to `placed`. Tank ownership is checked in `_verify_customer_tank`. | Portal orders come in on hold so the planner can't load them before a dispatcher confirms. |
| Tanks | `CustomerTank` has `customer_id`, `capacity_gallons`, `current_level_gallons`, `last_reading_at`, `status`, and a product. Forecasts are served by `/api/fuel/mvp/forecasts` (filters `customer_id`, `customer_tank_id`). | Reuse the repository and forecast reads, filtered to the caller's customer. |
| Export / PDF / limits | `services/csv_export.py` handles BOM, injection escaping, row cap, audit line and `export_rate_key`. `reportlab==4.5.0` is pinned (BOL PDFs). slowapi `limiter` in `middleware/rate_limiter.py`. | No new backend dependencies. |
| Frontend | Next.js 15.5 and React 19. `supertokens-auth-react`. `config/modules.ts` `Role` union. Staff shell under `app/dashboard`. Jest + Testing Library + Playwright. No axe tooling. CSP is Report-Only (OI-10). | New `app/portal` group, a `customer` role entry, and an a11y test dependency. |

## Glossary

- **Portal_User**: an `auth_users` row whose roles are exactly `["customer"]`, with a non-null `customer_id`.
- **Customer_Scope**: the pair (`tenant_id`, `customer_id`), taken only from the verified session claims, never from the request.
- **Portal_API**: every route under `/api/portal/`.
- **Staff_API**: every other authenticated HTTP route, and every WebSocket endpoint.
- **Staff_Role**: `admin`, `dispatcher`, `driver` or `platform_admin`.
- **Portal_Request**: a fuel order created through the Portal_API (`intake_channel = "web_portal"`).
- **Payment_Attempt**: the durable portal-side record of one ACH payment, from creation to a terminal Stripe outcome.

## Decisions

| ID | Decision | Rationale |
|---|---|---|
| PD1 | Add a new canonical role `customer`. `auth_users` gains a nullable `customer_id` column, and sessions carry a `customer_id` claim when it is set. A row holding `customer` holds no other role and no `driver_id`. `customer` is not added to `CUSTOMER_ASSIGNABLE_ROLES`, so it can only be granted through the portal-user grant flow (R1), which is the only writer of `auth_users.customer_id`. | This mirrors the driver app-access pattern. Keeping the role exclusive means no staff session can also carry Customer_Scope, and no customer session can hold a staff role. |
| PD2 | A Portal_User is bound to a commerce **Customer**, not an Account. The user sees every Account of that customer. | Tanks and orders carry only `customer_id`. Invoices carry both ids. Binding to an Account would hide the customer's own tanks and orders. |
| PD3 | Only a tenant `admin` invites, from the staff Customers page. There is no self-signup. The invite provisions the SuperTokens user and mints a password-set link with the existing OQ6 mechanism (`auth/password_admin.py`). The link is shown to the admin to hand off and is also emailed through the configured SuperTokens email delivery. Each customer can have at most 10 active Portal_Users. An admin can revoke access, which clears the binding and revokes all of that user's sessions. `dispatcher` can't invite. | This reuses the only credential-handoff path that works today, with no new email infrastructure. The admin-only gate matches the other identity-granting routes. 10 users covers a fleet's AP plus operations contacts. |
| PD4 | Customer sessions are denied **centrally**. Any request from a session holding `customer` to a route outside the portal allowlist gets 403 `PORTAL_ROUTE_FORBIDDEN`. The allowlist is the Portal_API, `GET /api/auth/me`, `POST /api/auth/change-password`, the SuperTokens recipe routes (`/auth/*`), and the public routes. Every WebSocket handshake from a customer session is refused. The deny lives in the shared auth layer (tenant guard / auth middleware), not in each router. | 56 modules depend on `get_tenant_context`, and some routes have no role check (for example the forecasts route). Gating route by route would leave a customer token able to read tenant-wide data. Default-deny is the only design where a route added later stays safe. WebSocket broadcasts are cross-tenant today (OI-01), so a customer must never hold a socket. |
| PD5 | Every Portal_API route requires the `customer` role (exact match). Any Staff_Role session gets 403 `INSUFFICIENT_ROLE`. There is no staff "view as customer" in v1. | Portal handlers derive scope from the `customer_id` claim, which staff don't have. Impersonation would need its own audit and consent design. |
| PD6 | When a Portal_API request names an id (invoice, order, tank, payment attempt) that is outside the Customer_Scope, or doesn't exist, the response is 404 `RESOURCE_NOT_FOUND`, never 403. | The response must not tell a caller that another customer's id exists. This matches `password_admin_endpoints` ("reported as not provisioned"). |
| PD7 | A global setting `customer_portal_enabled` (default `false`, `true` on staging) gates the portal. While it is off, every Portal_API route returns 404, the portal-user admin routes return 404, and the staff UI hides the portal panels. The invoice and payment sections also require the existing commerce backbone and invoicing flags. If those are off, the sections answer 404 and the UI hides them. | Commerce already uses global flags that answer 404 with flag-before-role ordering, so this adds no new per-tenant flag mechanism. |
| PD8 | A Portal_Request goes through `OrderIntakePipeline` on the `web_portal` channel. It is stored as `status = on_hold` with `hold_reason = "awaiting_dispatcher_confirmation"`. A dispatcher or admin confirms it with the existing `release-hold` route, which moves it to `placed`. The portal-review hold applies only at intake and is not re-applied when the hold is released. Other hooks (credit, pricing) still run as usual. Declining is the existing `cancel` route. The customer can cancel their own Portal_Request only while it is still `awaiting_dispatcher_confirmation`. | `placed` is a loadable status, so a request created as `placed` could be planned and loaded with no human review. Using the existing hold avoids a new status that every planner, loader, export and UI would need to learn. |
| PD9 | A Portal_Request must name one of the customer's own `active` tanks. Product = the tank's product, never chosen by the customer. Quantity is either `fill_to_full` or `gallons_requested` with 0 < gallons ≤ tank `capacity_gallons`. Requested delivery date: from today to today + 60 days in the tenant's time zone, with an optional window whose end is after its start. `po_number` is optional, at most 64 characters. `notes` are optional, at most 500 characters, and go into `special_instructions`. The request needs a client-generated `client_event_id` (UUID) for idempotency. Ship-to address and coordinates come from the tank, never from the request. | Binding the request to a tank reuses the existing ownership check, and the customer can't send fuel to an arbitrary address or pick the wrong product (dyed vs clear diesel, OI-02). The 60-day horizon covers scheduled fills without becoming a long-range planning surface. |
| PD10 | Ordering availability comes from `GET /api/portal/me` (`ordering_available`), which is `false` when `order_intake_pipeline` is `disabled` for the tenant. While it is `false` the form is disabled and shows a plain message. A 409 `ORDER_INTAKE_DISABLED` on submit (the flag flipped mid-session) keeps the user's input, shows the same message, and is not retried automatically. `shadow` and `active_*` count as available, because they write orders. | The flag is owned by another agent and is `disabled` on staging. Treating 409 as an expected state, not an error, keeps the portal usable while ordering is off. |
| PD11 | Customers see all of their own orders from every intake channel, mapped to customer-facing labels: Awaiting confirmation, Confirmed (`placed`/`confirmed`/`scheduled`), Out for delivery (`dispatched`/`in_transit`), Delivered, Not delivered (`failed`), Cancelled, and On hold (any other `hold_reason`, which is not shown). Driver, truck, run, plan and internal hold reasons are never shown. | Phone or EDI orders are still the customer's own deliveries, so hiding them would make the history look incomplete. Internal hold reasons can name credit status or compliance flags that are staff matters. |
| PD12 | Invoices are read-only. Customers see invoices with any status except `draft`; `void` is shown labelled "Void". Each invoice has an on-demand PDF (reportlab, no new dependency). The invoice list has a CSV download through the shared `services/csv_export.py` helper, which covers BOM, injection escaping, the 50,000-row cap, the audit line and the rate limit. Invoice data reaches the portal only through an allowlist projection: number, dates, status, account display name, line items (product, gallons, unit price, subtotal), tax, total, paid, remaining, and from `delivery_result` only `delivered_at`, `actual_gallons` and `ticket_number`. | Drafts aren't final and may change. Void invoices stay visible so a customer's statement matches their records. The allowlist is what keeps driver id, recipient name, photos and geotag out of the portal. |
| PD13 | ACH only, through Stripe `PaymentIntent` with `payment_method_types=["us_bank_account"]`, collected with Stripe.js Payment Element (Financial Connections, with microdeposit fallback). Stripe is in test mode on staging. One invoice per payment. The amount defaults to the invoice's `remaining_cents`; a partial amount from $1.00 to `remaining_cents` is allowed. The server reads `remaining_cents` itself and never trusts a client total. At most one non-terminal Payment_Attempt can exist per invoice. Bank accounts are not saved, and card payments are not offered. | ACH is what the audit asked for and avoids card fees on large fuel invoices. `Payment` is per invoice, so multi-invoice pay would need allocation rules. One in-flight attempt per invoice prevents double payment from two tabs. Saving bank accounts would add mandate and Stripe Customer lifecycle work, so it's deferred. |
| PD14 | A commerce `Payment` is recorded only on `payment_intent.succeeded`, via `payment_service.ingest(source="stripe", method="ach", external_id=<PaymentIntent id>)`. `processing` marks the attempt Pending. `payment_failed` and `canceled` make the attempt terminal with no commerce Payment. Overpayment from a race uses the existing credit-balance accrual. Later `charge.refunded` or `charge.dispute.created` events are audit-logged at WARN and are not reversed automatically. Staff reverse them through the existing reverse route. | `Payment` has no pending state, and ACH settles in days, so recording at submit time would mark unpaid invoices as paid. Automated dispute handling isn't needed for launch and would add another state machine. |
| PD15 | If the tenant has no Stripe integration configured, the portal shows "Online payment isn't available. Contact your supplier to pay." The payment-create route returns 409 `PORTAL_PAYMENTS_UNAVAILABLE`. | Staging has no Stripe instance today, and the portal must still work without one. |
| PD16 | Portal payments are not pushed to QuickBooks in v1. Tenants who use an ERP see the invoice move to `partial` or `paid` in Runsheet and reconcile the payment in their ERP. | The ERP owns AR (`_authz.py` D8). A QBO payment-push path is a separate integration. This is listed under Out of Scope. |
| PD17 | The tank view shows each of the customer's `active` tanks: product, capacity, current gallons and percent, last reading time, forecast runout date and days until reorder (from the existing forecast data), the next scheduled delivery (the customer's earliest order in a Confirmed or Out-for-delivery state for that tank), and delivery history for the last 24 months (date, gallons, product, ticket number). A reading older than 7 days is labelled "Reading may be out of date". Data loads when the page loads. Nothing is live-updated. | This reuses existing data with no WebSocket (PD4). Seven days marks an obviously stale monitor without alarming on normal daily readings. |
| PD18 | Every customer action and every staff portal-user admin action writes one structured `portal_audit` log line (JSON, INFO, or WARN for a refusal). The fields are: `tenant_id`, `actor_user_id`, `customer_id`, `action`, target ids, `outcome`, `request_id`, `timestamp`. There is no email, name, bank detail, amount entered or free text. State changes also leave a durable domain record: order events with the actor, the Payment_Attempt record, and the `auth_users` change. | This follows the data-export audit line precedent (log line, ids only). The durable records answer "who paid / ordered" without a new audit store. |
| PD19 | Rate limits per (tenant, user) through the existing slowapi `limiter`: Portal_API reads 120/min, order submit 10/min, order cancel 10/min, payment create 5/min, PDF/CSV downloads at `export_rate_limit` (default 5/min). Each limit is a setting. A breach returns 429 with `Retry-After`. The existing per-IP limits still apply. | Reads cover normal page loads. Writes are capped well below abuse rates but above any real usage. |
| PD20 | The portal UI is the `runsheet/src/app/portal/` route group, with its own layout. It has no staff `Sidebar`, `Header`, `AIChat`, `GlobalSearch`, `NotificationBell` or WebSocket. Sign-in stays on the shared `/signin`. After sign-in, a `customer` lands on `/portal` and staff land where they do today. A customer who opens a staff route is redirected to `/portal`, and staff who open `/portal` are redirected to the staff home. `modules.ts` `Role` gains `customer` with no staff modules visible. Accessibility target is WCAG 2.2 AA. One pinned dev dependency is added for automated axe checks (`@axe-core/playwright` exact version). | The audit asked for a route tree under `app/portal/`. Leaving out the staff shell keeps staff-only fetches and sockets from firing for customers. Full WCAG validation still needs manual testing with assistive technology. |
| PD21 | One email = one identity. If the invite email already exists in `auth_users` with any other role, or bound to another customer, the invite returns 409 `PORTAL_EMAIL_IN_USE`. | `auth_users.email` is unique, and silently re-binding a staff or other-customer identity would be a privilege change. |
| PD22 | If the bound Customer is `archived`, or the binding is revoked, every Portal_API call returns 403 `PORTAL_ACCESS_SUSPENDED`. The status is re-checked on each request (a cache of at most 60 s is allowed), and archiving revokes the customer's portal sessions. A `suspended` or `closed` Account doesn't block viewing or paying, and ordering follows the existing credit hooks. | An archived customer must lose access promptly. A suspended account still owes money, so blocking payment would hurt collection. |
| PD23 | Times are shown in the browser's local time zone with the zone abbreviation. Delivery dates are calendar dates in the tenant's zone. Money is USD from integer cents. Volumes use the tenant's `measurement_units`. | This matches existing UI conventions and avoids a new setting. |
| PD24 | Staff UI additions: (1) an admin-only "Portal access" panel on the customer detail page to invite, list, resend the link, and revoke; (2) an "Awaiting confirmation" filter and Confirm / Decline actions for Portal_Requests on the Orders page (dispatcher, admin). Confirm calls `release-hold` and Decline calls `cancel`. | Dispatcher confirmation (PD8) needs a visible queue. Reusing existing routes keeps one state machine. |
| PD25 | The CSP adds the Stripe origins that Payment Element needs (`js.stripe.com`, `api.stripe.com`, `*.stripe.com` frames, Financial Connections). The policy stays Report-Only until OI-10 is resolved. | Without these, the payment step fails once CSP is enforced. The OI-10 decision is outside this spec. |

### Assumptions (check in design review)

- A1. The `customer_id` on tanks and orders is the commerce `customer_id` (cross-module-entity-linkage Req: tank `customer_id` resolves to a commerce customer). Where it doesn't resolve, that tank or order isn't shown.
- A2. Existing forecast documents can be read by `customer_id`/`customer_tank_id` without the agent pipeline (the same filters the forecasts route uses).
- A3. The tenant's time zone is available from tenant settings. If it isn't, the date validation in PD9 uses UTC.
- A4. The SuperTokens built-in email service (or the configured SMTP relay) is good enough for invite email on staging. If no email arrives, the admin still has the link (PD3).

### Owner dependencies (not blockers for this spec)

- O1. Live ACH verification on staging needs a Stripe **test-mode** integration configured for `demo-tenant`. Creating Stripe credentials is on the steering "ask first" list, so the implementation step will ask then. Until then, payments are verified with a mocked connector and signed test webhooks.
- O2. Live verification of a successful Portal_Request needs `order_intake_pipeline` in `shadow` for the test window. That flag is shared with the QA-SWEEP agent: record the baseline, restore it exactly, and coordinate. The 409 path can be verified live as-is.

## Functional Requirements

### R1 Portal-user provisioning (admin)

User story: as a tenant admin, I want to invite and revoke portal users for a customer, so that the customer's staff can serve themselves.

1. WHEN an `admin` submits an invite (email, customer_id) for an `active` customer in the admin's tenant, THE System SHALL create or bind an `auth_users` row with roles `["customer"]`, that `customer_id`, `has_pii_access = false`, no `driver_id`, provision the SuperTokens user, and return a password-set link.
2. WHEN an invite succeeds AND email delivery is available, THE System SHALL also send the password-set link to the invited email.
3. IF the invite targets a customer outside the admin's tenant, or one that doesn't exist, THEN THE System SHALL return 404 and create nothing.
4. IF the invite email already exists with any other role or another `customer_id`, THEN THE System SHALL return 409 `PORTAL_EMAIL_IN_USE` and change nothing.
5. IF the customer already has 10 active Portal_Users, THEN THE System SHALL return 409 `PORTAL_USER_LIMIT_REACHED`.
6. IF the caller does not hold `admin` (including `dispatcher`, `driver` and `customer`), THEN THE System SHALL return 403 on every portal-user admin route.
7. WHEN an `admin` revokes a Portal_User, THE System SHALL remove the `customer` role and the `customer_id` binding, revoke all of that user's sessions, and make the user's next Portal_API call fail with 401 or 403.
8. WHEN an `admin` lists portal users for a customer, THE System SHALL return email, status (invited / active / revoked) and created time for that customer only.
9. WHEN an `admin` re-sends an invite, THE System SHALL mint a new password-set link with the existing mechanism.
10. THE System SHALL make the portal-user grant flow the only writer of `auth_users.customer_id`, and SHALL reject any other provisioning path that tries to assign the `customer` role.

### R2 Identity, isolation and route separation

User story: as the tenant, I need a customer to see only their own data, so that no other customer's or tenant's data is exposed.

1. WHEN a Portal_User signs in, THE System SHALL issue a session whose claims carry `tenant_id`, `roles = ["customer"]` and `customer_id`, all read from the bound `auth_users` row.
2. IF a session holds `customer` but has no `customer_id` claim, or holds `customer` together with any other role, THEN THE System SHALL refuse every authenticated route with 403 and log the refusal at WARN.
3. WHEN a session holding `customer` calls any HTTP route outside the portal allowlist (PD4), THE System SHALL return 403 `PORTAL_ROUTE_FORBIDDEN` before any handler or data access runs.
4. WHEN a session holding `customer` attempts any WebSocket handshake, THE System SHALL refuse it.
5. WHEN a session holding no `customer` role calls any Portal_API route, THE System SHALL return 403 `INSUFFICIENT_ROLE`.
6. WHILE `customer_portal_enabled` is false, THE System SHALL return 404 from every Portal_API route and every portal-user admin route, before the role check runs.
7. THE Portal_API SHALL derive `tenant_id` and `customer_id` only from the verified session, and SHALL ignore or reject (422) any `tenant_id` or `customer_id` supplied in the path, query or body.
8. WHEN a Portal_API request references an id that is outside the Customer_Scope, belongs to another tenant, or doesn't exist, THE System SHALL return 404 `RESOURCE_NOT_FOUND` with a body identical to the "doesn't exist" case.
9. WHEN any Portal_API list or search runs, THE System SHALL filter by both `tenant_id` and `customer_id` in the data-store query itself, not after fetching.
10. THE Portal_API SHALL build every response from an explicit allowlist model (`extra="forbid"`), and SHALL never include driver id, name, phone, email or license; truck or run ids; geotags; photos; signatures; other customers' data; or internal hold reasons.
11. WHILE the bound Customer is `archived` or the binding is revoked, THE System SHALL return 403 `PORTAL_ACCESS_SUSPENDED` on every Portal_API route (status re-checked per request, cache ≤ 60 s).
12. WHEN a customer is archived, THE System SHALL revoke all sessions of that customer's Portal_Users.

### R3 Account overview

1. WHEN a Portal_User calls `GET /api/portal/me`, THE System SHALL return the user's email, the customer's display name, the tenant's display name, and the capability booleans `ordering_available`, `invoices_available` and `payments_available`.
2. THE System SHALL set `ordering_available = false` exactly when the tenant's `order_intake_pipeline` state is `disabled`, `invoices_available` from the commerce invoicing flags, and `payments_available` from invoicing plus a configured Stripe integration.

### R4 Order requests

User story: as a customer, I want to request a delivery for my tank, so that I don't have to phone it in.

1. WHEN a Portal_User submits a valid request (PD9), THE System SHALL create the order through `OrderIntakePipeline` on the `web_portal` channel with `customer_id` from the session, ship-to and product from the tank, `status = on_hold` and `hold_reason = "awaiting_dispatcher_confirmation"`, and return 201 with the order id and the label "Awaiting confirmation".
2. WHEN the same `client_event_id` is submitted again by the same user, THE System SHALL return the original result and create no second order.
3. IF the request names a tank that isn't one of the customer's own `active` tanks, THEN THE System SHALL return 404 and create nothing.
4. IF any field breaks a PD9 rule, THEN THE System SHALL return 422 naming the invalid fields and create nothing.
5. IF the tenant's `order_intake_pipeline` flag is `disabled`, THEN THE System SHALL return 409 `ORDER_INTAKE_DISABLED` and create nothing.
6. WHEN the portal receives 409 `ORDER_INTAKE_DISABLED`, THE Portal UI SHALL keep the entered values, show "Online ordering is unavailable right now. Please contact your supplier to place this order.", announce it to assistive technology, and not retry automatically.
7. WHILE `ordering_available` is false, THE Portal UI SHALL show the same message and disable the submit control.
8. WHEN a dispatcher or admin confirms a Portal_Request with `release-hold`, THE System SHALL move it to `placed` (subject to the other hooks), and SHALL NOT re-apply the `awaiting_dispatcher_confirmation` hold.
9. WHILE a Portal_Request is `on_hold` with `awaiting_dispatcher_confirmation`, THE System SHALL keep it out of every consumer of `LOADABLE_ORDER_STATUSES`.
10. WHEN a Portal_User cancels their own request while it is `awaiting_dispatcher_confirmation`, THE System SHALL cancel it through the existing cancel path. IF it is in any other state, THEN THE System SHALL return 409 `ORDER_NOT_CANCELLABLE`.
11. WHEN a Portal_User lists orders, THE System SHALL return that customer's orders from every channel, newest first, paginated (≤ 50 per page), with the PD11 labels and no restricted fields.
12. WHEN a dispatcher or admin opens the Orders page, THE Staff UI SHALL offer an "Awaiting confirmation" filter and Confirm / Decline actions for Portal_Requests (PD24).

### R5 Invoices

User story: as a customer's accounts-payable contact, I want to see and download my invoices, so that I can pay and file them.

1. WHEN a Portal_User lists invoices, THE System SHALL return that customer's non-`draft` invoices across all of its accounts, newest first, paginated (≤ 50), filterable by status and issue-date range, through the PD12 projection.
2. WHEN a Portal_User opens an invoice in scope, THE System SHALL return its detail through the PD12 projection.
3. WHEN a Portal_User requests an invoice PDF in scope, THE System SHALL return `application/pdf` as an attachment named `invoice_<invoice_number or id>.pdf`, containing only projected fields.
4. WHEN a Portal_User requests the invoice CSV, THE System SHALL stream it through `services/csv_export.py` with the list's filters, limited to the Customer_Scope, with the projected columns.
5. IF a `draft` invoice id, or an id outside the scope, is requested in any form, THEN THE System SHALL return 404.
6. WHILE commerce invoicing is disabled, THE System SHALL return 404 from the invoice routes, and THE Portal UI SHALL hide the Invoices section.

### R6 ACH payment

User story: as a customer's AP contact, I want to pay an invoice by bank transfer, so that I don't need to mail a check.

1. WHEN a Portal_User starts a payment for an invoice in scope with status `open`, `partial` or `overdue`, giving an `Idempotency-Key` and an amount from 100 cents to the invoice's `remaining_cents`, THE System SHALL create a Payment_Attempt and a Stripe `us_bank_account` PaymentIntent whose metadata carries `tenant_id`, `customer_id`, `invoice_id` and `payment_attempt_id`, and return the client secret needed to confirm it.
2. WHEN the same `Idempotency-Key` is reused by the same user for the same invoice, THE System SHALL return the existing Payment_Attempt and create no second PaymentIntent. THE System SHALL send Stripe an idempotency key derived from the Payment_Attempt id.
3. IF a non-terminal Payment_Attempt already exists for the invoice, THEN THE System SHALL return 409 `PAYMENT_IN_PROGRESS`.
4. IF the amount is below 100 cents or above `remaining_cents`, or the invoice is `paid`, `void` or `draft`, THEN THE System SHALL return 422 (amount) or 409 (status) and create nothing.
5. IF the tenant has no Stripe integration, THEN THE System SHALL return 409 `PORTAL_PAYMENTS_UNAVAILABLE`.
6. IF Stripe returns an error or times out while the intent is being created, THEN THE System SHALL mark the Payment_Attempt `failed`, return 502 `PAYMENT_PROVIDER_ERROR` with no Stripe internals, and log at ERROR with the attempt id.
7. WHEN a verified Stripe webhook for a portal PaymentIntent reports `processing`, THE System SHALL set the attempt to `pending`.
8. WHEN a verified webhook reports `payment_intent.succeeded`, THE System SHALL check that the metadata `tenant_id` matches the webhook path and that the attempt's invoice and customer match, then record the payment through `payment_service.ingest(source="stripe", method="ach", external_id=<PaymentIntent id>)` and mark the attempt `succeeded`.
9. WHEN the same webhook event is delivered more than once, THE System SHALL record at most one commerce Payment for that PaymentIntent.
10. WHEN a verified webhook reports `payment_failed` or `canceled`, THE System SHALL mark the attempt `failed` or `canceled` and record no commerce Payment.
11. IF webhook metadata doesn't match a Payment_Attempt, or names another tenant, THEN THE System SHALL record nothing, log at WARN, and return 200 to Stripe.
12. THE existing Reconciliation_Record webhook handling SHALL keep working unchanged for intents that carry `reconciliation_id`.
13. WHEN a Portal_User views an invoice, THE Portal UI SHALL show any Payment_Attempt as "Payment processing" (created / pending), "Paid" (succeeded), or "Payment failed, try again" (failed / canceled).
14. THE System SHALL never store or return bank account or routing numbers. Only Stripe's last-4 and bank name may be shown.
15. WHEN a `charge.refunded` or `charge.dispute.created` event arrives for a portal payment, THE System SHALL write a WARN `portal_audit` line and change no commerce record.

### R7 Tanks

User story: as a customer, I want to see my tank levels and when my next delivery is coming, so that I don't run out.

1. WHEN a Portal_User lists tanks, THE System SHALL return that customer's `active` tanks with the PD17 fields.
2. WHEN a tank has no forecast, THE System SHALL return null forecast fields, and THE Portal UI SHALL show "Forecast not available yet".
3. WHEN `last_reading_at` is more than 7 days old, or missing, THE Portal UI SHALL show "Reading may be out of date".
4. WHEN a Portal_User opens a tank's delivery history, THE System SHALL return delivered orders for that tank from the last 24 months, newest first, paginated (≤ 50), with date, gallons delivered, product and ticket number only.
5. IF the tank is not one of the customer's own tanks, THEN THE System SHALL return 404.

### R8 Audit logging

1. WHEN any Portal_API request completes (success or refusal), and WHEN any portal-user admin action completes, THE System SHALL emit one `portal_audit` line with the PD18 fields.
2. THE `portal_audit` line SHALL NOT contain email, names, phone numbers, addresses, bank details, free-text notes or PO numbers.
3. WHEN a customer session is refused by the central deny (R2.3) or on a WebSocket (R2.4), THE System SHALL emit a WARN `portal_audit` line with `outcome = "forbidden_route"` and the path template.
4. WHEN an order is created or cancelled from the portal, THE System SHALL record the actor user id on the order event.
5. WHEN a payment is started, THE System SHALL record the actor user id on the Payment_Attempt.

### R9 Rate limiting

1. THE System SHALL apply the PD19 per-(tenant, user) limits to Portal_API routes through the existing `limiter`.
2. WHEN a limit is exceeded, THE System SHALL return 429 with `Retry-After`, and THE Portal UI SHALL show "Too many requests. Try again in N seconds."
3. THE limits SHALL be configurable through settings with the PD19 defaults.

### R10 Portal UI and accessibility

1. THE Portal UI SHALL live under `runsheet/src/app/portal/` with its own layout and pages: Overview, Orders (list and new request), Invoices (list and detail, with PDF / CSV / Pay), and Tanks (list and history).
2. THE Portal UI SHALL NOT mount the staff shell components listed in PD20, and SHALL NOT open WebSockets.
3. WHEN a `customer` signs in, THE UI SHALL route to `/portal`. WHEN a `customer` opens any staff route, THE UI SHALL redirect to `/portal`. WHEN staff open `/portal`, THE UI SHALL redirect to the staff home.
4. THE Portal UI SHALL meet WCAG 2.2 AA as its target: semantic landmarks and headings; every form control labelled; errors linked with `aria-describedby` and announced through a live region; full keyboard operation with visible focus; text contrast ≥ 4.5:1; no information conveyed only by color (levels and statuses carry text); target size ≥ 24×24 px.
5. THE Portal UI SHALL reflow without horizontal scrolling down to 320 CSS px wide, and SHALL use the existing design tokens.
6. WHEN any portal page is checked with axe in Playwright, THE check SHALL report zero serious or critical violations.
7. Existing staff pages SHALL behave exactly as before for staff roles. Existing staff unit and e2e suites pass unchanged.

## Non-Functional Requirements

- N1 Security: default-deny for customer sessions (R2.3–R2.4). No new unauthenticated route, apart from reusing the existing signed Stripe webhook. Portal endpoints don't log secrets, client secrets or bank data.
- N2 Performance: portal list endpoints at p95 < 500 ms and invoice PDF at p95 < 2 s on staging at `demo-tenant` data volume.
- N3 No new backend dependencies (reportlab, slowapi and stripe are already pinned). One frontend dev dependency at an exact version (PD20).
- N4 Errors use the existing `AppException` envelope and `ErrorCode` registry. New codes are added to `ERROR_CODE_STATUS_MAP`.
- N5 Schema changes go through Alembic (the `auth_users.customer_id` column and the Payment_Attempt store) and are backward compatible.

## Acceptance Criteria

1. A route-inventory test enumerates every mounted HTTP route and asserts that a `customer` session gets 403 `PORTAL_ROUTE_FORBIDDEN` on every route outside the PD4 allowlist. Adding a route without updating the allowlist must not let a customer through.
2. A test asserts that every WebSocket endpoint refuses a `customer` session.
3. A route-inventory test asserts that `admin`, `dispatcher`, `driver` and `platform_admin` sessions each get 403 on every Portal_API route.
4. Two-customer tests (customers A and B in one tenant) assert that A's session gets 404 for B's invoice, invoice PDF, order, order cancel, tank, tank history and payment start, and that A's lists and CSV contain no B rows.
5. Two-tenant tests (`demo-tenant` and `qa-tenant-b`) assert the same as AC 4 across tenants, and that a `tenant_id` or `customer_id` in the query or body is ignored or rejected.
6. A response-model test asserts that no Portal_API response contains the restricted fields in R2.10, including invoices whose `delivery_result` has driver id, recipient, photos and geotag.
7. Provisioning tests cover R1.1–R1.10: admin only, cross-tenant 404, email-in-use 409, the 10-user cap, revoke ends sessions, `customer` can't be assigned through another path, and a mixed-role or claimless customer session is refused (R2.2).
8. With `customer_portal_enabled = false`, every Portal_API and portal-admin route returns 404 for both customer and staff sessions.
9. Order tests: a valid request is stored `on_hold` / `awaiting_dispatcher_confirmation` on `web_portal`, is absent from loader, prioritizer and planner candidate queries, moves to `placed` on `release-hold` without being re-held, and a duplicate `client_event_id` returns the original order. A tank outside scope gives 404 and PD9 violations give 422.
10. With the intake flag `disabled`, `POST /api/portal/orders` returns 409 `ORDER_INTAKE_DISABLED`, `GET /api/portal/me` returns `ordering_available = false`, and a UI test shows the message, keeps the form values and makes no retry request.
11. Payment idempotency tests: the same `Idempotency-Key` gives one attempt and one PaymentIntent create call. A second key while an attempt is in flight gives 409. A duplicated `payment_intent.succeeded` webhook gives exactly one commerce Payment. Mismatched metadata gives no record and 200. An amount above remaining gives 422.
12. Webhook tests: `processing` → pending, `succeeded` → Payment recorded and invoice `partial`/`paid`, `failed`/`canceled` → no Payment. Reconciliation-record intents behave as before (existing `test_stripe_endpoints.py` and `test_stripe_connector.py` pass).
13. With no Stripe integration, payment create returns 409 `PORTAL_PAYMENTS_UNAVAILABLE` and the UI shows the PD15 message.
14. Invoice tests: drafts are never returned. The PDF is `application/pdf` and contains only projected fields (checked with pypdf). The CSV has a BOM and injection escaping and contains only scope rows.
15. Tank tests cover R7.1–R7.5, including no forecast and stale-reading cases.
16. Audit tests: every Portal_API route and portal-admin action emits exactly one `portal_audit` line with the PD18 fields and none of the R8.2 data. A central-deny refusal emits a WARN line.
17. Rate-limit tests: exceeding each PD19 limit returns 429 with `Retry-After`, and one user's limit doesn't consume another user's.
18. Playwright axe checks on every portal page report zero serious or critical violations. Keyboard-only tests complete sign-in → new request → invoice PDF download. Pages show no horizontal scroll at 320 px.
19. The existing backend, Jest and Playwright suites pass. CI passes on the commit before any staging deploy.
20. Staging verification uses `QA-` fixtures in `demo-tenant` and `qa-tenant-b`, which are deleted afterwards (dry run → delete → verify). Any flag touched is restored to its recorded baseline.

## Out of Scope

- The COGS / margin feed (OI-06's other half, its own spec).
- Contract / price-protection view (audit rec 9 mentions it, but it isn't in the owner's portal scope).
- Self-signup, customer-managed sub-users, and customer-side roles (view-only vs payer).
- Card payments, saved bank accounts, autopay, multi-invoice payments, and pushing portal payments to QuickBooks or other ERPs.
- Automated handling of ACH returns and disputes (logged only, PD14).
- Live order tracking, driver ETA, and live tank telemetry. Portal WebSockets.
- Customer notifications about request confirmation (the customer-notification pipeline may already send `order_status_update`; not changed here).
- Staff impersonation ("view as customer").
- Orders to addresses without a registered tank.
- Enforcing the CSP (OI-10) and fixing the cross-tenant WebSocket broadcast (OI-01). The portal avoids both by not opening sockets.
