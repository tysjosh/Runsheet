# Persistence layer — PostgreSQL source-of-truth

This package introduces **PostgreSQL as the transactional system-of-record**
for the financial / commerce models and for the idempotency-key concurrency
primitive, while keeping **Elasticsearch as a rebuildable read/search
projection**.

It is the first slice of the production data-architecture migration: prove the
pattern end-to-end for Commerce + idempotency before extending it to orders,
jobs, and master data.

## Why

Elasticsearch is excellent for search, dashboards, telemetry, and append-only
event streams, but it cannot enforce the guarantees money needs:

- no cross-document **unique constraints** (duplicate invoice numbers, duplicate
  Stripe charges, duplicate idempotency keys could all slip through under
  concurrency);
- no **row locks** (`SELECT ... FOR UPDATE`) for safe credit/balance mutation;
- no **foreign keys** to encode `customer → account → invoice → payment`.

Postgres provides all three. ES stays in the picture as a projection that can
be dropped and rebuilt from Postgres at any time.

## Architecture

```
        write path (one DB transaction)
  ┌─────────────────────────────────────────┐
  │  business row  +  outbox_events row      │   ← commit atomically
  └─────────────────────────────────────────┘
                      │
                      │  OutboxRelay polls unpublished rows
                      ▼
            Elasticsearch *_current indices   ← read / search projection
```

- **`database.py`** — async engine + `session_scope()` transaction context.
  Dormant unless `settings.database_url` is set.
- **`models.py`** — SQLAlchemy ORM: `customers`, `accounts`, `invoices`,
  `invoice_line_items`, `payments`, `idempotency_keys`, `outbox_events`,
  `invoice_counters`, plus the rest-of-commerce tables: `price_books`,
  `pricing_rules`, `invoice_events`, `account_events`, `dunning_events`,
  `ar_aging_snapshots`.
- **`projections.py`** — turns ORM rows into the exact ES `*_current` document
  shapes (single source of truth for the ES contract).
- **`outbox.py`** — `enqueue(...)` appends an outbox event in the same
  transaction as the business write.
- **`repositories.py`** — transactional write methods (each does business write
  + outbox enqueue atomically).
- **`outbox_relay.py`** — drains unpublished outbox rows into ES (at-least-once,
  idempotent upserts).

## Opt-in & reversible

Two settings gate everything:

| Setting | Default | Effect |
|---|---|---|
| `DATABASE_URL` | unset | When unset, the layer is **dormant** — app is ES-only, exactly as before. |
| `COMMERCE_DUAL_WRITE_POSTGRES` | `false` | When true (and `DATABASE_URL` set), commerce services ALSO write Postgres + outbox during the soak. ES stays the read path. |
| `COMMERCE_PAYMENTS_AUTHORITATIVE` | `false` | Promotes payments to Postgres-first so the dedupe constraint actually rejects re-delivered webhooks. |
| `COMMERCE_READ_FROM_POSTGRES` | `false` | Read-cutover: serve commerce get/list from Postgres instead of ES. |

During the soak the Postgres write is best-effort additive (a Postgres hiccup
never fails a request; ES remains authoritative). After parity is verified,
reads cut over to Postgres and the direct-to-ES writes are removed.

### Promoting payments to write-authoritative

Payments has an extra promotion flag, `COMMERCE_PAYMENTS_AUTHORITATIVE`. When
on, `PaymentService.ingest` writes the payment row to Postgres **first**, so
the `(tenant, source, external_id)` unique constraint becomes a hard
dedupe — a re-delivered Stripe/QBO webhook can never create a second payment
even under concurrency; the service returns the existing payment. Enable it
only **after** customers/accounts/invoices are dual-written and backfilled, so
the payment's FK parents already exist in Postgres (otherwise the authoritative
insert safely falls back to the ES path and logs a warning).

## Migration runbook

### Local Postgres via Docker

A `docker-compose.yml` at the backend root runs Postgres 16 for local dev and
the read-cutover soak. Its credentials match the `DATABASE_URL` already set in
`.env.development`:

```bash
docker compose up -d postgres     # start (detached); data persists in a named volume
docker compose ps                 # wait for "(healthy)"
docker compose logs -f postgres   # tail logs
docker compose down               # stop, keep data
docker compose down -v            # stop AND wipe the data volume
```

The container listens on `localhost:5432` as user/db `runsheet`. These are
local-only dev defaults — never reuse them for staging/production.

```bash
# 1. Point at your database (already set in .env.development for the container)
export DATABASE_URL="postgresql+psycopg://runsheet:runsheet@localhost:5432/runsheet"

# 2. Create the schema
./venv/bin/alembic upgrade head

# 3. (optional) enable commerce dual-write for the soak
export COMMERCE_DUAL_WRITE_POSTGRES=true
```

Create a new migration after changing `models.py`:

```bash
./venv/bin/alembic revision --autogenerate -m "describe change"
./venv/bin/alembic upgrade head
```

Roll back the initial migration:

```bash
./venv/bin/alembic downgrade base
```

## Running the outbox relay

The relay projects Postgres writes into ES. It runs **automatically as a
background task at startup** (`bootstrap/persistence.py`) whenever the
persistence layer is active (`DATABASE_URL` set) and `OUTBOX_RELAY_ENABLED` is
true (the default). It is cancelled cleanly on shutdown. Set
`OUTBOX_RELAY_ENABLED=false` to run it out-of-band (a separate worker) or to
pause projection; tune cadence with `OUTBOX_RELAY_POLL_INTERVAL_SECONDS`.

You can also drive it manually (CLI / backfill / tests):

```python
from services.elasticsearch_service import elasticsearch_service
from persistence.outbox_relay import OutboxRelay, project_pending

# Background task (what bootstrap does):
relay = OutboxRelay(elasticsearch_service)
await relay.run_forever(poll_interval_seconds=1.0)

# One-shot drain to completion (CLI / backfill):
published = await project_pending(elasticsearch_service)
```

Each event is an idempotent ES upsert keyed by the aggregate id, so
re-delivery after a crash is safe. Failed projections increment `attempts` and
record `last_error`; a row is parked after 10 attempts so a poison message
never wedges the queue.

## Tests

Run against in-memory SQLite (no external Postgres/ES needed):

```bash
ENVIRONMENT=test JWT_SECRET="ci-test-jwt-secret" JWT_ALGORITHM="HS256" \
  ELASTIC_ENDPOINT="http://localhost:9200" ELASTIC_API_KEY="mock-key-for-ci" \
  REDIS_URL="redis://localhost:6379" \
  ./venv/bin/python -m pytest tests/persistence/ --no-cov -q
```

The suite proves: DB-enforced uniqueness (invoice numbers, idempotency keys,
external payment ids), dual-write atomicity (business row + outbox commit or
roll back together), and byte-compatible ES projection via the relay.

## Migration to retiring the commerce ES indices

The ES projection for these four models is **transitional**, not a permanent
read surface. The end state is: commerce data lives only in Postgres, and the
`customers_current` / `accounts_current` / `invoices_current` / `payments_current`
ES indices are deleted. The phased path:

| Phase | Action | Flag / tool | Status |
|---|---|---|---|
| 1 | Dual-write (PG + outbox→ES) | `COMMERCE_DUAL_WRITE_POSTGRES` | ✅ all four aggregates |
| 2 | Payments authoritative dedupe | `COMMERCE_PAYMENTS_AUTHORITATIVE` | ✅ |
| 3 | One-time backfill ES → PG | `python -m persistence.backfill` | ✅ script + tests |
| 4 | Read-cutover: serve reads from PG | `COMMERCE_READ_FROM_POSTGRES` | ✅ get/list/find |
| 5 | Stop writing the ES projection | (stop the relay / drop dual-write) | ⬜ after parity soak |
| 6 | Drop ES indices + delete mappings/seeders | (manual ops + code removal) | ⬜ final |

Each phase is independently reversible by flipping its flag back off; nothing
is destructive until Phase 6.

### One-time backfill (Phase 3)

Populate Postgres with the historical ES records before cutting reads over.
Idempotent and safe to re-run; supports `--dry-run` and `--tenant`:

```bash
DATABASE_URL=postgresql+psycopg://... ENVIRONMENT=production \
  ./venv/bin/python -m persistence.backfill --dry-run        # report counts
DATABASE_URL=postgresql+psycopg://... ENVIRONMENT=production \
  ./venv/bin/python -m persistence.backfill --tenant demo-tenant
```

It inserts in dependency order (customers → accounts → invoices → payments) so
FKs are always satisfiable, skips rows already present, and does NOT enqueue
outbox events (ES is the *source* here, not the target).

### Read-cutover (Phase 4)

After dual-write + backfill reach parity, flip `COMMERCE_READ_FROM_POSTGRES=true`.
The services then resolve `get` / `list` / `find_by_order` from Postgres via
`persistence/read_repositories.py`, returning byte-identical projections
(`persistence/projections.py`). Reads no longer touch ES — the precondition for
Phases 5–6.

> **Dev status (2026-06-02):** `COMMERCE_READ_FROM_POSTGRES=true` is now LIVE in
> `.env.development`. Wired read paths (commerce get/list/find + the hybrid
> master-data/current-state/config gets and lists, the orders/jobs list +
> search paths, the scheduling metrics/analytics endpoints, the asset-cert
> **list** (sorted by expiry_date), the tax-engine FIPS jurisdiction + customer
> exemption lookups, the tenant_job_policies get, the legacy `trucks` list/get,
> and the ops `shipments` reads + metrics) serve from Postgres. Every migrated
> index is now cut over for reads. Flip the flag back off in the env file to
> instantly revert every read to ES.

### Parity check (gate the soak with evidence)

Before flipping the read-cutover, prove Postgres matches ES record-for-record:

```bash
DATABASE_URL=postgresql+psycopg://... ENVIRONMENT=development \
  ./venv/bin/python -m persistence.parity_check --tenant demo-tenant
```

It fetches every commerce record from BOTH stores and diffs the projected
shapes, reporting any divergence (missing rows, field mismatches). Exit code is
non-zero on any mismatch, so it can gate a soak in CI. Benign representation
differences (null vs empty collection, date vs full-datetime, recomputed
`updated_at`) are normalized; real divergences are flagged. Run it again
periodically during the soak to confirm dual-write keeps the two stores in
lockstep.

## What's next (not in this slice)

- ~~Extend dual-write to accounts~~ ✅ done (`AccountService` create / update /
  `refresh_open_balance`, with row-locked balance mutation).
- ~~Extend dual-write to invoices~~ ✅ done (`InvoiceService` generate / finalize /
  apply_payment / void).
- ~~Replace `invoice_numbering.py` with a Postgres sequence~~ ✅ done
  (`InvoiceCounterORM` + `InvoiceRepository.allocate_number`, allocated under a
  row lock inside the finalize transaction). The legacy module + its
  `invoice_counter_checkpoints` ES index have been **removed** (Phase 6).
- ~~**Payments** — dual-write `PaymentService`~~ ✅ done (ingest / reverse), with
  the **authoritative promotion** in place: set `COMMERCE_PAYMENTS_AUTHORITATIVE=true`
  and `PaymentService.ingest` inserts into Postgres first so the
  `(tenant, source, external_id)` unique constraint *rejects* a re-delivered
  webhook (returns the existing payment idempotently) instead of relying on the
  best-effort ES fast-path.
- ~~Backfill (Phase 3) + read-cutover (Phase 4)~~ ✅ done (`persistence.backfill`
  + `COMMERCE_READ_FROM_POSTGRES`).
- ~~Rest of commerce~~ ✅ done — price books + pricing rules, the invoice /
  account / dunning event ledgers, and AR aging snapshots are dual-written
  (outbox-projected), covered by the backfill + parity check.
  **Read-cutover (price books + pricing rules):** `PriceBookService.get` /
  `list` / `_get_rules_for_book` serve from PG (typed `PriceBookReadRepository`
  projecting `price_books_current` / `pricing_rules_current` via
  `price_book_to_doc` / `pricing_rule_to_doc`), and `PricingEngine._query_es_rules`
  sources its candidate set from PG (`read_pricing_rules_by_product` over the
  `ix_pricing_rule_tenant_product` index; effective-window / quantity /
  precedence filtering still runs in Python, identical to the ES path).
  **Read-cutover (secondary AR / credit / dunning reads + background jobs):**
  the invoice/account *scan* reads now serve from PG — `ARAgingService`
  (`compute_account_aging` / `compute_tenant_aging` bucket math +
  `_count_accounts_with_balance` cardinality), `CreditService._get_account` /
  `_compute_open_balance` (SQL `SUM(remaining_cents)`), and
  `DunningService._query_overdue_invoices` (status + `due_date <= cutoff`,
  ordered) fetch from `InvoiceReadRepository` / `AccountReadRepository`
  aggregation helpers. The two **cross-tenant background sweeps** —
  `invoice_overdue_job` (past-due open/partial) and `credit_override_expiry_job`
  (expired credit overrides) — read from dedicated `scan_*_all_tenants` repo
  methods (no tenant filter; each downstream `mark_overdue` / `expire_override`
  stays tenant-scoped). The append-only `account_events` / `dunning_events`
  ledger reads stay on ES by design. Verified byte-identical to ES against the
  live container (AR total and the cross-tenant past-due set matched exactly)
  with an ES guard.
  The invoice / account / dunning event ledgers + AR aging snapshots remain
  ES-served writes/reads for the append-only `*_events` streams + snapshot
  rollups.
- ~~Compliance config~~ ✅ done — `tax_jurisdictions`, `tax_exemptions`,
  `price_protection_contracts`, the compliance sell-side `pricing_rules`, and
  `supplier_contracts` are dual-written via hybrid document tables (typed index
  columns + verbatim ES document), covered by the backfill + parity check.
  Verified at 43/43 record parity against real data.
  **Read-cutover (pricing family):** `SalesPricingEngine.resolve_rule` sources
  its `pricing_rules` candidate rows from PG (`read_hybrid_search` on
  `compliance_pricing_rule` with the `product_code`+`status`+effective-date
  filter; the optional `expiry_date` upper bound is re-checked client-side, then
  the customer/account/product-default priority ordering runs unchanged).
  `PriceProtectionService.find_active_contract` and the tenant-wide
  `check_expiry` scan serve from PG (`read_hybrid_search` on
  `price_protection_contract`; the `[start_date, end_date]` window is
  re-checked client-side). The version-based CAS writes (`decrement_gallons`,
  status transitions) stay ES-authoritative during the soak — `_fetch_contract`
  deliberately still reads ES so the optimistic-concurrency verify loop sees its
  own writes — but `_write_status_transition` now also mirrors the terminal
  status to PG so a PG-served `check_expiry` scan does not re-transition the
  same contract every pass. `supplier_contracts` get/list serve from PG through
  the shared `_BaseTenantScopedRepository` cutover; list filters that map to a
  typed ORM column (`status` / `supplier_name` / `product_code`) translate to
  the PG `list`, while document-only filters (the `preferred_terminal_ids` JSON
  array) correctly fall back to ES so list semantics never silently change.
  Verified end-to-end against the live container with an ES guard.
- ~~Orders / jobs current-state~~ ✅ done — `fuel_orders_current`,
  `jobs_current`, `shipments_current`, `tenant_job_policies` are dual-written
  via hybrid document tables with a **stale-event guard** (rejects out-of-order
  upserts the way the ES scripted upsert does). Their `*_events` streams stay
  in ES. Write-mirror is wired for fuel orders (`save` + scripted upsert) and
  jobs (create + status transitions); `shipments_current` is covered by
  backfill + parity only (its ES scripted upsert is a server-side partial
  merge and is legacy/sunsetting, so a partial write-mirror would be lossy).
  Verified at 65/65 record parity against real data.
  **Read-cutover (orders/jobs lists):** `FuelOrderRepository.get` /
  `list_for_tenant` / `search` and `JobService._get_job_doc` / `list_jobs` /
  `get_active_jobs` / `get_delayed_jobs` now serve from Postgres when the flag
  is on. These list/search paths query the JSON `document` column directly (via
  `HybridReadRepository.search`) — NOT the typed mirror columns — because the
  typed `created_at` is the mirror-insert time, whereas ES sorts/filters on the
  document's business `created_at` / `scheduled_time`. Offset + total + term /
  `terms` / boolean / date-range filters all match the ES contract. JSON
  accessors (`as_string` / `as_boolean`) compile on both SQLite (tests) and
  Postgres (container). Verified end-to-end against the live container with an
  ES guard that fails if the ES read path is touched.
  **Read-cutover (scheduling metrics/analytics):** the `/scheduling/metrics/*`
  endpoints (`metrics/jobs` date-histogram, `metrics/completion`,
  `metrics/assets`, `metrics/delays`) now aggregate over Postgres job rows when
  the flag is on. Rather than push GROUP BY into portable SQL (which would
  diverge from the ES `date_histogram` calendar bucketing and the Python
  duration math), the cutover fetches the matching `jobs_current` documents via
  `read_hybrid_fetch_for_aggregation` and runs `job_metrics_aggregator` — a
  pure-Python module that reproduces the ES output exactly (empty interior
  buckets filled per `min_doc_count: 0`, `key_as_string` millisecond/`Z`
  format, terms-agg doc_count-desc ordering, identical completion/active-hours
  math). Verified byte-identical to the ES aggregations against the live
  container (same bucket count, same per-type/-asset numbers, including a
  pre-existing negative completion-time row that both paths compute the same).
  **Read-cutover (asset-cert list / tax engine / tenant policies / trucks):**
  asset_certifications **list** serves from PG via `read_hybrid_list_sorted`
  (keyset on the document `expiry_date asc, cert_id asc` — a document field, so
  it cannot use the typed-`created_at` keyset); the tax engine's
  `get_jurisdiction_rates` (FIPS `terms` rollup + effective/expiry date window,
  expiry-or-missing handled in Python) and `check_exemption` (customer +
  status=valid + expiry window, product-code scoping + priority selection
  reusing the existing candidate loop) serve from PG; `_get_tenant_policies`
  resolves the per-tenant `tenant_job_policies` row (keyed by tenant_id) from
  PG; and the legacy `trucks` `/fleet/trucks` list (asset_subtype==truck OR
  asset_type-missing predicate + created_at-desc sort reproduced in Python) and
  `/fleet/trucks/{id}` get serve from PG. The truck **update** path now also
  dual-writes to Postgres (previously only create did), so the read-cutover
  cannot serve a stale row after a partial update. All verified byte-identical
  to ES against the live container with an ES guard.
  **Read-cutover (fleet dashboard summary + assets alias):** `GET /fleet/summary`
  (trucks match_all scan → status counts + the `assets`-alias `by_type` /
  `by_subtype` terms aggs + active/delayed filters) and the multi-asset
  endpoints `GET /fleet/assets` (asset_type / asset_subtype / status term
  filters, created_at-desc) + `GET /fleet/assets/{id}` now serve from PG. The
  `assets` index is an ES **alias onto `trucks`**, so all of these read the one
  migrated `truck` aggregate: a single `read_hybrid_fetch_for_aggregation`
  pull, with the `by_type` / `by_subtype` rollups reproduced in Python in the
  ES terms-agg order (doc_count desc, then key asc). Because `truck` is
  tenant-optional (`fetch_for_aggregation` does not tenant-filter), the
  endpoints apply an explicit `tenant_id` match so the result matches the ES
  `inject_tenant_filter` exactly (legacy null-tenant docs excluded); the by-id
  paths add the same guard so a tenant-optional `get` cannot leak a cross-tenant
  asset. Verified byte-identical to the ES rollups against the live container
  (total 10, on_time 8, delayed 2, by_type `{vehicle:10}`, by_subtype
  `{truck:6, tanker:2, van:2}`).
  **Stays on ES — agent fleet semantic search:** `Agents/tools/search_tools.py`
  `search_fleet_data` uses an ES `multi_match` full-text relevance query across
  `cargo.description` / `driver_name` / `asset_name` / `vessel_name` / etc.
  This is genuine full-text search (relevance-ranked), not a structured filter,
  so it is intentionally left reading the ES `trucks` projection — reproducing
  ES relevance scoring in Postgres would diverge. The `trucks` index therefore
  cannot be dropped while this tool is in use; it remains a search/read
  projection by design.
  **Asset-cert status-transition write-mirror:** the expiry sweep's
  `_transition_to_expiring_soon` / `_transition_to_expired`, the
  `_clear_dispatch_restriction` supersede, and `update_status` previously wrote
  the new status to ES only — leaving the PG row at `valid` and causing a
  read-cutover/parity drift. They now mirror the change to PG via
  `mirror_current_state_fields` (a new `CurrentStateRepository.set_fields` that
  merges the partial into the verbatim `document` + typed `status` column), so
  PG stays the source-of-truth and the next PG-served scan does not re-fire the
  transition. Verified: parity returned to 106/106 after reconciling the
  pre-existing CERT-001/CERT-002 drift through this same path.
  **Read-cutover (tax endpoint list handlers):** `GET /api/compliance/tax-jurisdictions`
  (fips_code / tax_type term filters + effective_date<=iso, with the
  "expiry >= iso OR missing" open-ended-row rule applied in Python) and
  `GET /api/compliance/exemptions` (customer_id + status=valid term filters +
  expiry_date>=iso, with the "product_codes contains X OR blanket/missing" rule
  in Python) now serve from PG via `read_hybrid_fetch_for_aggregation` over the
  `tax_jurisdiction` / `tax_exemption` hybrid aggregates — the same back-end the
  tax engine already reads. Verified byte-identical to ES against the live
  container (4 jurisdictions, `?tax_type=excise` → 3, 3 exemptions).
  **Two pre-existing write-mirror bugs surfaced + fixed during this batch (both
  exposed once the overdue sweep + invoice get were PG-backed):**
  1. `InvoiceService.mark_overdue` wrote `status=overdue` to ES only (the
     sibling finalize / pay / void transitions all mirror via
     `mirror_invoice_fields`, but overdue was missed). With the invoice get +
     overdue sweep now PG-backed, the stale PG `open`/`partial` status caused
     the hourly cron to re-mark the same invoices forever (14 duplicate
     `overdue_marked` events accrued). Fixed by adding the matching
     `mirror_invoice_fields` call.
  2. `invoices_current` is `dynamic: strict` but did **not** declare
     `_last_applied_seq`, which `_update_projection` stamps on every status
     transition — so the projection write was rejected outright and the status
     never persisted in ES either. Fixed by declaring the field in
     `INVOICES_CURRENT_MAPPING` (+ an additive `put_mapping` on the live index);
     `parity_check` already treated it as a known ES-only projection field.
  Also fixed the analogous `scheduling.delay_detection_service` gap: marking a
  job `delayed=True` wrote to ES only, so a PG-served `get_delayed_jobs` /
  delay-metrics read disagreed — it now mirrors via `mirror_current_state_fields`.
  Reconciled the accumulated dev drift (duplicate events deduped, INV-0001 /
  INV-0004 transitioned to overdue in both stores, JOB-007 delayed flag synced)
  → parity back to 106/106. Regression tests added:
  `test_mark_overdue_mirrors_status_to_postgres` and
  `test_invoices_current_mapping_declares_last_applied_seq`.
  **Read-cutover (agent lookup tools + locations):** the `get_all_locations`
  and `find_truck_by_id` agent tools now serve from PG via
  `read_hybrid_fetch_for_aggregation` over the `location` / `truck` aggregates
  (both tenant-optional, so an explicit `tenant_id` match mirrors the ES
  `inject_tenant_filter`). Wiring these surfaced a cluster of pre-existing bugs
  on the legacy `locations` index:
  1. `locations` was created with **dynamic mapping**, so its `tenant_id` is
     `text` (not `keyword`). A `term: {tenant_id}` query matches nothing on a
     text field — so `get_all_locations`' ES path already returned 0, AND the
     backfill/parity tools silently saw ES=0 (a blind spot). Fixed `parity_check`
     to fall back to `tenant_id.keyword` then a client-side filter (so it sees
     the same rows the app serves), and backfilled the 4 master locations into
     PG. Parity rose 106 → 110 (locations now visible + in sync).
  2. `get_all_locations` read `type` / `name` / `region` but the docs carry
     `location_type` / `location_name` / `address` — it rendered "Unknowns /
     None" on BOTH paths. Fixed the tool to accept both field shapes.
  3. `parity_check._fetch_es_all` now clears its scroll contexts (the serverless
     cluster caps open scroll contexts; the multi-query fallback would otherwise
     exhaust them). *Both went with the cluster:* the function is
     `_fetch_documents_all` and reads `es_documents` with one `SELECT`, so there is
     no scroll and no `tenant_id.keyword` fallback chain.
  **Live-position write-mirror:** the ingestion location-update path
  (`ingestion/service.py`) and the Geotab connector wrote `current_location` to
  the ES `trucks` doc only. Now that the fleet reads serve from PG, both mirror
  the position fields to PG via `mirror_current_state_fields` (a partial
  field-merge — notably safer than the ingestion path's ES `index_document`,
  which full-replaces the doc).
  **Stays on ES by design:** the agent fleet *semantic* search
  (`search_fleet_data`, `multi_match` relevance) and the `search_orders` /
  `search_support_tickets` / `search_inventory` semantic tools — full-text
  relevance search is an ES strength, not a structured filter PG can reproduce.
- **Agents layer — autonomous monitor sweeps (in progress)** ✅ first chunk done.
  The background monitor agents run a single **cross-tenant** ES sweep then
  dispatch per-tenant internally. A new `HybridReadRepository.search_all_tenants`
  + `read_hybrid_search_all_tenants` bridge helper reproduces that pattern (same
  term / terms / bool / range / exists filters + document-field sort, WITHOUT
  the tenant clause; system-level only — request-path reads stay tenant-scoped).
  Cut over: `JobSLAMonitor` (in_progress + `estimated_arrival<=threshold`),
  `DelayResponseAgent` (in_progress + `estimated_arrival<now`; its
  `_find_available_asset` truck lookup uses tenant-scoped `read_hybrid_search`),
  `SLAGuardianAgent` (in_transit shipments + `estimated_delivery<=threshold`),
  and `TruckFuelMonitor` (fetches trucks cross-tenant then applies the
  **numeric** `fuel_level_pct` threshold in Python — the PG JSON range helper
  compares as strings, correct for ISO dates but NOT numbers). Verified
  byte-identical to ES against the live container (in_progress jobs
  `[JOB-007, JOB-008, JOB-009]` on both). Remaining agent reads (overlay
  optimizers, tank/route/compartment agents reading `fuel_stations` /
  `mvp_tank_forecasts` / `customer_tanks` / `truck_compartments`, and the
  agent memory / approval-queue stores) are NOT yet cut over — several read
  un-migrated indices (those stay ES by design); the migrated-aggregate reads
  among them are the next chunk.
- **Agents layer — overlay optimizers (jobs_current readers)** ✅ done. The
  signal-driven overlay optimizers' `jobs_current` reads now serve from PG:
  `DispatchOptimizer._query_affected_jobs` (in_progress + `terms job_id`) and
  `._query_available_assets` (on_time trucks, tenant-scoped via a document
  `tenant_id` term since `truck` is tenant-optional); `OutcomeTracker._measure_kpis`
  (`terms job_id`); `RevenueGuard._compute_route_margins` (completed/delivered +
  `completed_at >= now-7d`, with the ES date-math resolved to a concrete ISO
  cutoff for the PG string range); `JobPriorityEngine._query_active_jobs`
  (scheduled/assigned/in_progress, sorted by `estimated_arrival`); and
  `DriverNudgeAgent` (cross-tenant assigned + `assigned_at <= cutoff`, with the
  ES `must_not driver_acked=True` negation applied in Python). Verified
  byte-identical to ES against the live container (active jobs JOB-001..009 on
  both paths). Remaining: the tank/route/compartment agents read un-migrated
  indices (`fuel_stations`, `mvp_tank_forecasts`, `customer_tanks`,
  `truck_compartments`, `mvp_*`) which stay on ES by design; `route_planning_agent`
  reads `fuel_orders_current` (migrated) — a candidate for a later chunk.
- **Agents layer — fuel_orders_current readers** ✅ done. The remaining migrated
  `fuel_orders_current` agent reads now serve from PG:
  `RoutePlanningAgent._fetch_routable_orders` (confirmed/scheduled) +
  `._get_driver_for_truck` (by-id truck lookup, tenant-guarded);
  `DeliveryPrioritizationAgent._fetch_pending_orders` (placed/confirmed/scheduled,
  tenant-scoped) + `._discover_tenants_with_pending_orders` (the ES tenant
  terms-agg reproduced by a cross-tenant fetch + distinct tenant_ids in Python);
  and `CompartmentLoadingAgent._query_fuel_orders` (loadable statuses,
  tenant-scoped). Verified byte-identical to ES against the live container
  (routable orders ORD-0001..0006 on both paths). With this, **every migrated
  aggregate read in the autonomous + overlay agent layers is cut over**
  (`jobs_current`, `fuel_orders_current`, `shipments_current`, `trucks`). What
  remains ES-only in the agents is by design: un-migrated indices
  (`fuel_stations`, `mvp_tank_forecasts`, `customer_tanks`, `truck_compartments`,
  `mvp_load_plans`, `mvp_routes`, `inventory`, `fuel_events`, `rack_prices`,
  `storm_*`, `meter_registry`), the agent memory / approval-queue stores, the
  `*_events` streams, and full-text semantic search.
  **Read-cutover (ops shipments):** the ops shipment reads now serve from PG —
  `GET /ops/shipments` (list), `/ops/shipments/{id}` (get; event history still
  reads the un-migrated `shipment_events` from ES), `/ops/shipments/sla-breaches`
  (exists `estimated_delivery` + past-due, via `search` `exists_fields` +
  exclusive `range_lt`), `/ops/shipments/failures` (failed shipments from PG;
  per-row `failure_reason` enrichment still reads `shipment_events`), and the
  metrics `GET /ops/metrics/shipments` / `/ops/metrics/sla` / `/ops/metrics/failures`
  (Python `shipment_metrics_aggregator` reproducing the ES `date_histogram` +
  status/reason terms + the SLA painless-script breach rule). The shipment
  **write path** now also dual-writes: `OpsElasticsearchService.upsert_shipment_current`
  mirrors to Postgres after a non-stale ES write (the repo's stale-event guard
  matches the ES scripted upsert). `/ops/riders*` stays on ES (riders are not a
  migrated aggregate). NB: the ES scripted upsert no-ops fresh inserts on the
  current serverless ES (a pre-existing ES quirk, identical on both paths).
  `terminals`, `asset_certifications`, `intake_channels`, `trucks`, `locations`
  are dual-written via hybrid document tables. Write-mirror is wired for
  drivers (create), asset certifications (create), depots (create + update),
  terminals (base repo create/update), and intake channels (create / update /
  rotate-secret) and trucks (legacy generic-ES create); `locations` is
  seed-only so it is covered by backfill + parity. Verified at 100/100 record
  parity against real data.
- **Phase 5 — stop the ES projection.** After a parity soak with reads on
  Postgres, stop running the outbox relay (and/or turn off dual-write) so the
  migrated ES indices stop receiving writes.
- **Phase 6 — retire the index.** Add it to `RETIRED_ES_INDICES` (see below) so
  nothing projects to it any more. Note: keep the index's **mapping** in its
  `*_es_mappings.py` registry. The reason changed when Elasticsearch went — the
  rebuild tool no longer creates indices, because the document store is one table
  with no per-index typing — but `persistence/document_field_policy.py` reads those
  mappings to decide which fields must stay unqueryable, and in a `jsonb` column
  everything is queryable unless something says otherwise. Only remove a
  mapping/seeder entry when an aggregate is being deleted entirely. The deprecated
  `invoice_numbering.py` + its `invoice_counter_checkpoints` index were fully
  removed since the Postgres counter replaced them outright.

### Drift repair: rebuild-from-Postgres (`persistence.rebuild_document_store`)

The migrated indices are *projections* of the relational tables, so they can be
reconstructed. `rebuild_document_store` is the inverse of `backfill`: it reads every
row for an aggregate, runs it through the SAME projector the relay uses
(`persistence.projections.PROJECTORS`), and writes each document **verbatim** —
`index_document(..., stamp_timestamps=False)`, because the default rewrites
`updated_at` to now() and would diverge the field from the value stored on the row.

Two things it used to do are gone with the cluster: recreating the index with its
declared strict mapping (the document store has no index to create, and no
per-index typing to get wrong), and refreshing afterwards (a write is visible to the
next read). It also used to be the *reversibility safety net* for dropping an ES
index; a dropped index is not a thing any more, so its remaining job is repairing
drift that `persistence.parity_check` has found.

```bash
# Rebuild one index from Postgres:
ENVIRONMENT=development ./venv/bin/python -m persistence.rebuild_document_store \
    --aggregate intake_channel --tenant demo-tenant
# Rebuild every migrated aggregate's index:
ENVIRONMENT=development ./venv/bin/python -m persistence.rebuild_document_store \
    --all --tenant demo-tenant     # add --dry-run to report counts only
```

### Proven reversible drop runbook (Phase 5→6 per index)

> **Historical.** This procedure operated on Elasticsearch indices and there is no
> cluster to drop from. Retiring an aggregate today is the `RETIRED_ES_INDICES` gate
> below and nothing else. Kept because steps 1 and 5 are still the right shape for
> retiring one, and because of the lesson at the end.

Demonstrated end-to-end on `intake_channels` against the live cluster:

1. Confirm the index's reads are cut over to PG (get/list/search/metrics) and
   its writes mirror to PG (so a rebuild loses nothing). Run `parity_check`.
2. `DELETE` the ES index.
3. Confirm the app's read path still works — it now serves from Postgres with
   the index gone (this is the whole point of the read-cutover).
4. `rebuild_document_store --aggregate <agg>` to reconstruct it (or leave it
   dropped for good once you no longer need the ES search/dashboard surface).
5. `parity_check` → `PARITY OK`.

**Lesson learned during the POC:** a dynamically-typed recreate broke `tenant_id`
`term` filtering — it landed as `text`, so a tenant-scoped query matched nothing
while the rebuild logged every document indexed and exited 0. The rebuild tool
handled it by looking the mapping up from the registries. That whole failure mode
belonged to Elasticsearch: the document store keys on `(index_name, doc_id)` with a
`varchar` tenant column, so there is no typing decision to get wrong.

### Retiring an index: the `RETIRED_ES_INDICES` gate

Set `RETIRED_ES_INDICES` (comma-separated, or a JSON array) to the index names whose
relational table is their sole store. This gate, read inside
`ElasticsearchService`, makes `index_document` / `update_document` /
`delete_document` **skip** those indices — so the direct service writes AND the
outbox-relay projection (which calls `index_document`) become no-ops.
`parity_check` skips retired indices. Fully reversible: remove the name from
`RETIRED_ES_INDICES` and `rebuild_document_store --aggregate <agg>` to repopulate.

What the gate protects against changed with the cluster. It used to stop the app
recreating a dropped index with dynamic mappings — the startup index-setup would
otherwise have done exactly that, and `setup_order_intake_indices` was the specific
one to watch. Those functions are deleted. The remaining cost of not setting it is
redundant rows accumulating in `es_documents` that no read path consults, which is
cheaper but still worth avoiding: they are a second copy that can drift.

Before retiring an index, audit EVERY consumer — not just the obvious get/list.
The `intake_channels` retirement surfaced two extra read paths that had to be
cut over first: `get_by_channel_id` (the **unauthenticated webhook** ingestion
resolves the tenant FROM the channel) and `get_dispatcher_channel` (`find_one`
on a document field), plus a service-level `delete` that had to remove the PG
source-of-truth row (`mirror_current_state_delete`) and a webhook-seed write
that had to mirror to PG.

#### Done: `intake_channels` retired (2026-06-02)

`intake_channels` is the first index taken all the way through Phase 6. All
read paths (`get`, `list_for_tenant`, `get_by_channel_id`,
`get_dispatcher_channel`) serve from Postgres; create/update/rotate mirror to
PG and `delete` removes the PG row; the ES index is dropped and listed in
`RETIRED_ES_INDICES`. Verified against the live cluster: index stays dropped
across a restart, reads + webhook resolution serve from PG, writes return
`skipped_retired_index` without recreating the index, and `parity_check` is
green (95 records across the remaining indices). To undo:
`RETIRED_ES_INDICES=` (drop it from the list), restart, then
`python -m persistence.rebuild_document_store --aggregate intake_channel`.

#### Done: `supplier_contracts` retired (2026-06-04)

The second index taken to Phase 6, and the first of the pricing family. Reads
(`get` / `list_for_tenant` via the shared `_BaseTenantScopedRepository` cutover)
serve from Postgres; create/update mirror to PG via `mirror_compliance_config_upsert`.
Dropped from the live cluster and added to `RETIRED_ES_INDICES`. Verified
end-to-end: pre-drop PG=2 ES=2; after `DELETE` the repo still served
`list_for_tenant(active)` → `[SUPP-001, SUPP-002]` and `get(SUPP-001)` from PG
with the index gone; a `repo.create` after retirement returned
`skipped_retired_index` from ES yet **persisted to PG** (read back from PG);
the index stayed dropped across a restart; `parity_check` skips it and is green
(108 records). Reversibility proven live: `rebuild_document_store --aggregate
supplier_contract` recreated it byte-identically with the correct **strict**
mapping (`tenant_id` `keyword`, `dynamic: strict`), then it was re-dropped.
**Gap found + fixed during this drop:** `setup_fuel_ops_indices` did not honor
the `RETIRED_ES_INDICES` gate (only the order-intake setup did), so the first
restart silently recreated the dropped index. Added the same retired-index skip
guard (regression test: `test_skips_retired_indices`). To undo:
`RETIRED_ES_INDICES=intake_channels` (drop it from the list), restart, then
`python -m persistence.rebuild_document_store --aggregate supplier_contract`.

#### Done: pricing family retired (2026-06-04)

`price_books_current`, `pricing_rules_current` (commerce) and `pricing_rules`
(the compliance sell-side rules) taken to Phase 6. Reads were already cut over
(`PriceBookService.get`/`list`/`_get_rules_for_book`, `PricingEngine`, and
`SalesPricingEngine.resolve_rule`); the last ungated ES readers were wired
before dropping:
- `commerce/api/pricing_endpoints.py::list_pricing_rules` → PG via
  `read_hybrid_fetch_for_aggregation("compliance_pricing_rule", …)`.
- `PriceBookService._remove_rules_for_book` (a write-path read-before-delete)
  now sources the rule_ids from PG, so a price-book update works after the
  index is gone (`search_documents` is NOT retire-gated and would otherwise
  error on a dropped index).
Dropped from the live cluster and added to `RETIRED_ES_INDICES`. Verified
end-to-end with the indices gone: a `PriceBookService.create` (book + fan-out
rule) persisted to PG with ES gated, `get`/`list` served from PG, and
`PricingEngine.resolve` returned the rule from the PG candidate set; all three
indices stayed dropped across a restart; `parity_check` skips them and is green
(108 records). `price_protection_contracts` is intentionally **kept** — its
`_fetch_contract` still reads ES inside the version-based CAS decrement loop.
**Gap found + fixed:** the `RETIRED_ES_INDICES` startup gate was only honored by
`setup_order_intake_indices` and (just before) `setup_fuel_ops_indices`;
`setup_commerce_indices` and `setup_compliance_indices` did not check it and
would have recreated the dropped indices on the next restart. Added the same
retired-skip guard to both (regression test: `test_skips_retired_indices` in
the compliance + fuel-ops mapping suites). The programmatic seeders are now
also retirement-safe centrally: `seed_all_data._bulk` / `_single` drop writes
to any retired index. To undo any of these:
`RETIRED_ES_INDICES=…` (remove the name), restart, then
`python -m persistence.rebuild_document_store --aggregate price_book|pricing_rule|compliance_pricing_rule`.
- **Migration scope complete** — all recommended source-of-truth domains
  (commerce, compliance config, orders/jobs current-state, master data) now
  dual-write to Postgres with outbox→ES projection, backfill, and parity. The
  remaining ES indices (event streams, telemetry, agent/ML, search/dashboard
  projections, notifications/queues) stay in Elasticsearch by design.

### Dev-environment hardening (post-migration journey test, 2026-06-03)

An end-to-end operator-journey test (create customer → account → job lifecycle →
intake-channel registration → analytics) surfaced three issues — none caused by
the migration, all fixed at the correct layer:

1. **`account_events` / `invoice_events` mapping rejected projections (503).**
   Those indices mapped `payload` as a strict object and lacked the
   `created_at` / `updated_at` / `sequence_number` fields that
   `ElasticsearchService.index_document` auto-stamps. Fixed in
   `commerce/services/commerce_es_mappings.py` (`payload` → `enabled: false`,
   added the missing date/seq fields) and the two empty indices were recreated
   with the new mappings.

2. **Intake-channel registration 500 (`kms_key_id required`) off-AWS.**
   Registering a channel stores its HMAC secret in `TenantCredentialsVault`,
   which needs a KMS key. Dev/CI have no `FUEL_OPS_KMS_KEY_ID` (and no AWS
   creds). Added `services/local_kms.py` — a `LocalKMSClient` implementing the
   boto3 KMS subset the vault uses (`generate_data_key` / `decrypt`) with real
   AES-GCM envelope encryption under a process-local master key
   (`LOCAL_KMS_MASTER_KEY`, stable dev default so blobs survive restarts).
   `bootstrap/agents.py` injects it whenever no `FUEL_OPS_KMS_KEY_ID` is set and
   the environment is not `production`; the real-KMS path is unchanged. Verified
   end-to-end: register → 201 + one-time plaintext secret, channel persists to
   the PG `intake_channels` table, list serves from PG, and rotate-secret
   round-trips the vault against the live ES cluster.

3. **Parity false-positive on event `created_at` / `updated_at`.** Event and
   snapshot projections key off the domain timestamp (`occurred_at` /
   `queued_at`) and have no `created_at` / `updated_at` Postgres column, but ES
   stamps them onto `_source` at write time — so every projected event showed a
   spurious divergence. Added them to `parity_check`'s per-aggregate ignore set
   for `invoice_event` / `account_event` / `dunning_event` /
   `ar_aging_snapshot` (same class as the existing invoice `_last_applied_seq`
   exclusion). `parity_check` is green at 106/106 records.


### Dispatcher journey hardening (manual + agentic E2E, 2026-06-03)

A second end-to-end test simulating a fuel dispatcher's day — in BOTH the manual
REST/UI flow and the agentic AI-assistant flow — surfaced six more pre-existing
defects (none migration-related; all fixed at the correct layer). The full
journey now passes 21/21 and `parity_check` stays green at 106/106.

1. **Adapter registry never populated (every order intake failed).**
   `bootstrap/fuel.py` called `IntakeAdapterRegistry.register("dispatcher",
   "1.0", adapter)` positionally, but `register(adapter, *, channel_type,
   schema_version)` takes the type/version as KEYWORD-only args. The resulting
   `TypeError` was swallowed by a broad `except`, leaving the registry empty so
   every dispatcher/CSV/EDI/partner order 500'd with "No adapter registered".
   Fixed all four registration calls to pass keyword args.

2. **OrderIntakePipeline wired with `None` deps (boot-order bug).** The pipeline
   is built in `bootstrap/fuel.py` (#5) but `intake_channel_repository` (#11) and
   `credentials_vault` (#10) are registered later — so it kept
   `intake_channel_repo=None` and every dispatcher create 500'd
   (`'NoneType' has no attribute 'get_dispatcher_channel'`). Added
   `set_intake_channel_repo` / `set_credentials_vault` setters and late-inject
   them from `bootstrap/integrations.py` (also re-points the webhook receiver).

3. **No dispatcher intake channel existed.** `_resolve_dispatcher_channel` only
   looked up a `channel_type="dispatcher"` channel and 404'd when absent (the
   docstring claimed "seeded at first use" but nothing created it). Added
   `IntakeChannelRepository.ensure_dispatcher_channel` — idempotently provisions
   a stable `{tenant}-dispatcher` channel on first use.

4. **Poison-queue error path crashed (masking real errors).** `poison_queue.py`
   used `self.ops_es.es_service` (no such attribute) and `await
   es.client.X(...)` on the SYNC ES client. Fixed every method to
   `self.ops_es.client.X(...)` without await. The same sync-client misuse was
   fixed across `legacy_mirror_backfill_worker.py` (it was fully broken in
   production — every `await client.search/get/delete/update` raised
   "object ... can't be used in 'await' expression"); its unit tests were
   mocking the client as async and so never caught it — now mocked as the real
   sync client.

5. **`fuel_order_events` mapping rejected projections (503).** The index mapped
   `event_payload` as a strict `nested` object, but order events carry free-form
   payloads (`intake_channel`, `dispatcher_user_id`, …) → strict-dynamic
   rejection. Remapped to `{"type":"object","enabled":false}` (same as
   job_events / account_events). Also added `fuel_order_events` to
   `ElasticsearchService.index_document`'s `_TIMESTAMP_SKIP_INDICES` — it's a
   strict event-stream index that rejected the auto-stamped
   `created_at`/`updated_at`. Empty index recreated with the corrected mapping.

6. **Fresh orders persisted to NEITHER store (read-after-write 404).** On the
   serverless ES the `scripted_upsert` staleness-guard ran on a fresh insert,
   compared the incoming timestamp to itself, and set `ctx.op='noop'` — the
   `upsert` body was never materialised and the PG mirror (gated on
   `result != "noop"`) was skipped, so a 201 create was immediately a 404 read
   (reads served from PG). `FuelOrderRepository.upsert_with_last_event_timestamp`
   now detects a noop with no existing doc and indexes the document directly,
   then mirrors to Postgres.

7. **Agent fallback chat crashed (`Agent.run_async` AttributeError).** The
   non-streaming `/api/chat/fallback` path called `self.agent.run_async(...)`;
   the Strands `Agent` exposes `invoke_async` (returning an `AgentResult` whose
   `__str__` is the text). Fixed to `await self.agent.invoke_async(message)` then
   `str(result)`. The agentic dispatcher questions now return grounded,
   data-backed answers.

Dev config note: the order-intake pipeline is gated behind the
`overlay.order_intake_pipeline` feature flag (rollout gate, default `disabled`).
For demo-tenant it is set to `active_auto` (via `FeatureFlagService`) so the
dispatcher keyboard path creates orders through the new pipeline instead of
short-circuiting to `legacy_passthrough`.
