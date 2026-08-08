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

## What this surfaced: the app was single-instance only — now fixed

The obvious answer to a saturated worker is more workers or more replicas. That
was unsafe when this baseline was taken, and it was the more consequential
finding. **It has since been fixed** — see the leader-election section below and
`persistence/leader_election.py`. The throughput numbers above still stand as a
per-process figure; the remedy for them is now available.

Every background job started unconditionally in every process, with no
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

Ownership is re-verified before **every** drain, not just at startup. The lock is
session-scoped while `drain_once` runs on a different session, so anything that
kills the lock-holding connection — a managed-Postgres failover, a
`pg_terminate_backend`, an `idle_in_transaction_session_timeout` reaping this
deliberately idle session — releases the lock while the loop keeps draining. The
loop compares `pg_backend_pid()` against the pid the lock was granted on and
re-contends when it changes. Verified by terminating the lock backend while a
second connection held the lock: the relay drained 0 times in the following 1.5 s
and logged the loss at WARNING.

**Now fixed: the periodic sweeps and the agents.** All 13 sweeps and every
autonomous agent are gated on an elected leader
(`persistence/leader_election.py`). One Postgres advisory lock represents the
"run the periodic jobs" role; `run_periodic` skips the cycle on a follower and
keeps the loop alive so leadership can move there later.

One role lock rather than one per job, forced by the connection budget: a
session-scoped lock needs a connection that lives as long as the loop, and ~34
singleton jobs against `database_pool_size` 10 + `database_max_overflow` 5 would
exhaust the pool and block the request path. The cost is that background work is
not spread across replicas — one replica runs all of it.

The lock uses the two-argument `pg_try_advisory_lock(classid, objid)` form, which
Postgres keeps in a separate key space from the relay's one-argument bigint key;
verified against a real server by granting both at once with deliberately
colliding bytes.

Verified with two real backend processes against one Postgres: both derived the
same lock objid, one logged `This process is now the sweep leader`, the other
`Sweep leader held elsewhere — standing by`, both answered `/health/live` 200 and
`/api/orders` 401, and killing the leader moved leadership to the standby within
one verify interval while it kept serving traffic.

**So a rolling deploy is now the supported path** and `desiredCount` may exceed 1.
The remaining caveat is not about the sweeps: a rolling deploy runs old and new
code simultaneously against a migrated schema, so a destructive migration still
wants a serialised deploy.

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
