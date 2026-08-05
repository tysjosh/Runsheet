# Load test baseline

Run it yourself:

```sh
cd Runsheet-backend
ENVIRONMENT=development ./venv/bin/python -m scripts.loadtest \
  --email dispatcher@demo.runsheet.test --password '<password>' \
  --concurrency 40 --duration 30
```

`scripts/loadtest.py` uses `httpx`, which is already a dependency, rather than
Locust — which is neither declared nor installed, so a Locust file could have
been committed and never run. It reports per-endpoint p50/p95/p99, throughput,
status codes, and the **transactional-outbox backlog** before, after and until
drained. `--max-p95-ms` and `--max-error-rate` make it usable as a gate.

## Measured — 2026-08-04

Single `uvicorn main:app` worker, Elasticsearch 8.11 and Postgres 16 co-located
on the same laptop, ~14 orders and 5 customers seeded. Mixed load: four read
endpoints round-robin, every 10th request per worker a `POST /api/orders`.

| Concurrency | Total rps | Read p95 | Write p50 | Write p95 | Write p99 | Errors |
|---|---|---|---|---|---|---|
| 5  | 84  | 49–87 ms | 287 ms | 511 ms | 519 ms | 0 |
| 40 | 135 | 132–817 ms | 573 ms | 1816 ms | 2745 ms | 0 |
| 80 | 152 | 642–1605 ms | 1020 ms | 2304 ms | 3703 ms | 0 |

**Zero errors at every level** — nothing fell over, no timeouts, no 5xx.

**Throughput saturates.** 8× the concurrency bought 1.8× the throughput
(84 → 152 rps) while latency grew roughly 10×. That is one process at its
ceiling: work is queueing, not being served faster. Writes cost ~10× a read at
every concurrency, which is expected — the write path carries the pricing hook,
the credit check and the dual-write with its outbox row.

**The outbox relay is not the bottleneck.** After 429 order writes the backlog
peaked at 15 unpublished rows and returned to zero in 0.5s.

These numbers characterise *that* setup. They are a baseline to regress against
and a way to see the shape of a limit — not a capacity plan. Treat the absolute
values as meaningless for production sizing and the *shape* as informative.

## What this surfaced: the app was single-instance only

The obvious answer to a saturated worker is more workers or more replicas. That
would have been unsafe, and this is the more consequential finding.

Every background job starts unconditionally in every process, with no
leader election and no guard:

- the outbox relay (`bootstrap/persistence.py`)
- ~15 periodic sweeps — invoice overdue, invoice draft-finalize, AR aging
  snapshot, credit-override expiry, price-protection expiry, rack-price refresh,
  driver daily reset, driver retention, delay detection, approval expiry, ERP
  invoice export, POD transition repair
- the agent scheduler and every overlay agent's monitor cycle

**Fixed for the relay.** `run_forever` now holds a Postgres advisory lock and an
instance that cannot take it stands down (logged at INFO — being a follower is
not a fault). Without it, two relays select the same unclaimed rows: re-indexing
one event is harmless, but relay A taking event 3 while relay B takes event 5 for
the same aggregate, with B committing first, leaves the **older** payload last —
the Elasticsearch projection permanently behind the row it mirrors, silently.
`FOR UPDATE SKIP LOCKED` was considered and rejected: it stops the duplicate work
but not the inversion. Verified with two relays against real Postgres — one
drained 11 cycles, the other stood down throughout.

**Not fixed: the periodic sweeps.** Each still runs in every process. Two
instances means two AR-aging snapshots for the same day and two overdue sweeps
racing the same invoices — and `invoice_events` has a unique
`(invoice_id, sequence_number)`, so a race there raises rather than
double-writing. They need the same advisory-lock treatment, or extraction into a
single worker process. **Until then this backend runs as exactly one instance,**
and a rolling deploy that briefly overlaps two is outside what has been verified.

## Suggested gate

Once the numbers exist for the real environment, wire the thresholds in:

```sh
python -m scripts.loadtest ... --concurrency 40 --duration 60 \
  --max-p95-ms 800 --max-error-rate 0.005
```

Not added to CI here: a shared runner's numbers would be noise, and a
load-test gate that flakes gets muted. It belongs against a staging environment
sized like production.

## Still unmeasured

- **Sustained load.** The longest run was 30s. Connection-pool exhaustion, memory
  growth and Elasticsearch merge pressure need tens of minutes.
- **The planning pipeline under load.** `POST /plan/generate` runs four agents and
  took ~1s single-shot; it is not in the mix here.
- **WebSocket fan-out.** No concurrent-subscriber measurement.
- **Realistic data volumes.** 14 orders and 5 customers. Pagination and
  aggregation costs do not show up at that size.
