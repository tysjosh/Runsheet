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
  row lock inside the finalize transaction). The legacy module is deprecated.
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
- ~~Master data~~ ✅ done — `drivers` (compliance CDL/medical), `depots`,
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
- **Phase 6 — drop the ES indices.** Delete the migrated indices, remove them
  from the seed registry + mapping modules, and delete the deprecated
  `invoice_numbering.py` and `invoice_counter_checkpoints` index.
- **Migration scope complete** — all recommended source-of-truth domains
  (commerce, compliance config, orders/jobs current-state, master data) now
  dual-write to Postgres with outbox→ES projection, backfill, and parity. The
  remaining ES indices (event streams, telemetry, agent/ML, search/dashboard
  projections, notifications/queues) stay in Elasticsearch by design.

```
