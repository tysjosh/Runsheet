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
| 4 | Flip `DOCUMENT_STORE_BACKEND=postgres`; soak | done in development |
| 5 | Stop writing to Elasticsearch | done |
| 6 | Remove the client, mappings and ILM policies | done |
| 7 | **Delete the cluster** | done |

Deletion was step 7, after parity, with explicit confirmation. Nothing before it
destroyed data, and the whole-cluster export from Phase 0 is kept.

`DOCUMENT_STORE_BACKEND` no longer exists. It was the Phase 4 switch, and Phase 6
removed it along with `ELASTIC_ENDPOINT`, `ELASTIC_API_KEY`, the `elasticsearch`
dependency and every cluster-management code path. Rolling back is no longer a flag:
it means restoring the cluster from `Runsheet-backend/es-full-backup/` and reverting
the Phase 5 and 6 commits.

### Phase 0 — whole-cluster backup

> `scripts/es_only_backup.py` was **deleted in Phase 6** along with the cluster it
> talked to. The export it produced —
> `Runsheet-backend/es-full-backup/`, 103 indices, 7,623 documents, 7.1 MB, verified
> — is kept and is the only remaining copy. What follows is how it was taken.

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

> `scripts/document_store_parity.py` was **deleted in Phase 6**: it compared the two
> backends and there is only one left. Its result is the record below. For ongoing
> drift between the relational tables and the document projection, use
> `python -m persistence.parity_check`, which compares those two and is still live.

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

### Phase 4 — the cutover

Setting `DOCUMENT_STORE_BACKEND=postgres` (requires `DATABASE_URL`) routed the
document plane at Postgres. The flag was validated at startup, so a misspelling —
`postgresql`, `pg` — failed rather than leaving the service quietly on the legacy
path, and it was inert without a database.

Phase 6 **removed the flag**, because there is no other path to select. There is
also no `scripts/document_store_parity.py` any more: it compared the two backends
and one of them is gone. What it proved is recorded below and in Phase 3.

#### What the flip actually looked like

Verified against the live development stack rather than inferred from the suite,
because the suite cannot answer "does the flag route against real data":

| Check | Result |
|---|---|
| `_pg_store()` engaged | `PostgresDocumentStore` |
| counts vs the cluster, 6 indices | identical (see the `customer_tanks` note) |
| read-after-write | written, read back, and searchable with **no refresh** |
| keyset pagination through `MeterAuditService` | 2 pages, 3 items, 3 distinct |
| the three rewritten metrics | return numbers, not empty results |
| inventory summary (was a 500) | 14 items, £27,645.07 |

`customer_tanks` reads 7 in Postgres against 6 in Elasticsearch. That is
`TANK-DRILL-1`, left in the development database by the Phase 1 drop-and-rebuild
drill — dev-only residue, not a migration defect. Worth knowing before the next
person runs the same comparison and reads it as one.

Two behaviour notes an operator will see in the logs:

* the relay's log line said "published N event(s) to Elasticsearch"
  unconditionally. Under this flag that is the opposite of what happens — the
  relay projects through `index_document` and follows the switch like every other
  write — so it now reads the backend rather than naming it.
* `job_events` is empty in development, so the two paired-event latency metrics
  report `count: 0`. That is the honest answer, and it is distinguishable from the
  old failure, which returned the same empty shape because the aggregation had
  raised.

#### The raw-client surface is closed

Code that reached past `ElasticsearchService` to `es.client.search(...)` or
`es.client.update(...)` was **not** routed by the switch. After the cutover those
sites would keep talking to Elasticsearch while everything else talked to Postgres,
and the two stores would diverge — silently, because each call individually
succeeds.

`tests/unit/test_raw_elasticsearch_client_inventory.py` inventories that surface
with an explicit allowlist. The test fails on an unlisted call site, and on a
listed one that no longer exists, so the list cannot rot and can only shrink.

**It has now shrunk to nothing in the application data plane.** All 41 sites have
been rewritten onto the facade:

| Was | Sites | Now |
|---|---|---|
| `ops/api/endpoints.py` | 21 | `search_documents` |
| `ops/ingestion/poison_queue.py` | 7 | the facade passthroughs on `OpsElasticsearchService` |
| `fuel/driver_repository.py` | 4 | `atomic_update`, `update_by_query` |
| `fuel/compartment_state_models.py` | 4 | `atomic_update`, `get_document` |
| `Agents/approval_queue_service.py` | 2 | `atomic_update` |
| `Agents/tools/ops_{report,search}_tools.py` | 2 | `search_documents` |
| `ops/services/ops_es_service.py` | 1 | `upsert_if_newer` (bulk scripted upsert) |
| `bootstrap/core.py` | 1 | never was Elasticsearch — Redis `scan`, misclassified |

The list is **empty** now. What was left after those 41 was migration tooling (3
calls) and the facade's own Elasticsearch branch (13 calls, the calls the switch
chose *between*), and Phase 6 closed all four files:

| Was | Calls | Now |
|---|---|---|
| `services/elasticsearch_service.py` | 13 | deleted with the Elasticsearch branch |
| `persistence/rebuild_from_postgres.py` | 1 | `rebuild_document_store.py`, `index_document(..., stamp_timestamps=False)` |
| `scripts/seed_kfactor_demo.py` | 1 | same opt-out — its delivery dates are the data |
| `services/data_seeder.py` | 1 | `delete_by_query` |

The three tooling calls all existed for one reason: `index_document` force-stamps
`updated_at` to now(), which would have restamped a rebuilt index and collapsed the
K-factor demo's three-week delivery spacing into a single instant. The
`stamp_timestamps=False` opt-out is therefore load-bearing for the empty inventory,
not a convenience — `tests/unit/test_raw_elasticsearch_client_inventory.py` pins its
existence for that reason.

Note the direction of the facade's count: it went **up**, from 9 to 13, as
`upsert_if_newer`, `atomic_update` and `update_by_query` each absorbed a raw call
from elsewhere. That is the expected shape — the total across the codebase falls
while the facade's share of it rises.

The scanner was also wrong in two ways worth recording, because both made the
number look better than it was:

* it matched text, so the docstrings five of these files carry explaining which
  raw call they *used* to make counted as raw calls. `poison_queue.py` had all
  seven migrated and still reported one. It parses the AST now.
* it only recognised `.client.<method>(...)`, so
  `helpers.bulk(self.client, actions)` in `ops_es_service.py` was invisible — a
  write of every batched shipment and rider, on a code path the inventory
  reported as clean. Passing the client to a data-plane helper now counts.

An empty application data plane is necessary for the cutover, not sufficient.
See *Still outstanding* below.

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

All of these sites are migrated. `ElasticsearchService.upsert_if_newer` owns the
timestamp guard, `atomic_update` owns the read-modify-write, and
`update_by_query` owns the bulk variant — each with an Elasticsearch
implementation and a Postgres one behind the same signature. Three byte-identical
transcriptions of the painless upsert script existed; there is now one, and the
remaining reference binds to it rather than restating it.

`update_by_query` is the one place the two backends differ in cost rather than
just in mechanism. On Postgres it is one statement over locked rows. On
Elasticsearch it resolves the matching ids and calls `atomic_update` on each,
which is N round trips instead of one. That is deliberate: the alternative was
writing the same rule twice, once in Python and once in painless, and paying for
that duplication permanently to optimise the branch that is being deleted. It
refuses to run past `UPDATE_BY_QUERY_MAX_DOCS` (5,000) rather than applying a
prefix, because a partially-applied `update_by_query` leaves the index in a state
no caller asked for and no caller can detect.

One behaviour changed in the bulk path, deliberately. `helpers.bulk` counted a
scripted `noop` as a success, so an ingestion run that discarded every event as
stale was indistinguishable from one that applied every event. `bulk_upsert` now
returns `discarded` separately from `successful`.

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

#### The query shapes the store refused, and the two it silently dropped

Closing the raw-client surface was necessary and not sufficient. What remained was
query *shapes*, and chasing them turned up a worse category than the one that was
already written down.

**Refused loudly** — three sites. Two pipeline-aggregation latency metrics
(`bucket_script` / `stats_bucket` / `avg_bucket`) and one script-valued `sum`. The
metrics caught their own exception and returned `{"buckets": [], "overall": {}}`,
so "fails loudly" was only true inside the log; a caller saw an empty metric.
`inventory.get_summary` has no `except` and was a genuine 500. All three are now
Python post-processing over supported aggregations.

**Dropped silently** — and this is the category that was not in this document at
all. The store's rule is that an unsupported clause raises rather than being
ignored; it was enforced for clauses inside `query` and `aggs` and *not at all* for
the top level of the search body, where an unrecognised key was simply never read.

| Key | Sites | What dropping it did |
|---|---|---|
| `search_after` | 9 public, 8 internal | pagination returns page 1 forever |
| `runtime_mappings` | 1 | `stats` over a field that does not exist → **reports zero seconds of send latency as though measured** |
| `track_total_hits` | 9 | nothing; Postgres totals are always exact |

`search_after` also exposed a live defect that has nothing to do with this
migration. All nine public sites built the boundary as `[cursor, cursor]` where
`cursor` is an id, against a `[{created_at: desc}, {id: asc}]` sort — so an id went
in as a date boundary. On the cluster, today:

```
page 1: HTTP 200
    _id=ACC-004  sort=[1767978188875, 'ACC-004']
    _id=ACC-008  sort=[1762448588875, 'ACC-008']

page 2 with the shipped [cursor, cursor]: HTTP 400
    failed to parse date field [ACC-008] with format
    [strict_date_optional_time||epoch_millis]

page 2 with the real sort values [1762448588875, 'ACC-008']: HTTP 200
    _id=ACC-010
    _id=ACC-007
```

Page 2 has never worked on any of them. The cursor stays an **id** rather than
becoming an opaque encoded sort tuple, because these endpoints have a second
implementation — `persistence/read_repositories.py` serves them from the relational
tables under `COMMERCE_READ_FROM_POSTGRES` — and that one already paginates
correctly with an id cursor. Two implementations of one endpoint handing out
different cursor formats would invalidate every in-flight cursor on either flag
flip. `services/keyset_pagination.py` resolves the cursor row server-side, which is
what the relational path does in SQL, so there is no API change.

One deliberate asymmetry: an unresolvable cursor is a 400 here, where the
relational path silently restarts at page 1. For the usual `while next_cursor:`
loop, restarting never terminates — page 1 returns with the same cursor attached.
That is also how the regression showed up when it was injected: nine tests failed
with `pagination did not terminate`.

The store now honours `search_after`, returns `sort` values on hits as Elasticsearch
does, accepts `track_total_hits` and `timeout` with a stated reason, and **refuses
every other top-level key**.

### Phases 5–7 — no cluster, no client, no container

**Phase 5 — stop talking to the cluster.** `services/no_cluster.py` replaces the
Elasticsearch client with `NoClusterClient`: control-plane namespaces (`indices`,
`ilm`, `cluster`, `cat`, `snapshot`) become logged no-ops, and every data-plane
method **raises** `ClusterRemovedError`.

The split is the point. A permissive mock would also swallow `client.index(...)`,
so a document write that still reached the raw client would vanish with no error —
the failure mode this whole migration kept finding. `client = None` was the other
option and would have meant nineteen individual guards in eighteen modules.

`connect()` used to raise on a failed ping, and this class builds a module-level
singleton, so stopping the container did not degrade the service: it stopped the
application from **importing**, inside `import data_endpoints`. Verified by stopping
it — uvicorn died at `ConnectionError: Failed to ping Elasticsearch` before a single
route was registered.

Readiness moved with it. `elasticsearch` was a **critical** health dependency, so a
stopped cluster made `/health/ready` report `unhealthy` and an orchestrator would
have pulled a working service out of rotation. The probe is now `SELECT 1` through
the engine the document store uses.

**Phase 6 — delete the code.** Roughly 1,400 lines went: 15 `setup_*_indices`
functions, 27 `ElasticsearchService` methods (ILM, index setup, schema validation,
serverless detection, seven legacy mapping getters), nine ops methods,
`services/mapping_validator.py`, the `elasticsearch` import and dependency, the
`ELASTIC_*` settings and their validators, `DOCUMENT_STORE_BACKEND` and the
per-method dual branches.

Two things deliberately survived:

* **The mapping registries stay.** `persistence/document_field_policy.py` reads the
  declared mappings to decide which fields must remain unqueryable — in a `jsonb`
  column everything is queryable, so moving off Elasticsearch silently *widens* the
  filterable surface. Two of the affected fields are credentials:
  `fuel_orders_current.pod_otp` (the proof-of-delivery one-time code) and
  `driver_devices.push_token`. Deleting the mappings would have removed the policy's
  input.
* **`ElasticsearchService` keeps its name.** Renaming it would touch 567 files for
  no behavioural gain.

Moving the ops mappings out of four deleted methods into
`ops/services/ops_es_mappings.py` and registering them with the field policy closed
a gap that predates the migration: `ops_poison_queue.original_payload` is declared
`enabled: false` — Elasticsearch cannot filter on it at all — and was freely
queryable in `jsonb` because those mappings had never been in the policy's registry.

Rehoming `all_index_mappings()` into the field policy made it registry-driven rather
than a 160-line hand-written list, which took its coverage from ~70 indices to 86 and
surfaced a latent Elasticsearch defect: **seven `dynamic: strict` indices do not
declare `created_at` / `updated_at`** (`payments_current`, `ar_aging_snapshots`,
`dunning_events`, `driver_reports`, `tenant_job_policies`, `riders_current`,
`ops_poison_queue`) while `index_document` auto-stamps both. `PaymentService.ingest`
writes `payments_current` through `index_document`, so on Elasticsearch every such
write would have been rejected. Pinned as a frozen list.

One diagnostic change worth naming: `_iter_mappings` logs a failed registry import
at **ERROR**, not DEBUG. A registry that fails to import does not degrade the field
policy, it **deletes part of it** — and silently. Found the hard way: the generated
ops module was first written with `json.dumps`, so it contained a lowercase `false`,
parsed fine, and raised `NameError` on import. At DEBUG the policy simply reported no
restrictions for the ops indices and nothing said so.

**Phase 7 — delete the container.** The `runsheet-es` container and its volume are
gone, along with the Elasticsearch service in CI's image-smoke job (which stood up a
cluster only because readiness checked one). `Runsheet-backend/es-full-backup/` —
103 indices, 7,623 documents, 7.1 MB, verified — is the only remaining copy of the
cluster and is deliberately kept.

##### Still outstanding

* **Staging and production were never on Elasticsearch under this code.** They are
  not flipped because there is no longer a flag: the first deploy of these commits
  is their cutover, and it needs `DATABASE_URL` set and migrations at head or
  readiness fails. `.env.production` is still placeholders.
* **The two pipeline-aggregation shapes are still refused.** `bucket_script` /
  `stats_bucket` / `avg_bucket` and script-valued `sum` raise rather than being
  translated; the three call sites do their own Python post-processing. A new caller
  reaching for a pipeline aggregation gets an exception, which is the intended
  behaviour but is worth knowing before writing one.
* **The outbox relay is redundant, not misdirected.** It projects through
  `index_document`, so it copies relational aggregates into `es_documents` — another
  Postgres table. Harmless, and `persistence/parity_check.py` is what detects drift
  between the two, but it should stop running for any aggregate whose reads have
  moved to the relational path.

## Standing facts

- Never on Elastic Cloud. `ELASTIC_ENDPOINT` was a placeholder throughout;
  development ran `elasticsearch:8.11.0` in Docker.
- `COMMERCE_READ_FROM_POSTGRES` is `true` in development, defaults to `False`, and
  is absent from `.env.production`.
- `_cat/indices` `docs.count` included nested documents and ES system indices. The
  top-level figure came from `_count`; `_cluster/stats` overcounted (it reported
  9,353 against a real 7,623).
