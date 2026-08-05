# Backup and restore

Postgres is the source-of-truth. Elasticsearch is a **projection** and is
rebuildable from it — with three exceptions named below that are not.

Everything here is executable: `Runsheet-backend/scripts/backup_restore.sh`.
The `drill` subcommand performs a real dump → restore → row-count comparison, and
runs on every pull request in the `migration-check` job. Before it existed there
was no restore procedure and nobody had restored this database.

## What is authoritative, and what is not

| Store | Role | Recoverable from |
|---|---|---|
| Postgres | Source-of-truth for commerce/financial records, orders and jobs current-state, master data, idempotency keys, invoice counters | Its own dump. Nothing else. |
| Elasticsearch | Search + read projection, fed by the transactional outbox | `python -m persistence.rebuild_from_postgres --all` |

### Three Elasticsearch indices are NOT projections

`persistence.rebuild_from_postgres.ES_ONLY_INDICES`:

```
customer_tanks
truck_compartments
fuel_stations
```

These have no Postgres source of truth, no projector and no rebuild spec — a
test asserts that each entry genuinely has none, so the list cannot rot. **Losing
the Elasticsearch cluster loses this data outright,** and it is not covered by a
Postgres dump. `truck_compartments.last_loaded_product` is what the
cross-contamination guard reads before assigning a product to a compartment:
without it the guard cannot tell that a compartment last carried diesel before
loading gasoline.

They now have their own backup:

```sh
cd Runsheet-backend
python -m scripts.es_only_backup export --out-dir ./es-backup   # + manifest
python -m scripts.es_only_backup verify --out-dir ./es-backup
python -m scripts.es_only_backup restore --out-dir ./es-backup  # refuses an export that does not verify
python -m scripts.es_only_backup drill                          # export → restore to scratch → compare → clean up
```

The index list is imported from `ES_ONLY_INDICES`, never restated, so adding a
fourth ES-only index extends this backup automatically. `drill` compares document
**content**, not just counts — a count-only check would pass a restore that
silently dropped `last_loaded_product`, which is the failure that matters. Drilled
against the dev cluster: 29 documents across the three indices, content
identical; `verify` rejects a truncated file, invalid JSON, a record missing
`_source`, and a missing manifest.

**Run it on the same schedule as the Postgres dump.** These two backups are not
interchangeable and neither covers the other.

This is a stopgap, not the answer. The real options are a Postgres source of truth
for these entities (a migration plus ORM models, projectors and repository
rewiring — `truck_compartments` is the one with a correctness consequence) or a
configured Elasticsearch snapshot repository with a retention policy. Both need a
decision and infrastructure; the export exists so the gap is covered while that
decision is made.

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
4. Restore the three `ES_ONLY_INDICES` from their own export — a Postgres restore
   cannot reconstruct them:
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
