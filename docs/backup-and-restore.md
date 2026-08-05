# Backup and restore

Postgres is the source-of-truth. Elasticsearch is a **projection** and is
rebuildable from it. That is now true without exception; it was not until the
fuel-asset migration, and the section below records what changed.

Everything here is executable: `Runsheet-backend/scripts/backup_restore.sh`.
The `drill` subcommand performs a real dump → restore → row-count comparison, and
runs on every pull request in the `migration-check` job. Before it existed there
was no restore procedure and nobody had restored this database.

## What is authoritative, and what is not

| Store | Role | Recoverable from |
|---|---|---|
| Postgres | Source-of-truth for commerce/financial records, orders and jobs current-state, master data, idempotency keys, invoice counters | Its own dump. Nothing else. |
| Elasticsearch | Search + read projection, fed by the transactional outbox | `python -m persistence.rebuild_from_postgres --all` |

### Three indices used to be unrecoverable. They are not any more.

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
established hybrid shape (typed filter columns plus the verbatim Elasticsearch
document, in `jsonb`). Each has a passthrough projector, a `_REBUILD_SPECS` entry,
a backfill and dual-write from every writer, so `rebuild_from_postgres --all`
restores them like anything else and `ES_ONLY_INDICES` is empty.

Verified by drill against the dev cluster — drop the index, rebuild from Postgres,
compare:

| Index | Documents | Bodies | Mapping | Tenant-scoped `term` |
|---|---|---|---|---|
| `customer_tanks` | 6 | identical | all filter fields `keyword` | 6/6 |
| `truck_compartments` | 9 | identical | all filter fields `keyword` | 9/9 |
| `fuel_stations` | 14 | identical | all filter fields `keyword` | 14/14 |

The mapping column is not decoration. The first run of that drill rebuilt
`truck_compartments` with a **dynamic** mapping, because
`rebuild_from_postgres._lookup_mapping` consulted five mapping registries and the
`truck_compartments` mapping lives in a sixth. `tenant_id` came back as `text`
instead of `keyword`, so a tenant-scoped `term` query matched **0 of 9**
documents while the rebuild logged nine indexed and exited 0. `fuel_stations` had
the same gap and its module had no registry to consult at all.
`tests/unit/test_rebuild_mapping_coverage.py` now asserts that every rebuildable
index resolves to a declared mapping whose `tenant_id` is `keyword`.

#### The ES-only backup script

`scripts/es_only_backup.py` still exists and still imports its index list from
`ES_ONLY_INDICES`, so a fourth index added without a projector is covered
automatically. With the list empty its **default scope refuses** rather than
writing an empty manifest that `verify` would call internally consistent — a green
backup covering nothing.

Use `--all` for a whole-cluster export. That is the mode the Elasticsearch →
Postgres migration needs: once ES stops being written to, everything in it is
irreplaceable, not just the entries that lack a projector.

```sh
cd Runsheet-backend
python -m scripts.es_only_backup export --all --out-dir ./es-full-backup
python -m scripts.es_only_backup verify --out-dir ./es-full-backup
python -m scripts.es_only_backup restore --all --out-dir ./es-full-backup  # refuses an export that does not verify
python -m scripts.es_only_backup drill                                     # export → restore to scratch → compare → clean up
```

`verify` is driven by the manifest, not by the index list, so it cannot pass a
whole-cluster export while leaving most of its files unread. `drill` compares
document **content**, not just counts — a count-only check would pass a restore
that silently dropped `last_loaded_product`.

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
3. Re-project Elasticsearch from the restored source of truth:
   `python -m persistence.rebuild_from_postgres --all --tenant <tenant>`
4. Step 3 now covers the fuel assets too (`customer_tanks`,
   `truck_compartments`, `fuel_stations`), so no separate restore is needed. If
   `ES_ONLY_INDICES` is ever non-empty again, restore those from their own export
   here — a Postgres restore cannot reconstruct them:
   `python -m scripts.es_only_backup restore --out-dir ./es-backup`
5. Confirm the schema is at head: `python -m scripts.check_migrations`
6. Start the application and confirm `/health/ready` returns 200.

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
- **Whether the ES-only indices are acceptable at launch.** The export above
  covers them operationally, but it is a periodic snapshot: anything written
  between exports is still lost with the cluster. Whether that is tolerable is a
  product decision. `truck_compartments` is the one to weigh first — its
  `last_loaded_product` gates a physical safety check, so the cost of losing it
  is a cross-contamination guard that cannot see the previous load, not merely
  missing data. Give it a Postgres source of truth, or accept the snapshot window
  explicitly and say what it is.
