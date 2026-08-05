# Elasticsearch → Postgres migration

Goal: remove Elasticsearch. Postgres becomes the only store.

This is a multi-phase job and each phase is independently shippable. This
document records what is done, what is next, and the measurements the plan rests
on — so the next session does not have to rediscover them.

## Why this is possible

The cluster is small and the query surface is narrow. Measured, not estimated:

| Measurement | Value |
|---|---|
| Documents (top-level, via `_count`) | 7,623 |
| Primary store size | 6.1 MB |
| Indices | 103 |
| Elasticsearch call sites | 684 across 217 non-test files |

No feature in use requires a search engine:

- **No vector search.** No `knn`, no ELSER, no `semantic_text`. `semantic_search`
  is a plain `multi_match`.
- **No geo queries.** Ten mapping files declare `geo_point`; nothing issues a geo
  query. Routing uses Haversine in Python.
- **No ILM dependency.** Policies are applied when present and skipped cleanly
  when absent; five indices reference one.

Query DSL usage, by clause count across the codebase:

```
term 813   bool 388   must 268   terms 168   filter 155   range 115
match_all 75   exists 38   script 18   match 10   nested 7   multi_match 6
wildcard 4   query_string 2
```

Aggregations: `terms` 168, `range` 115, `sum` 31, `avg` 22, `date_histogram` 17.

All of that translates to SQL over a `jsonb` column. The 684 call sites are the
real cost, not the feature set — which is why the plan does not rewrite them.

## Approach

Write **one** Postgres-backed adapter implementing the existing
`ElasticsearchService` interface, translating the DSL subset above to SQL over
`jsonb`. Call sites do not change.

~25 aggregate types are already in Postgres at full parity: 10 relational via
`persistence.projections.PROJECTORS`, 15 hybrid via
`HybridReadRepository._SPECS`.

## Phases

| # | Phase | Status |
|---|---|---|
| 0 | Whole-cluster backup, verified | done |
| 1 | Postgres source of truth for the three ES-only indices | done |
| 2 | Postgres-backed `ElasticsearchService` adapter (DSL → SQL over `jsonb`) | next |
| 3 | Backfill every remaining index; parity per index | not started |
| 4 | Flip reads to the adapter behind a flag; soak | not started |
| 5 | Stop writing to Elasticsearch | not started |
| 6 | Remove the client, mappings and ILM policies | not started |
| 7 | **Delete the cluster** | not started |

Deletion is step 7, after parity, with explicit confirmation. Nothing before it
destroys data.

### Phase 0 — whole-cluster backup

`scripts/es_only_backup.py` gained `--all` (whole-cluster) and `verify` became
**manifest-driven**. It previously read 3 of 103 files, so it would have passed a
truncated whole-cluster export; proven by truncating `mvp_load_plans` from 149 to
100 documents and watching the old check pass and the new one fail.

Also fixed four silent seed-id bugs in `seed_all_data._resolve_json_doc_id`, which
walked one ordered candidate list so a foreign key could win the document `_id`:

| Index | Was keyed by | Effect |
|---|---|---|
| `rack_prices` | `terminal_id` | 5 rows → 3 documents; rack prices are per (terminal, product) and the sourcing recommender scores on them |
| `weather_observations` | nothing resolvable | every row skipped, index **empty**, while `EsHddProvider` reads it for K-factor degree-days |
| `atg_readings` | `instance_id` | 2 rows → 1 document |
| `customer_tanks` | `customer_id` | latent: one customer with two tanks would have kept one |

### Phase 1 — the three ES-only indices

`customer_tanks`, `truck_compartments` and `fuel_stations` were the entire
contents of `rebuild_from_postgres.ES_ONLY_INDICES`: authoritative operational
state with no Postgres table, so recreating the cluster destroyed it permanently.
See [docs/backup-and-restore.md](backup-and-restore.md) for what that cost and how
the rebuild is now verified.

Migration `0008_fuel_asset_tables` adds three hybrid tables — typed filter columns
plus the verbatim ES document in `jsonb`, not `json`, because the Phase-2 adapter
needs GIN indexing and the `jsonb` operators.

Three different primary-key rules, each forced by the data:

- **`customer_tanks`** keys on `customer_tank_id`, which is what the production
  writer uses. The live ES documents are keyed by `customer_id` (the seeder bug
  above), so the backfill remaps as it copies rather than importing a collision.
  Readers use a `term` query on `customer_tank_id`, never an `_id` fetch, so the
  remap is invisible to them.
- **`truck_compartments`** preserves the ES `_id` verbatim as `compartment_key`
  (`f"{truck_id}_{compartment_id}"`, e.g. `TNK-002_C1`) because the application
  fetches compartments by that id.
- **`fuel_stations`** preserves the ES `_id` as `station_key`, because the index
  carries **two** id conventions: `FuelService.create_station` writes
  `f"{station_id}::{fuel_type}"` while every seeded document and the ATG
  connector's update path use the bare `station_id`. Keyed on `station_id` a
  second product for the same station would collide and one document would
  vanish.

  That disagreement is a real pre-existing bug — an ATG reading for an
  API-created station updates nothing — and it is **not fixed here**. The table
  reproduces it faithfully so the two stores stay comparable under parity.

Dual-write is wired at every writer: `CustomerTankRepository`
(create/update/delete), `CompartmentStateRepository._atomic_update` (which mirrors
the full post-update document, not the patch), the compartment-config endpoint,
`FuelService` (create + four partial updates), and both Veeder-Root ATG fallback
paths.

Verified against the dev cluster:

- Backfill: 6 / 9 / 14 documents, matching Elasticsearch exactly.
- Parity: `customer_tank` 6=6, `truck_compartment` 9=9, `fuel_station` 14=14, all
  OK.
- Drop-and-rebuild drill on all three: bodies identical, mappings correct,
  tenant-scoped `term` matching every document.
- Live dual-write: create, partial patch (untouched fields survive the merge) and
  delete all propagate to Postgres.

Two defects were found by that verification rather than by reading:

1. **`_lookup_mapping` did not know where two of the three mappings live**, so a
   rebuild after a drop recreated the index with a dynamic mapping. `tenant_id`
   came back as `text`, and a tenant-scoped `term` query matched 0 of 9 documents
   while the rebuild logged nine indexed and exited 0.
   `tests/unit/test_rebuild_mapping_coverage.py` now asserts every rebuildable
   index resolves to a declared mapping whose `tenant_id` is `keyword`.
2. **`parity_check` raised `KeyError: 'shipment'`** and abandoned the run,
   silently skipping the seven aggregates after it. `shipment` stayed in
   `_INDEX_BY_AGG` after rev 0007 dropped its table, and only surfaces when
   `shipments_current` is absent from `retired_es_indices`.

### Phase 2 — the adapter (next)

Implement the `ElasticsearchService` interface over Postgres. Scope is set by the
clause counts above: `term`, `terms`, `bool`/`must`/`filter`/`should`/`must_not`,
`range`, `exists`, `match_all`, plus `terms`/`range`/`sum`/`avg`/`date_histogram`
aggregations. `script` (18), `wildcard` (4) and `query_string` (2) need
case-by-case handling; `nested` (7) needs a decision on whether to model the
subdocuments relationally.

## Standing facts

- Not on Elastic Cloud and never were. `ELASTIC_ENDPOINT` is a placeholder;
  development runs `elasticsearch:8.11.0` in Docker.
- `COMMERCE_READ_FROM_POSTGRES` is `true` in development, defaults to `False`, and
  is absent from `.env.production`.
- `_cat/indices` `docs.count` includes nested documents and ES system indices. Use
  `_count` for a top-level figure; `_cluster/stats` overcounts (it reported 9,353
  against a real 7,623).
