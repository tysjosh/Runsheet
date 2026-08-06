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
| 2 | Postgres-backed document store (DSL → SQL over `jsonb`) | done |
| 3 | Copy every remaining index; whole-cluster parity | done |
| 4 | Flip `DOCUMENT_STORE_BACKEND=postgres`; soak | ready, not flipped |
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

### Phase 2 — the document store

Migration `0009_es_documents` adds one generic table — `(index_name, doc_id)`
primary key matching Elasticsearch exactly, `tenant_id` lifted to a typed column,
the document in `jsonb`, and a GIN index. One table rather than ~75 because the
whole cluster is 7,623 documents and the largest index holds 988, so per-index
partitioning buys nothing measurable, and these documents have no agreed schema.

Four modules:

| Module | Job |
|---|---|
| `persistence/document_query.py` | DSL → SQL predicates, sort, `_source` |
| `persistence/document_matcher.py` | the same DSL in Python, for `filter` aggregations and as the property-test oracle |
| `persistence/document_aggregations.py` | aggregations over the fetched match set |
| `persistence/document_store.py` | the `ElasticsearchService` async surface |
| `persistence/document_field_policy.py` | fields that must stay unsearchable |

`ElasticsearchService` delegates its nine async document methods to the store when
`DOCUMENT_STORE_BACKEND=postgres`. No call site changes; a rollback is flipping
the variable back.

**Aggregations run in Python, not SQL.** Filtering and sorting need indexes and
decide which rows are read, so they go to SQL. Aggregation does not: every
aggregating query in the codebase passes `size: 0` over one tenant's slice of an
index of at most 988 documents. `date_histogram` with nested sub-aggregations,
`top_hits`, and ES's epoch-millis treatment of `min`/`max` on dates are each
awkward in SQL and simple over Python values. The bound is enforced, not assumed:
`MAX_AGGREGATION_ROWS = 50_000` and past it the store raises rather than
truncating.

**An unsupported clause raises.** This is the load-bearing decision. `script`,
`nested`, `query_string`, `geo_*`, pipeline aggregations and calendar-month
histograms all fail loudly with the clause named. A translator that silently drops
a clause returns wrong rows and the caller cannot tell that from a real result —
which is the shape of every silent-empty defect this migration has found.

#### Behaviour differences, stated rather than hidden

| Area | Elasticsearch | Postgres store |
|---|---|---|
| Read-after-write | needs a refresh | immediately consistent |
| `_score` | computed | always `null`; sorting by it is a no-op |
| `match` / `multi_match` | analyzed terms, stemming, `fuzziness` | case-insensitive substring |
| `date` precision | milliseconds | full microseconds, so fewer ties |
| `float` fields | 32-bit, so `2.28` sums as `2.2799999713897705` | full double |
| unsorted queries | arbitrary order | ordered by id |

The text-matching row is the one with user-visible consequences: four call sites
pass `fuzziness: AUTO` and get substring behaviour instead. Accepted rather than
refused, because refusing would break four working reads for a difference none of
them depends on — but it is a real change, not a no-op.

#### Fields that must stay unsearchable

Eighteen indices declare fields Elasticsearch stores but does not index —
`"type": "binary"`, `"index": false`, `"enabled": false`. In a `jsonb` column
everything is queryable, so the move silently widens what a caller can filter on.
Two cases make that a security property rather than a curiosity:

* **`fuel_orders_current.pod_otp`** — the proof-of-delivery one-time code. With ES
  it cannot appear in a filter at all. Made searchable, an authenticated caller
  who can reach any order-search endpoint can confirm a guessed OTP one query at a
  time.
* **`driver_devices.push_token`** — an Expo push credential, same reasoning.

`persistence/document_field_policy.py` reads the declared mappings (the code, not
the cluster, so it survives the cluster) and the store refuses any query that
filters, sorts or aggregates on such a field. Returning the document is
unaffected — Elasticsearch returns these fields too; only querying them is
blocked.

### Phase 3 — whole-cluster parity

`scripts/document_store_parity.py` copies an index into `es_documents` and then
runs a battery of query bodies against **both** backends, diffing the total, the
ordered id list, every returned document body, and every aggregation.

Result on the development cluster: **1,426 comparisons identical across all 103
indices**, with 21 void — queries Elasticsearch itself refuses (a `terms`
aggregation on a dynamically-mapped `text` field needs fielddata), so there is
nothing to compare against.

Comparing ids rather than counts is the point. Two backends can return the same
number of different documents; the ordered id list catches a sort that puts
`"10"` before `"9"`.

The tool distinguishes four outcomes, because conflating them makes it unreadable:
a genuine divergence; a legitimate tie-break difference (verified by checking the
sort *values* are monotonic and the page boundary matches, not the ids); a
float32-precision difference in an aggregation; and a void comparison. The first
whole-cluster run reported 119 of 1,125 divergences, nearly all of them the tool
comparing unsorted pages of 50 from 1,151 documents — which neither backend
promises anything about.

#### Defects the parity run found that the tests had not

1. **`exists` on an empty array.** `mvp_tank_forecasts` has two documents with
   `anomaly_flags: []`. Elasticsearch excludes them — an empty array is zero
   values — and the store included them, because `->>'anomaly_flags'` renders an
   array as the *text* `'[]'`, which is not NULL. ES=1981, PG=1983. Now tested via
   `jsonb_typeof`, and the property test generates empty arrays.
2. **`terms` aggregation tie-break.** Elasticsearch orders by count in the
   requested direction and breaks ties by key **ascending** regardless. Sorting on
   a tuple with `reverse=True` reversed both, so equal-count buckets came back
   key-descending: `[C4, C3, C2, C1, C5]` where ES returns `[C1, C2, C3, C4, C5]`.
   Same counts, different order, and any caller taking "the top bucket" from a tie
   got a different answer.

Three earlier defects were found by the property test before the parity run:
the generic `JSON` comparator compiling `contains` to string `LIKE`; SQL
three-valued logic making a document match neither a query nor its negation; and
`minimum_should_match: 0` being treated as 1.

### Phase 4 — the cutover (ready)

Set `DOCUMENT_STORE_BACKEND=postgres` (requires `DATABASE_URL`). The flag is
validated at startup, so a misspelling — `postgresql`, `pg` — fails rather than
leaving the service quietly on the legacy path. It is inert without a database.

Before flipping, per environment:

```sh
cd Runsheet-backend
python -m scripts.document_store_parity run --all --tenant <tenant>
```

#### The flag is not yet safe to flip

Code that reaches past `ElasticsearchService` to `es.client.search(...)` or
`es.client.update(...)` is **not** routed by the switch. After the cutover those
sites keep talking to Elasticsearch while everything else talks to Postgres, and
the two stores diverge — silently, because each call individually succeeds.

`tests/unit/test_raw_elasticsearch_client_inventory.py` inventories that surface
with an explicit allowlist. The test fails on an unlisted call site, and on a
listed one that no longer exists, so the list cannot rot and can only shrink.
**While it is non-empty the flag splits writes across two stores.**

| Remaining | Sites | Why it has not moved |
|---|---|---|
| `ops/api/endpoints.py` | 21 | mechanical searches; the store answers them unchanged |
| `ops/ingestion/poison_queue.py` | 7 | index / get / update / delete / count / search |
| `fuel/driver_repository.py` | 4 | painless counter scripts + `update_by_query` |
| `fuel/compartment_state_models.py` | 4 | two `if_seq_no` OCC loops |
| `Agents/approval_queue_service.py` | 2 | `if_seq_no` OCC |
| `Agents/tools/ops_{report,search}_tools.py` | 2 | mechanical searches |
| `bootstrap/core.py` | 1 | `scan` over an index at startup |
| migration tooling | 3 | exists to talk to Elasticsearch; moves last |

##### Read-modify-write now has a replacement

Two Elasticsearch patterns did read-modify-write: painless `scripted_upsert`, and
`if_seq_no` / `if_primary_term` optimistic concurrency with a retry loop.
`PostgresDocumentStore.atomic_update` replaces both with `SELECT … FOR UPDATE`,
which is strictly stronger — a concurrent writer waits instead of losing a race,
so **the retry loops disappear**. `CompartmentStateRepository` retries three times
with jittered backoff and raises `CompartmentStateConflictError` on persistent
contention, a 409 the caller has to handle; against a locked row that state cannot
arise.

Verified non-vacuously: with the row lock removed, ten concurrent increments
produce **3**, losing seven writes. With it, ten.

Two of the four sites are migrated. `ElasticsearchService.upsert_if_newer` now owns
the timestamp guard, with an Elasticsearch implementation and a Postgres one, so
`fuel/order_repository.py` (3 raw calls → 0) and `ops/services/ops_es_service.py`
(1 → 0) go through the facade. Three byte-identical transcriptions of the painless
script existed; there is now one, and the other reference binds to it.

The comparison is `isBefore || isEqual` — an event whose timestamp **equals** the
stored one is discarded. That reads like an off-by-one and is not: at-least-once
delivery makes an equal timestamp the common case for a redelivery, and applying it
would overwrite whatever a later event already wrote. Verified against both
backends live, all four cases agreeing:

| Case | Elasticsearch | Postgres |
|---|---|---|
| fresh insert | applied | applied |
| newer event | applied | applied |
| older event | discarded | discarded |
| equal timestamp | discarded | discarded |

The Elasticsearch implementation also keeps a workaround worth naming: serverless
Elasticsearch reported `noop` **and** failed to materialise the `upsert` body on a
fresh insert, which silently dropped every new order and produced a 404 straight
after a 201. The Postgres path cannot have that failure — there is no split between
"run the script" and "apply the upsert".

##### Still outstanding

* **Two `if_seq_no` OCC sites** (`compartment_state_models.py`,
  `approval_queue_service.py`) and **`driver_repository.py`'s counter scripts** to
  move onto `atomic_update`.
* **24 mechanical searches** to route through `search_documents`.
* **Six pipeline aggregations** in
  `notifications/services/communication_metrics_service.py` and one script-valued
  `sum` in `inventory/service.py`, to be rewritten as Python post-processing over
  bucket output.
* **A bulk scripted upsert** in `ops/services/ops_es_service.py` — a different
  shape from the single-document one, still on `helpers.bulk`.
* **The outbox relay** still projects into Elasticsearch. It should project into
  `es_documents`, at which point the ES indices for the ~25 already-migrated
  aggregates stop being written at all.

## Standing facts

- Not on Elastic Cloud and never were. `ELASTIC_ENDPOINT` is a placeholder;
  development runs `elasticsearch:8.11.0` in Docker.
- `COMMERCE_READ_FROM_POSTGRES` is `true` in development, defaults to `False`, and
  is absent from `.env.production`.
- `_cat/indices` `docs.count` includes nested documents and ES system indices. Use
  `_count` for a top-level figure; `_cluster/stats` overcounts (it reported 9,353
  against a real 7,623).
