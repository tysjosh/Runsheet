# Backup and restore

**One store. A `pg_dump` is the whole backup.**

That is the single biggest change the Elasticsearch removal made to this document.
There used to be two stores to think about and a rule for which one could be
reconstructed from the other; now the relational tables and the document plane
(`es_documents`) are in the same database and the same dump. Nothing needs
re-projecting after a restore, because the projection is *in* the restore.

Everything here is executable: `Runsheet-backend/scripts/backup_restore.sh`.
The `drill` subcommand performs a real dump → restore → row-count comparison, and
runs on every pull request in the `migration-check` job. Before it existed there
was no restore procedure and nobody had restored this database.

## What is authoritative, and what is not

| Table group | Role | Recoverable from |
|---|---|---|
| Relational (commerce/financial records, orders and jobs current-state, master data, idempotency keys, invoice counters) | Source-of-truth | The dump. Nothing else. |
| `es_documents` | Document plane: everything reads and writes documents here. Projected aggregates are fed by the transactional outbox | The dump. Projected indices can additionally be rebuilt with `python -m persistence.rebuild_document_store --all` |

The second row is worth reading twice: `es_documents` is **not** purely a
projection. Indices with no relational table behind them — the majority of the 103
— hold their only copy there. The rebuild tool covers the ~28 aggregates in
`_REBUILD_SPECS`; the dump covers everything.

### The Elasticsearch cluster is gone

The final whole-cluster export is at `Runsheet-backend/es-full-backup/`: 103
indices, 7,623 documents, 7.1 MB, verified, gitignored. It is the only remaining
copy of the cluster and is deliberately kept. Everything in it was copied into
`es_documents` and checked at 1,426 identical query comparisons across all 103
indices before the cluster was deleted — see
[docs/elasticsearch-to-postgres-migration.md](elasticsearch-to-postgres-migration.md).

`scripts/es_only_backup.py`, which produced that export, was deleted with the
cluster.

### Three indices used to be unrecoverable. They are not any more.

This is history now — there is no cluster to lose — but it is the reason the
fuel-asset tables exist and the reason
`tests/unit/test_fuel_asset_postgres_homes.py` guards them, so it stays recorded.

`customer_tanks`, `truck_compartments` and `fuel_stations` held authoritative
operational state with no Postgres table, no ORM model and no projector behind
them. Losing the Elasticsearch cluster lost the data outright, and a Postgres dump
did not cover it. That is not hypothetical: an end-to-end test of the MVP pipeline
recreated the cluster, and the A1 tank-forecasting and A3 compartment-loading
stages then ran with no input while `POST /api/fuel/mvp/plan/generate` still
reported `status: "complete"`.

They were never seed data. `KFactorCalibrationService` writes calibrated
`k_factor` values back into `customer_tanks`; the Veeder-Root ATG connector
updates tank levels in `customer_tanks` and `fuel_stations`; and
`CompartmentLoadingAgent` writes `last_loaded_product` into `truck_compartments`,
which is what the cross-contamination guard reads before assigning a product to a
compartment. Without it the guard cannot tell that a compartment last carried
diesel before loading gasoline — so losing that field does not merely lose data,
it removes the evidence a safety check depends on.

Migration `0008_fuel_asset_tables` gives all three a Postgres table with the
established hybrid shape (typed filter columns plus the verbatim document, in
`jsonb`). Each has a passthrough projector, a `_REBUILD_SPECS` entry, a backfill
and dual-write from every writer, so `rebuild_document_store --all` restores them
like anything else.

Verified by drill against the dev cluster while it still existed — drop the index,
rebuild from Postgres, compare:

| Index | Documents | Bodies | Mapping | Tenant-scoped `term` |
|---|---|---|---|---|
| `customer_tanks` | 6 | identical | all filter fields `keyword` | 6/6 |
| `truck_compartments` | 9 | identical | all filter fields `keyword` | 9/9 |
| `fuel_stations` | 14 | identical | all filter fields `keyword` | 14/14 |

The mapping column in that table is worth explaining, because the failure it
records cannot happen any more and the reason is instructive. The first run of that
drill rebuilt `truck_compartments` with a **dynamic** mapping: the rebuild tool's
`_lookup_mapping` consulted five mapping registries and the `truck_compartments`
mapping lived in a sixth. `tenant_id` came back as `text` instead of `keyword`, so a
tenant-scoped `term` query matched **0 of 9** documents while the rebuild logged
nine indexed and exited 0.

The document store has no index to create and no per-index typing — it is one table
keyed `(index_name, doc_id)` with a `varchar` tenant column — so index creation and
mapping lookup were deleted from the rebuild tool along with
`tests/unit/test_rebuild_mapping_coverage.py`, whose premise was exactly this. The
mapping *registries* survive for a different reason: the field policy reads them to
keep declared-unsearchable fields unqueryable.

## Commands

```sh
cd Runsheet-backend

# Take a dump. Verifies the archive is readable before reporting success —
# a dump nobody has read back is a hope, not a backup.
scripts/backup_restore.sh dump "$DATABASE_URL" runsheet-$(date -u +%Y%m%dT%H%M%SZ).dump

# Check an existing dump is intact.
scripts/backup_restore.sh verify runsheet-20260804T120000Z.dump

# Restore. DESTRUCTIVE: drops the objects the dump recreates.
scripts/backup_restore.sh restore "$DATABASE_URL" runsheet-20260804T120000Z.dump

# Prove a restore works without touching the live database: dumps, restores into
# a scratch database, compares per-table row counts, drops the scratch.
scripts/backup_restore.sh drill "$DATABASE_URL" runsheet_drill
```

The script accepts the app's `postgresql+psycopg://` URL as well as plain
`postgresql://`; libpq rejects the driver suffix with an "invalid URI scheme"
error that does not obviously point at the cause, so it is stripped.

## Required before any migration

The chain contains `0007_drop_shipments_current`, which **drops a table**. It is
irreversible without a dump. Take and verify one first — this is step 2 of
[docs/deploy-runbook.md](deploy-runbook.md), not optional.

## Restoring

1. Stop the application, or scale it to zero. A restore drops and recreates
   objects; requests served mid-restore see a partial schema.
2. `scripts/backup_restore.sh restore "$DATABASE_URL" <dump>`
3. Confirm the schema is at head: `python -m scripts.check_migrations`
4. Start the application and confirm `/health/ready` returns 200.

There is no re-projection step. Steps 3 and 4 here used to be
`rebuild_document_store --all` followed by a separate restore for the three
fuel-asset indices from their own Elasticsearch export, because a Postgres restore
could not reconstruct them. Both are gone: `es_documents` is in the dump.

`python -m persistence.rebuild_document_store --all --tenant <tenant>` is still
available and is now a *repair* tool rather than a recovery step — use it if the
document projection has drifted from the relational tables, which
`python -m persistence.parity_check` detects.

Do **not** use `alembic downgrade` to undo past `0007_drop_shipments_current`:
the table is dropped and the rows are gone, so a downgrade recreates an empty
table and the data exists only in the dump.

## Not decided yet

These are policy choices, not engineering gaps, and they are the operator's to
make. Naming them here so they are not mistaken for solved:

- **Schedule and retention.** How often a dump is taken, how long it is kept,
  and how many generations. Nothing is scheduled today.
- **Off-host storage and encryption.** A dump on the database host survives a
  bad migration and nothing else. Dumps contain customer records, invoices and
  payment references.
- **RPO / RTO.** Managed Postgres point-in-time recovery gives a much lower RPO
  than periodic dumps and is worth enabling regardless of this script; the
  drill's value is that it proves the dumps are restorable.
A fourth item stood here: whether the ES-only indices were acceptable at launch,
given that the whole-cluster export covering them was a periodic snapshot and
anything written between exports was lost with the cluster. It is **settled** — the
cluster is gone and `es_documents` is in the same dump as everything else, so there
is no separate snapshot window to accept. `truck_compartments`, the one to weigh
first because `last_loaded_product` gates a physical safety check, has had a
Postgres table since `0008_fuel_asset_tables`.
