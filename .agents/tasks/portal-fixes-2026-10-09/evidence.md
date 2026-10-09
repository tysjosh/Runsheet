# Portal fixes (2026-10-09): evidence

Branch `production-readiness/portal-fixes` (worktree `.worktrees/portal-fixes`, off `origin/production-readiness/go-live-blockers` at `fccfbbd`). Iteration 1. Code commit `8359457`; this file, `sendgrid-setup.md` and the screenshots follow in a docs commit (`git add -f .agents/...`). Not pushed, no PR, no deploy (release step).

## Staging investigation (read-only one-shot tasks on `runsheet-staging-api:44`)

| Probe | Finding |
|---|---|
| Redis `tenant:demo-tenant:display_name` | **absent**. The PE1 name was never set: phase 3P release step 1 (`set_tenant_display_name --name`) didn't run, so `supplier_name()` fell back to the tenant id. Root cause of A1. |
| Redis `tenant:demo-tenant:settings` | absent (defaults: US, gal/mi). No tenant time zone exists anywhere; the Dispatch Board rule (`get_tenant_timezone`) gives `America/Chicago`. |
| `overlay_ff:order_intake_pipeline:demo-tenant` | `disabled` (read only, never changed). |
| `QA-OWNER-REQ-1` | `status=cancelled`, `hold_reason=null`. One `order_cancelled` event at 2026-10-08T23:25:56Z, payload `reason=cancelled_by_customer`, `old_status=on_hold`, `actor_user_id=4f779b3f…`. |
| actor `4f779b3f…` | `auth_users` maps it to **olukotunjosh@gmail.com** (the owner). |
| API log 23:24–23:26Z | owner session loads Home (23:24:02), Orders (23:24:10, 23:25:49), then a browser `OPTIONS` + `POST /api/portal/orders/QA-OWNER-REQ-1/cancel` → 200 at 23:25:56 (`portal_audit` `cancel_portal_order`, outcome ok). |

### A8 conclusion: not a bug

`QA-OWNER-REQ-1` was cancelled from the owner's own signed-in browser with the row's **Cancel request** button, 6.5 s after opening Orders. Only `PortalOrderService.cancel` writes `cancelled_by_customer`. PD8 gives Cancel request no confirm step (like staff Decline), so one click cancels. No code change. Adding a confirm step would be a product change to PD8, so it's raised in the summary rather than made.

### B1: what happened to "Request delivery" on staging before this change

`POST /api/portal/orders` → `PortalOrderService.submit` read `pipeline.get_ordering_state()` (the `order_intake_pipeline` flag). With the flag `disabled`, it answered 409 `ORDER_INTAKE_DISABLED`, and `/me.ordering_available` was false. Even past that check, `_ingest_common` step (0) short-circuited to `legacy_passthrough`. When it worked, the order landed `on_hold` / `awaiting_dispatcher_confirmation` (`web_portal`, step i3). Dispatchers see it in the awaiting-confirmation queue (`GET /api/orders?hold_reason=awaiting_dispatcher_confirmation`), confirm with `POST /api/orders/{id}/release-hold` (→ `placed`, portal shows Confirmed) and decline with `POST /api/orders/{id}/cancel`. The only notice was the orders WebSocket broadcast. No email existed.

## Staging changes made

| When (UTC) | What | How | Reversible |
|---|---|---|---|
| 2026-10-09 | `demo-tenant` display name set to **Demo Fuels** | one-shot `python -m scripts.set_tenant_display_name --tenant demo-tenant --name "Demo Fuels"` on `api:44` (task `a6884a494aa2410291e8af68877b821c`), then `--show` → `name='Demo Fuels'` | `--clear` |

No flag changed (`order_intake_pipeline` only read). No fixture created or deleted. No `QA-SWEEP-*` record touched. No credential created or read.

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | Supplier fallback is "Your fuel supplier" (portal, PDF, invite), never the slug. | Brief A1. Same text the invite email already used. |
| D2 | Display name persists through (a) the Redis no-TTL key, set now, and (b) a startup seed: `SEED_TENANT_DISPLAY_NAME` (staging script default "Demo Fuels") for `SEED_TENANT_ID`, written with `SET NX`, so an operator's name always wins. | Survives deploys and a Redis rebuild without a manual step. |
| D3 | Portal times use the **tenant zone** (`/me.time_zone` from the Dispatch Board rule, America/Chicago today) and name the zone on **every** time, including delivered moments. This replaces PD23 (browser zone). Request-dialog times are wall clock in that zone, and the dialog says so ("Times are Central Daylight Time."). | Brief A5. One clock for customer and dispatcher. The zone appears on every row because CDT/CST changes with DST. |
| D4 | Quantities in lists are whole gallons with a muted qualifier ("1,600 gal requested", "276 gal delivered"). Invoice line items and invoice delivery keep the billed decimal (`billedVolume`). | Brief A6; invoice arithmetic must still add up. |
| D5 | Orders table: fixed layout, two-line cells (tank/product, day/time, value/qualifier, PO/ticket), PO and ticket merged into one "PO and ticket" column, truncation only with `title`. Below 1280 px the row button reads "Cancel"; its accessible name stays "Cancel request for …". | Brief A4. Fits 982 px at 1024 with no clipping. |
| D6 | **Portal ordering decoupled**: a per-tenant setting `tenant:{id}:portal_ordering` (Redis, no TTL, only `disabled` stored), **default ON**. With no settings store wired it's on; a failed read is off (fail closed). `ingest_portal` passes `honour_overlay_flag=False`, so the rollout flag no longer gates portal requests. Other channels are unchanged. | Brief B2. The shared flag isn't ours. |
| D7 | The admin control is a checkbox at the top of the **Portal access** panel (customer detail, admin only), labelled as tenant-wide, backed by `GET/PUT /api/commerce/portal-settings` (GET any staff role, PUT admin). Not added to staff Settings. | "Wherever tenant portal settings live": portal admin lives in that panel. Staff Settings is a Phase 3 module page I must not edit. |
| D8 | Validation added: min 25 gal (or the whole tank when it's smaller), ≤ capacity, ≤ room left (capacity − level) when the reading is fresh (`portal_stale_reading_days`), window must end in the future. Product is always the tank's own product (no customer choice), and the dyed-diesel hook still runs. The client mirrors these rules. | Brief B2. A stale reading isn't trusted to refuse a request. |
| D9 | Dispatcher notice through the activity log (`agent_id=system`, "New delivery request: {customer} · {tank} · {qty} · {window}"), the feed behind the staff bell, broadcast to the tenant only. | "Existing notification pipeline". The bell renders `action_type: tool_name`, so both are readable text. |
| D10 | Customer emails go only to the portal user who made the request (`order_placed` event's actor → `auth_users` email): received on submit, confirmed on release-hold, declined on staff cancel of a request still awaiting confirmation. A customer's own cancel sends nothing. Non-portal and non-awaiting orders send nothing. | Privacy. No email field is stored on portal orders. |
| D11 | Templates are 4 new `email` entries in `DEFAULT_TEMPLATES` (`portal_invite`, `portal_request_received/_confirmed/_declined`). The tenant's edited copy wins when present. HTML is generated from the same text (escaped, `lang="en"`, labelled link plus the visible URL, 16 px, no images or tracking). `SendGridEmailDispatcher` gains an optional `html_body`. | "Use the existing notification templates mechanism". Text and HTML can never disagree. |
| D12 | `sendgrid==6.12.5` added to `requirements.txt`. | It was **missing**, so the existing SendGrid dispatcher would have failed its import on staging and fallen back to the stub even with a key. Dry-run resolve: adds `Werkzeug 3.1.9`, `python-http-client 3.3.7`. `cryptography==46.0.4` satisfies `>=45.0.6`. |
| D13 | `staging_aws.sh`: one secret `runsheet-staging/sendgrid-api-key` → ECS secrets `SENDGRID_API_KEY` + `SMTP_PASSWORD`. Env: `SENDGRID_FROM_EMAIL`, `SMTP_HOST=smtp.sendgrid.net`, `SMTP_PORT=587`, `SMTP_USERNAME=apikey`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, `SMTP_SECURE=false`. `EMAIL_FROM` defaults to `no-reply@${DOMAIN}`. The ARN joins the execution role's read-secrets inline policy (`ensure_execution_role`, now also called from `deploy` when the secret exists). A missing secret gives a warning, no email, and the deploy continues. The key is never read or echoed (ARN only). Plus `SEED_TENANT_DISPLAY_NAME`. | Brief C1. Localized edits. |
| D14 | Phone row padding: `space.rowY` back to 16 px on phones (Account rows, invoice line items, Home empty-orders row); tank rows use the new `space.tankRowY` (12 px). | Brief A9. Home fold unchanged (chrome.spec fold test passes). |
| D15 | Cancelled/Void badges in the portal render the shared badge markup without strikethrough (`PortalBadge`); shared `StatusBadge` and tokens are untouched. | Brief A7. Staff components aren't mine to change. |
| D16 | Task docs and screenshots under `.agents/` are committed with `git add -f` (the CI "ignored files" check doesn't see `.git/info/exclude`, and other `.agents` files are already tracked). | Brief: commit gitignored paths. |

## Consistency pass (all portal pages)

The new `e2e/portal/portal-fixes.spec.ts` runs owner-shaped data (long customer name, long PO and ticket, a 275.5 gal delivery, a cancelled request) at 390×844, 1024×768, 1280×800 and 1440×900 on Home, Orders, Request delivery, Invoices, Invoice, Void invoice, Pay, Tanks, Tank and Account. On every page it checks: no page-level horizontal scroll; no table wider than its card or content spilling a cell; every truncated text has a `title`; nothing struck through; no `demo-tenant`/`cust_`/`acct_`/`inv_`/`undefined`/`NaN` in the text; axe 0 critical/serious. It found and fixed truncation without titles on Home/Orders phone rows, the invoice row number and due line, tank and picker titles, the page title, and tank history lines. Dates: every single-moment time (`BalanceCard` payment, invoice delivery and payment, tank last reading and history, orders delivered) now uses `dateTimeZone` in the tenant zone.

Screenshots (40): `.agents/tasks/ui-revamp-2026-10-08/screenshots/portal-fixes/{page}-{w}x{h}.png`. `orders-1024x768.png` shows the full table within the card with the PO truncated with a tooltip, "Demo Fuels" above the untruncated customer name, avatar initials from the user, and a legible Cancelled badge. `home-390x844.png` shows three tanks and Balance due above the tab bar.

## Verification (iteration 1, local macOS)

| Check | Command | Result |
|---|---|---|
| Backend full | `pytest -q --no-cov --timeout=120 -x --deselect tests/unit/test_dispatch_board_perf.py` (CI env: REDIS_URL, JWT_SECRET, ENVIRONMENT=test) | **14,808 passed**, 274 skipped (Postgres-backed suites skip locally; Docker wedged, so they need CI) |
| Backend portal + new | `pytest tests/portal tests/unit/test_tenant_settings.py tests/unit/test_template_renderer.py tests/unit/test_real_channel_dispatchers.py` | all pass. New `tests/portal/test_portal_fixes.py` 21 tests: ordering default/per-tenant/fail-closed, display-name seed NX, settings API (admin PUT, dispatcher GET/403, customer refused, portal-off 404, unknown field 422), received/confirmed/declined emails to the requester only, replay notifies once, customer cancel sends nothing, other orders send nothing, no-SendGrid still notifies dispatchers, notifier failure never fails the request, templates registered, invite wording + HTML escaping, tenant template override, window/quantity formats, SendGrid configured/unconfigured with HTML and **no key in logs**, auth email via SendGrid SMTP settings with no key in logs. `test_portal_orders.py`: ordering off 409 / per-tenant / replay while off / **works with the pipeline flag disabled, shadow, active_gated, active_auto**, room-left 422, minimum 422, window already over 422. Existing cancel-race tests (`test_order_release_hold_portal.py`) still pass. |
| Endpoint registry | `python scripts/generate_endpoint_registry.py` | regenerated (+2 rows: GET/PUT `/api/commerce/portal-settings`), committed |
| Alembic | no migration (Redis keys only) | single head unchanged |
| tsc | `npx tsc --noEmit` | 0 errors |
| Lint | `npm run lint` (Biome) | 0 errors, 2 pre-existing warnings |
| Jest full | `npx jest --ci` | **149 suites, 2041 passed**, 1 skipped (a first run alongside the backend suite timed out in staff dispatch-board suites under load; rerun alone: all pass) |
| Jest new | `tenantZoneFormat.test.ts`, `topBarIdentity.test.tsx`, PortalOrderingSetting cases in `PortalAccessPanel.test.tsx`, updated `portalStatus`/`portalFormat`/`RequestDeliveryDialog` tests | pass |
| Portal e2e (fixtures) | `npx playwright test -c playwright.portal.config.ts --project=chromium` | **174/174** (chrome 22, visual+axe 106 with 0 critical/serious, sign-in keyboard 3, portal-fixes 43). 9 baselines re-recorded for intended changes: account-390 and home-empty-390 (16 px rows), orders-1280 (table), request ×6 (gallons hint, zone line) |
| ui-revamp e2e | `npx playwright test -c playwright.ui-revamp.config.ts --project=chromium` | **128/128** |
| Build | `next build` (portal e2e webServer) | OK |
| Fresh-checkout build | `git archive HEAD runsheet design scripts` → `tmp/portal-fixes/fresh`, shared `node_modules` symlink, `npx next build` | OK (`/portal` 3.48 kB); dir deleted |
| staging_aws.sh | `bash -n`; generator run with and without `SENDGRID_SECRET_ARN` | syntax OK; without: no SMTP/SendGrid env or secrets; with: 7 env + 2 secrets pointing at the ARN |
| GitHub CI (Postgres jobs) | not run: branch not pushed in this step | **pending, release step** |

Cleanup: `.next`, `test-results`, coverage removed; probe scripts deleted. `tmp/portal-fixes/` holds only run logs and the one-shot helper.

## Iteration 2 (review fixes)

| Finding | Fix |
|---|---|
| HIGH: blocking SendGrid send on request paths | `SendGridEmailDispatcher.dispatch` now runs the synchronous `client.send` in a dedicated 4-thread executor (`sendgrid-send`) under `asyncio.wait_for(..., SEND_TIMEOUT_SECONDS=10)`; a timeout returns `failed` with `failure_reason` "SendGrid send timed out after 10s". `__init__` also sets the SDK's `python_http_client` socket timeout to the same bound (checked in the 6.12.5 / 3.3.7 sources: `Client.timeout` feeds `urlopen(timeout=...)`), so a stalled thread frees itself. Fixed at the dispatcher (D17), so the notification pipeline is covered too. A dedicated executor rather than `to_thread`: the first attempt with `to_thread` still held the test request for the full hang, because the default executor is joined on loop shutdown. |
| LOW: key in shell history | `sendgrid-setup.md` step 3 uses `read -rs SG_KEY`, `--secret-string "$SG_KEY"`, `unset SG_KEY`. |
| LOW: tenant-wide switch on a per-customer panel | No code change. Raised below for the owner. |
| LOW: nearly-full tank only allows Fill to full | No code change. Raised below for the owner. |

New tests (`tests/portal/test_portal_fixes.py`): `test_hung_sendgrid_times_out_without_blocking_the_event_loop` (send hangs; dispatch returns `failed` in < 2 s, a concurrent ticker keeps running, the HTTP client timeout is set, no key in logs) and `test_submit_and_release_hold_return_when_sendgrid_hangs` (real `send_portal_email` → dispatcher with a hanging fake SDK; `POST /api/portal/orders` and release-hold each return in < 3 s, dispatchers still notified, order Confirmed). Without the fix each request blocks for the full 5 s hang.

| Check | Command | Result |
|---|---|---|
| Targeted | `pytest --no-cov tests/portal/test_portal_fixes.py tests/unit/test_real_channel_dispatchers.py` | 50 passed |
| Backend full + coverage (CI form) | `pytest --cov=. --cov-fail-under=70 -x -q -p no:cacheprovider --deselect tests/unit/test_dispatch_board_perf.py` | **14,810 passed**, 274 skipped (Postgres), coverage **79.68%** |
| Board perf budgets | `pytest tests/unit/test_dispatch_board_perf.py --no-cov` | 9 passed |
| Changed-file coverage | `python scripts/check_coverage.py --threshold 0` | passed (309 files) |
| Endpoint registry | `python scripts/generate_endpoint_registry.py` + `git diff docs/endpoint-registry.md` | no diff |
| Alembic | no migration | single head unchanged |
| tsc / lint / Jest | `npx tsc --noEmit`; `npm run lint`; `npx jest --ci` | 0 errors; 0 errors, 2 pre-existing warnings; 149 suites, 2041 passed, 1 skipped |
| Build, fresh-checkout build, portal + ui-revamp e2e | not rerun | No file under `runsheet/` changed this iteration (tree `26d9ac0` identical to iteration 1), so iteration 1's results (174/174, 128/128, builds OK) still apply to the same frontend tree. |
| GitHub CI (Postgres jobs) | not run | pending, release step |

Cleanup: coverage output removed; `tmp/portal-fixes/pkg` (downloaded wheels to read the SDK timeout) deleted.

## Owner follow-ups

- SendGrid: `sendgrid-setup.md` (create a Mail Send key, verify the sender, store the secret with `read -rs`, tell the orchestrator).
- Where the ordering switch lives: Customers → any customer → **Portal access** panel → "Customers can request deliveries online" (admin only). It's **tenant-wide**: switching it on one customer changes it for every customer, and the label says so. Staff Settings would be a better home once that module is in scope (Phase 3 owns it now).
- Nearly-full tanks: when a fresh reading leaves less than 25 gal of room, no gallon amount passes both the 25 gal minimum and the room-left limit, so the customer can only choose **Fill to full** (the dialog hint explains this). Options: keep it, or lower the minimum for nearly-full tanks.
- Possible product change (not made): a confirm step on the portal's Cancel request (PD8 has none). The owner's own click cancelled `QA-OWNER-REQ-1`.
