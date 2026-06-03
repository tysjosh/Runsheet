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
  (outbox-projected), covered by the backfill + parity check. Reads for these
  are not yet routed through Postgres (the four core aggregates are the ones
  wired for read-cutover); add read repos for them before dropping their ES
  indices.
- ~~Compliance config~~ ✅ done — `tax_jurisdictions`, `tax_exemptions`,
  `price_protection_contracts`, the compliance sell-side `pricing_rules`, and
  `supplier_contracts` are dual-written via hybrid document tables (typed index
  columns + verbatim ES document), covered by the backfill + parity check.
  Verified at 43/43 record parity against real data.
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
- **Phase 6 — drop the ES indices.** Drop the migrated index and add it to
  `RETIRED_ES_INDICES` (see below). Note: keep the index's **mapping** in its
  `*_es_mappings.py` registry — `rebuild_from_postgres` needs it to recreate
  the index with the correct strict mapping if you ever rebuild. Only remove a
  mapping/seeder entry when an aggregate is being deleted entirely (not just
  retired from ES). The deprecated `invoice_numbering.py` + its
  `invoice_counter_checkpoints` index were fully removed since the Postgres
  counter replaced them outright.

### Reversibility safety net: rebuild-from-Postgres (`persistence.rebuild_from_postgres`)

Once reads are cut over, the migrated ES indices are disposable *projections*,
so dropping one must be reversible. `rebuild_from_postgres` is the inverse of
`backfill`: it reads every PG row for an aggregate, runs it through the SAME
projector the relay uses (`persistence.projections.PROJECTORS`), recreates the
index with its **correct strict mapping** (looked up from the domain mapping
registries — NOT ES dynamic typing, which would silently make `tenant_id`
`text` and break `term` queries), and indexes each doc **verbatim** (it writes
via the raw ES client so `index_document`'s `updated_at = now()` rewrite does
not diverge the field from the PG-stored value), then refreshes.

```bash
# Rebuild one index from Postgres:
ENVIRONMENT=development ./venv/bin/python -m persistence.rebuild_from_postgres \
    --aggregate intake_channel --tenant demo-tenant
# Rebuild every migrated aggregate's index:
ENVIRONMENT=development ./venv/bin/python -m persistence.rebuild_from_postgres \
    --all --tenant demo-tenant     # add --dry-run to report counts only
```

### Proven reversible drop runbook (Phase 5→6 per index)

Demonstrated end-to-end on `intake_channels` against the live cluster:

1. Confirm the index's reads are cut over to PG (get/list/search/metrics) and
   its writes mirror to PG (so a rebuild loses nothing). Run `parity_check`.
2. `DELETE` the ES index.
3. Confirm the app's read path still works — it now serves from Postgres with
   the index gone (this is the whole point of the read-cutover).
4. `rebuild_from_postgres --aggregate <agg>` to reconstruct it (or leave it
   dropped for good once you no longer need the ES search/dashboard surface).
5. `parity_check` → `PARITY OK`.

The drop is only truly final once nothing reads the index. Re-running the
rebuild restores it byte-identically at any time, so the operation is safe to
rehearse. **Lesson learned during the POC:** always recreate the index with its
registered mapping — a dynamically-typed recreate breaks `tenant_id` `term`
filtering (it lands as `text`). `rebuild_from_postgres` handles this.

### Permanent drop: the `RETIRED_ES_INDICES` gate

A *permanent* Phase 6 drop needs one more thing than the rehearsal: a way to
stop the app from recreating the dropped index. Set `RETIRED_ES_INDICES`
(comma-separated, or a JSON array) to the dropped index names. This gate, read
inside `ElasticsearchService`, makes `index_document` / `update_document` /
`delete_document` **skip** those indices — so the direct service writes AND the
outbox-relay projection (which calls `index_document`) become no-ops, and the
startup index-setup (`setup_order_intake_indices`) skips recreating them.
`parity_check` skips retired indices (Postgres is their sole store). Fully
reversible: remove the name from `RETIRED_ES_INDICES` and
`rebuild_from_postgres --aggregate <agg>` to bring the index back.

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
`python -m persistence.rebuild_from_postgres --aggregate intake_channel`.
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
