#!/usr/bin/env python3
"""Load driver for the Runsheet API.

Written against ``httpx``, which is already a dependency, rather than Locust —
which is not declared or installed, so a Locust file could be committed but never
run. An unexecuted load test is worth nothing; this one produces numbers.

What it measures
----------------
* per-endpoint latency percentiles (p50 / p95 / p99) and throughput
* error rate, with non-2xx status codes counted separately from transport errors
* **outbox backlog** before and after the run, and how long it takes to drain

That last one is the question nobody had answered: commerce writes land in
Postgres with a transactional-outbox row, and a background relay projects those
rows into Elasticsearch. If the relay drains slower than writes arrive, the
backlog grows without bound and Elasticsearch falls progressively further behind
the source of truth — while every request still returns 200.

What it does NOT measure
-----------------------
Production capacity. Run against a single local uvicorn worker with co-located
Elasticsearch and Postgres, these numbers characterise *that* setup. Their value
is as a baseline to regress against and as a way to find the shape of a
bottleneck, not as a capacity plan.

Usage
-----
    ENVIRONMENT=development ./venv/bin/python -m scripts.loadtest \\
        --base-url http://127.0.0.1:8080 \\
        --email dispatcher@demo.runsheet.test --password 'Demo1234!' \\
        --concurrency 20 --duration 30

    # Fail the process when a threshold is breached (for CI / a release gate):
    ... --max-p95-ms 800 --max-error-rate 0.01
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx


@dataclass
class Result:
    latencies_ms: List[float] = field(default_factory=list)
    statuses: Dict[int, int] = field(default_factory=lambda: defaultdict(int))
    transport_errors: Dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record(self, elapsed_ms: float, status: Optional[int], error: Optional[str]) -> None:
        self.latencies_ms.append(elapsed_ms)
        if error is not None:
            self.transport_errors[error] += 1
        else:
            self.statuses[status] += 1

    @property
    def count(self) -> int:
        return len(self.latencies_ms)

    @property
    def failures(self) -> int:
        bad = sum(self.transport_errors.values())
        bad += sum(n for code, n in self.statuses.items() if code >= 400)
        return bad

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        # Nearest-rank; explicit rather than statistics.quantiles so a run with
        # very few samples still reports something honest instead of raising.
        idx = min(len(ordered) - 1, max(0, int(round(p / 100.0 * len(ordered))) - 1))
        return ordered[idx]


async def sign_in(client: httpx.AsyncClient, email: str, password: str) -> str:
    """Return a SuperTokens access token, or raise with the response body."""
    resp = await client.post(
        "/auth/signin",
        json={"formFields": [{"id": "email", "value": email},
                             {"id": "password", "value": password}]},
        headers={"rid": "emailpassword"},
    )
    token = resp.headers.get("st-access-token")
    if not token:
        raise RuntimeError(
            f"signin did not return st-access-token (HTTP {resp.status_code}): "
            f"{resp.text[:300]}"
        )
    return token


async def outbox_backlog() -> Optional[int]:
    """Unpublished transactional-outbox rows, or None when PG is not configured.

    Queried directly rather than through an endpoint because no endpoint exposes
    it — which is itself worth noting: the backlog is invisible in production.
    """
    try:
        from persistence.database import is_persistence_enabled, session_scope
        from sqlalchemy import text
    except Exception:
        return None
    if not is_persistence_enabled():
        return None
    try:
        async with session_scope() as session:
            row = await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
            )
            return int(row.scalar() or 0)
    except Exception:
        return None


# (label, method, path, body-builder). GETs dominate the dispatcher UI; the POST
# is included because the write path is where the outbox, the pricing hook and
# the credit check all live, and a read-only load test would exercise none of it.
READS: List[Tuple[str, str]] = [
    ("GET /api/orders", "/api/orders?limit=20"),
    ("GET /api/commerce/customers", "/api/commerce/customers?limit=20"),
    ("GET /api/fleet/trucks", "/api/fleet/trucks"),
    ("GET /api/compliance/meters", "/api/compliance/meters"),
]


async def worker(
    client: httpx.AsyncClient,
    token: str,
    results: Dict[str, Result],
    stop_at: float,
    customer_id: Optional[str],
    write_every: int,
    worker_id: int,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    n = 0
    while time.monotonic() < stop_at:
        n += 1
        do_write = customer_id is not None and write_every > 0 and n % write_every == 0
        if do_write:
            label = "POST /api/orders"
            payload = {
                "client_event_id": f"load-{worker_id}-{n}-{int(time.time()*1000)}",
                "customer_id": customer_id,
                "customer_name": "Load Test",
                "ship_to_address": "1 Depot Rd, Houston, TX",
                "ship_to_lat": 29.76,
                "ship_to_lon": -95.36,
                "product_code": "DIESEL_2",
                "gallons_requested": 250,
                "call_type": "will_call",
            }
            started = time.perf_counter()
            try:
                resp = await client.post("/api/orders", json=payload, headers=headers)
                results[label].record(
                    (time.perf_counter() - started) * 1000, resp.status_code, None
                )
            except Exception as exc:
                results[label].record(
                    (time.perf_counter() - started) * 1000, None, type(exc).__name__
                )
            continue

        label, path = READS[n % len(READS)]
        started = time.perf_counter()
        try:
            resp = await client.get(path, headers=headers)
            results[label].record(
                (time.perf_counter() - started) * 1000, resp.status_code, None
            )
        except Exception as exc:
            results[label].record(
                (time.perf_counter() - started) * 1000, None, type(exc).__name__
            )


async def first_customer_id(client: httpx.AsyncClient, token: str) -> Optional[str]:
    resp = await client.get(
        "/api/orders?limit=1", headers={"Authorization": f"Bearer {token}"}
    )
    if resp.status_code != 200:
        return None
    body = resp.json()
    items = body.get("items") or body.get("orders") or []
    return items[0].get("customer_id") if items else None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument(
        "--write-every",
        type=int,
        default=10,
        help="every Nth request per worker is a POST /api/orders; 0 disables writes",
    )
    ap.add_argument("--max-p95-ms", type=float, default=None)
    ap.add_argument("--max-error-rate", type=float, default=None)
    ap.add_argument(
        "--drain-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the outbox backlog to return to its start value",
    )
    args = ap.parse_args()

    limits = httpx.Limits(
        max_connections=args.concurrency * 2,
        max_keepalive_connections=args.concurrency * 2,
    )
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=30.0, limits=limits
    ) as client:
        token = await sign_in(client, args.email, args.password)
        customer_id = await first_customer_id(client, token)
        if customer_id is None and args.write_every > 0:
            print("! no customer available — running reads only", file=sys.stderr)

        backlog_before = await outbox_backlog()

        results: Dict[str, Result] = defaultdict(Result)
        stop_at = time.monotonic() + args.duration
        wall_start = time.perf_counter()
        await asyncio.gather(*[
            worker(client, token, results, stop_at, customer_id,
                   args.write_every, i)
            for i in range(args.concurrency)
        ])
        wall = time.perf_counter() - wall_start

        total = sum(r.count for r in results.values())
        failures = sum(r.failures for r in results.values())
        error_rate = (failures / total) if total else 1.0

        print(f"\nconcurrency={args.concurrency} duration={wall:.1f}s")
        print(f"{'endpoint':<32}{'n':>7}{'rps':>9}{'p50':>8}{'p95':>8}{'p99':>8}"
              f"{'fail':>7}")
        for label in sorted(results):
            r = results[label]
            print(
                f"{label:<32}{r.count:>7}{r.count / wall:>9.1f}"
                f"{r.percentile(50):>8.0f}{r.percentile(95):>8.0f}"
                f"{r.percentile(99):>8.0f}{r.failures:>7}"
            )
        print(f"{'TOTAL':<32}{total:>7}{total / wall:>9.1f}"
              f"{'':>8}{'':>8}{'':>8}{failures:>7}")
        print(f"error rate: {error_rate * 100:.2f}%")

        for label in sorted(results):
            r = results[label]
            codes = ", ".join(f"{c}×{n}" for c, n in sorted(r.statuses.items()))
            errs = ", ".join(f"{e}×{n}" for e, n in sorted(r.transport_errors.items()))
            detail = " ".join(x for x in (codes, errs) if x)
            print(f"  {label}: {detail}")

        # --- outbox drain -------------------------------------------------
        drained_in: Optional[float] = None
        backlog_peak = backlog_before
        if backlog_before is not None:
            after = await outbox_backlog()
            backlog_peak = after
            print(f"\noutbox backlog: {backlog_before} before -> {after} after writes")
            started = time.perf_counter()
            while time.perf_counter() - started < args.drain_timeout:
                current = await outbox_backlog()
                if current is not None and current <= backlog_before:
                    drained_in = time.perf_counter() - started
                    break
                await asyncio.sleep(0.5)
            if drained_in is not None:
                print(f"  relay drained back to {backlog_before} in {drained_in:.1f}s")
            else:
                final = await outbox_backlog()
                print(f"  ⚠️  still {final} unpublished after "
                      f"{args.drain_timeout:.0f}s — the relay is not keeping up, "
                      f"so Elasticsearch is falling behind Postgres while every "
                      f"request still returns 200")
        else:
            print("\noutbox backlog: not measured (persistence layer dormant)")

        # --- thresholds ---------------------------------------------------
        failed = False
        if args.max_error_rate is not None and error_rate > args.max_error_rate:
            print(f"❌ error rate {error_rate * 100:.2f}% exceeds "
                  f"{args.max_error_rate * 100:.2f}%")
            failed = True
        if args.max_p95_ms is not None:
            for label in sorted(results):
                p95 = results[label].percentile(95)
                if p95 > args.max_p95_ms:
                    print(f"❌ {label} p95 {p95:.0f}ms exceeds {args.max_p95_ms:.0f}ms")
                    failed = True
        if backlog_before is not None and drained_in is None:
            print("❌ outbox did not drain within the timeout")
            failed = True
        if not failed:
            print("✅ within thresholds")
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
