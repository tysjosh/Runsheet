"""Scheduled job that computes real analytics_events snapshots from order data.

Runs daily and, for every tenant with order activity (discovered via a
terms aggregation on ``fuel_orders_current``), computes and writes:

* ``daily_performance`` — on-time delivery %, average delay minutes,
  fleet utilization %, from actual delivered/failed orders and driver
  status in the trailing 24-hour window.
* ``route_performance`` — on-time % per dispatch run (``assigned_run_id``,
  falling back to ``assigned_driver_id`` when no run id was recorded).
* ``delay_cause_analysis`` — the field-reported exception types
  (``driver_exceptions.exception_type``) behind delivered/failed orders
  that ran late, as a percentage share.
* ``regional_performance`` — on-time % per US state, resolved from each
  order's ``ship_to_lat``/``ship_to_lon`` via ``StateBoundaryDetector``.

This replaces the seed-only ``analytics_events`` documents that
``services/data_seeder.py`` and ``seed_all_data.py`` wrote for demo
purposes. Those seeders are unaffected — a tenant with no real order
history simply gets no snapshot, and ``get_current_metrics`` /
``get_route_performance_data`` / etc. degrade to an explicit "no data"
result rather than a fabricated fallback value.

On-time is measured against the order's own promise: a delivered order
is on-time when ``delivery_result.delivered_at <= delivery_window_end``
(orders with no window recorded are excluded from on-time scoring but
still counted in the total). ``average_delay_minutes`` is the mean of
``delivered_at - delivery_window_end`` in minutes across *late* orders
only, clamped at zero.

``customer_satisfaction`` is intentionally NOT produced here — there is
no survey/rating feature anywhere in this codebase, so fabricating a
number would be worse than omitting it. Callers must treat a missing
``customer_satisfaction`` metric as "not available", not zero.

Regional bucketing degrades to an ``"UNKNOWN"`` bucket when
``StateBoundaryDetector`` cannot resolve a state (e.g. the TIGER
shapefile is not installed in this environment, or the order has no
ship-to coordinates) rather than raising or silently dropping the order
from the total.

Registered via the existing scheduler infrastructure (asyncio background
task pattern used throughout bootstrap/), following the same shape as
``ar_aging_snapshot_job.py`` / ``invoice_overdue_job.py``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from driver.services.driver_es_mappings import DRIVER_EXCEPTIONS_INDEX
from fuel.services.order_es_mappings import (
    DRIVERS_CURRENT_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# Interval between snapshot runs (seconds). Daily, matching the AR-aging
# snapshot cadence — these are day-level performance rollups, not
# real-time metrics.
ANALYTICS_SNAPSHOT_INTERVAL_SECONDS = 86400  # 24 hours

#: Trailing window scored on each run. A day-over-day rollup: every run
#: scores "the last 24 hours" rather than accumulating a growing window.
_LOOKBACK_HOURS = 24

#: Elasticsearch/document-store index this job writes to. Matches the
#: index name the existing ``get_current_metrics`` / ``get_route_
#: performance_data`` / ``get_delay_causes_data`` /
#: ``get_regional_performance_data`` readers already query.
ANALYTICS_EVENTS_INDEX = "analytics_events"

#: Statuses that resolve an order (no longer active) and are therefore
#: eligible for performance scoring.
_TERMINAL_STATUSES = ("delivered", "failed")

_MAX_SCAN_SIZE = 2000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def run_analytics_snapshot_cycle(
    es_service: ElasticsearchService,
    *,
    state_boundary_detector: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> int:
    """Compute and write daily analytics snapshots for every active tenant.

    Args:
        es_service: The document-store-backed ES facade.
        state_boundary_detector: Optional injected
            :class:`compliance.services.state_boundary_detector.StateBoundaryDetector`.
            Lazily constructed when omitted. Passing an instance lets
            callers share the one already loaded by the compliance
            domain rather than reloading shapefile data per job run.
        now: Injectable clock for deterministic tests.

    Returns:
        The number of tenants for which a snapshot was written.
    """
    reference_now = now or _utcnow()
    window_start = reference_now - timedelta(hours=_LOOKBACK_HOURS)

    # Discover tenants with order activity via a terms aggregation —
    # same pattern as ar_aging_snapshot_job.py / invoice_overdue_job.py.
    tenant_query: Dict[str, Any] = {
        "size": 0,
        "aggs": {
            "tenants": {
                "terms": {"field": "tenant_id", "size": 10000},
            }
        },
    }
    try:
        tenant_resp = await es_service.search_documents(
            FUEL_ORDERS_CURRENT_INDEX, tenant_query, size=0
        )
    except Exception as exc:
        logger.error("Analytics snapshot tenant scan failed: %s", exc)
        # Raise so run_periodic(record="success") retries instead of
        # recording a successful run.
        raise RuntimeError("Analytics snapshot tenant scan failed") from exc

    buckets = (
        tenant_resp.get("aggregations", {}).get("tenants", {}).get("buckets", [])
    )
    tenant_ids = [b["key"] for b in buckets if b.get("key")]
    if not tenant_ids:
        logger.debug("No tenants with order data found for analytics snapshot")
        return 0

    if state_boundary_detector is None:
        from compliance.services.state_boundary_detector import (
            StateBoundaryDetector,
        )

        state_boundary_detector = StateBoundaryDetector()

    snapshot_count = 0
    for tenant_id in tenant_ids:
        try:
            await _snapshot_tenant(
                es_service,
                tenant_id=tenant_id,
                window_start=window_start,
                reference_now=reference_now,
                state_boundary_detector=state_boundary_detector,
            )
            snapshot_count += 1
        except Exception as exc:
            logger.error(
                "Analytics snapshot failed for tenant %s: %s", tenant_id, exc
            )

    if snapshot_count == 0:
        # Every tenant failed; partial failure stays a logged success.
        raise RuntimeError(
            f"Analytics snapshot failed for all {len(tenant_ids)} tenant(s)"
        )
    logger.info(
        "Analytics snapshot cycle complete: %d tenant(s) snapshotted",
        snapshot_count,
    )
    return snapshot_count


async def _snapshot_tenant(
    es_service: ElasticsearchService,
    *,
    tenant_id: str,
    window_start: datetime,
    reference_now: datetime,
    state_boundary_detector: Any,
) -> None:
    orders = await _query_terminal_orders(
        es_service, tenant_id=tenant_id, window_start=window_start
    )
    exceptions_by_order = await _query_exceptions_by_order(
        es_service, tenant_id=tenant_id, order_ids=[o["order_id"] for o in orders]
    )
    drivers = await _query_drivers(es_service, tenant_id=tenant_id)

    scored = [_score_order(o) for o in orders]
    scored = [s for s in scored if s is not None]

    await _write_daily_performance(
        es_service,
        tenant_id=tenant_id,
        scored=scored,
        drivers=drivers,
        reference_now=reference_now,
    )
    await _write_route_performance(
        es_service, tenant_id=tenant_id, scored=scored, reference_now=reference_now
    )
    await _write_delay_cause_analysis(
        es_service,
        tenant_id=tenant_id,
        scored=scored,
        exceptions_by_order=exceptions_by_order,
        reference_now=reference_now,
    )
    await _write_regional_performance(
        es_service,
        tenant_id=tenant_id,
        scored=scored,
        state_boundary_detector=state_boundary_detector,
        reference_now=reference_now,
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def _query_terminal_orders(
    es_service: ElasticsearchService,
    *,
    tenant_id: str,
    window_start: datetime,
) -> List[Dict[str, Any]]:
    """Return delivered/failed orders for ``tenant_id`` resolved since ``window_start``.

    Filters on ``updated_at`` (the timestamp stamped by every
    ``apply_status_transition`` call) rather than ``delivery_result.
    delivered_at`` so failed orders — which carry no delivery_result —
    are captured too.
    """
    query: Dict[str, Any] = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"terms": {"status": list(_TERMINAL_STATUSES)}},
                    {"range": {"updated_at": {"gte": window_start.isoformat()}}},
                ]
            }
        },
        "size": _MAX_SCAN_SIZE,
    }
    try:
        resp = await es_service.search_documents(
            FUEL_ORDERS_CURRENT_INDEX, query, size=_MAX_SCAN_SIZE
        )
    except Exception as exc:
        logger.warning(
            "Analytics snapshot: order query failed for tenant=%s: %s",
            tenant_id,
            exc,
        )
        return []
    return [h["_source"] for h in resp.get("hits", {}).get("hits", [])]


async def _query_exceptions_by_order(
    es_service: ElasticsearchService,
    *,
    tenant_id: str,
    order_ids: List[str],
) -> Dict[str, List[str]]:
    """Return ``{order_id: [exception_type, ...]}`` for the given orders.

    A single query with a bounded IN-list rather than N per-order queries.
    Degrades to an empty map on failure so a broken exceptions index never
    blocks the rest of the snapshot.
    """
    if not order_ids:
        return {}
    query: Dict[str, Any] = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"terms": {"order_id": order_ids}},
                ]
            }
        },
        "size": _MAX_SCAN_SIZE,
    }
    try:
        resp = await es_service.search_documents(
            DRIVER_EXCEPTIONS_INDEX, query, size=_MAX_SCAN_SIZE
        )
    except Exception as exc:
        logger.warning(
            "Analytics snapshot: exceptions query failed for tenant=%s: %s",
            tenant_id,
            exc,
        )
        return {}

    out: Dict[str, List[str]] = defaultdict(list)
    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source", {})
        order_id = source.get("order_id")
        exc_type = source.get("exception_type")
        if order_id and exc_type:
            out[order_id].append(exc_type)
    return dict(out)


async def _query_drivers(
    es_service: ElasticsearchService, *, tenant_id: str
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {
        "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
        "size": _MAX_SCAN_SIZE,
    }
    try:
        resp = await es_service.search_documents(
            DRIVERS_CURRENT_INDEX, query, size=_MAX_SCAN_SIZE
        )
    except Exception as exc:
        logger.warning(
            "Analytics snapshot: driver query failed for tenant=%s: %s",
            tenant_id,
            exc,
        )
        return []
    return [h["_source"] for h in resp.get("hits", {}).get("hits", [])]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class _ScoredOrder:
    """On-time scoring result for a single terminal order."""

    __slots__ = (
        "order_id",
        "status",
        "on_time",
        "delay_minutes",
        "route_key",
        "lat",
        "lon",
        "has_window",
    )

    def __init__(
        self,
        *,
        order_id: str,
        status: str,
        on_time: Optional[bool],
        delay_minutes: float,
        route_key: str,
        lat: Optional[float],
        lon: Optional[float],
        has_window: bool,
    ) -> None:
        self.order_id = order_id
        self.status = status
        self.on_time = on_time
        self.delay_minutes = delay_minutes
        self.route_key = route_key
        self.lat = lat
        self.lon = lon
        self.has_window = has_window


def _score_order(order: Dict[str, Any]) -> Optional[_ScoredOrder]:
    """Score one delivered/failed order for on-time performance.

    Returns ``None`` for a malformed record (missing order_id) rather
    than raising, so one bad document never drops the whole snapshot.

    On-time is measured against ``delivery_window_end`` when present:
        * ``delivered`` with ``delivery_result.delivered_at`` <=
          ``delivery_window_end`` → on time.
        * ``delivered`` later than the window, or any ``failed`` order
          that had a window → late.
        * No window recorded on the order → excluded from on-time
          scoring (``on_time=None``) but still counted for delay-cause /
          regional bucketing.
    """
    order_id = order.get("order_id")
    if not order_id:
        return None
    status = order.get("status", "")

    window_end = _parse_dt(order.get("delivery_window_end"))
    has_window = window_end is not None

    on_time: Optional[bool] = None
    delay_minutes = 0.0

    if has_window:
        if status == "delivered":
            delivery_result = order.get("delivery_result") or {}
            delivered_at = _parse_dt(delivery_result.get("delivered_at"))
            if delivered_at is not None:
                late_seconds = (delivered_at - window_end).total_seconds()
                on_time = late_seconds <= 0
                if not on_time:
                    delay_minutes = late_seconds / 60.0
            # A delivered order with no delivery_result (shouldn't happen
            # post-POD, but a legacy/backfilled record could lack it) is
            # excluded from on-time scoring rather than guessed at.
        elif status == "failed":
            # A failed order that had a promised window missed it by
            # definition — scored as late with no measurable delay
            # duration (there is no delivered_at to diff against).
            on_time = False

    route_key = order.get("assigned_run_id") or order.get("assigned_driver_id") or "unassigned"

    return _ScoredOrder(
        order_id=order_id,
        status=status,
        on_time=on_time,
        delay_minutes=max(0.0, delay_minutes),
        route_key=route_key,
        lat=order.get("ship_to_lat"),
        lon=order.get("ship_to_lon"),
        has_window=has_window,
    )


# ---------------------------------------------------------------------------
# Writers — each produces one analytics_events document per tenant/bucket.
# ---------------------------------------------------------------------------


async def _index_event(
    es_service: ElasticsearchService,
    *,
    event_id: str,
    tenant_id: str,
    event_type: str,
    timestamp: datetime,
    fields: Dict[str, Any],
) -> None:
    doc = {
        "event_id": event_id,
        "event_type": event_type,
        "tenant_id": tenant_id,
        "timestamp": timestamp.isoformat(),
        **fields,
    }
    await es_service.index_document(ANALYTICS_EVENTS_INDEX, event_id, doc)


async def _write_daily_performance(
    es_service: ElasticsearchService,
    *,
    tenant_id: str,
    scored: List["_ScoredOrder"],
    drivers: List[Dict[str, Any]],
    reference_now: datetime,
) -> None:
    scoreable = [s for s in scored if s.on_time is not None]
    delivery_performance_pct = (
        round(100.0 * sum(1 for s in scoreable if s.on_time) / len(scoreable), 1)
        if scoreable
        else None
    )
    late = [s for s in scoreable if not s.on_time and s.delay_minutes > 0]
    average_delay_minutes = (
        round(sum(s.delay_minutes for s in late) / len(late), 1) if late else 0.0
    )

    active_drivers = sum(
        1
        for d in drivers
        if d.get("status") == "active" or (d.get("active_order_count") or 0) > 0
    )
    fleet_utilization_pct = (
        round(100.0 * active_drivers / len(drivers), 1) if drivers else None
    )

    # customer_satisfaction is intentionally omitted — no real data source
    # exists yet (see module docstring). Readers must treat its absence as
    # "not available", not zero.
    metrics: Dict[str, Any] = {
        "delivery_performance_pct": delivery_performance_pct,
        "average_delay_minutes": average_delay_minutes,
        "fleet_utilization_pct": fleet_utilization_pct,
        "orders_scored": len(scoreable),
        "orders_total": len(scored),
    }

    await _index_event(
        es_service,
        event_id=f"daily_performance_{tenant_id}_{reference_now.date().isoformat()}",
        tenant_id=tenant_id,
        event_type="daily_performance",
        timestamp=reference_now,
        fields={"metrics": metrics},
    )


async def _write_route_performance(
    es_service: ElasticsearchService,
    *,
    tenant_id: str,
    scored: List["_ScoredOrder"],
    reference_now: datetime,
) -> None:
    by_route: Dict[str, List["_ScoredOrder"]] = defaultdict(list)
    for s in scored:
        if s.on_time is not None:
            by_route[s.route_key].append(s)

    for route_key, entries in by_route.items():
        performance_pct = round(
            100.0 * sum(1 for e in entries if e.on_time) / len(entries), 1
        )
        await _index_event(
            es_service,
            event_id=(
                f"route_performance_{tenant_id}_{route_key}_"
                f"{reference_now.date().isoformat()}"
            ),
            tenant_id=tenant_id,
            event_type="route_performance",
            timestamp=reference_now,
            fields={
                "route_name": route_key,
                "metrics": {
                    "performance_pct": performance_pct,
                    "orders_scored": len(entries),
                },
            },
        )


async def _write_delay_cause_analysis(
    es_service: ElasticsearchService,
    *,
    tenant_id: str,
    scored: List["_ScoredOrder"],
    exceptions_by_order: Dict[str, List[str]],
    reference_now: datetime,
) -> None:
    late_order_ids = {s.order_id for s in scored if s.on_time is False}
    if not late_order_ids:
        return

    cause_counts: Dict[str, int] = defaultdict(int)
    attributed = 0
    for order_id in late_order_ids:
        types = exceptions_by_order.get(order_id) or []
        if not types:
            continue
        # An order can carry more than one reported exception; count each
        # distinct type once per order so a chatty order does not dominate
        # the distribution.
        for exc_type in set(types):
            cause_counts[exc_type] += 1
        attributed += 1

    unattributed = len(late_order_ids) - attributed
    if unattributed > 0:
        cause_counts["unreported"] += unattributed

    total = sum(cause_counts.values())
    if total == 0:
        return

    for cause, count in cause_counts.items():
        percentage = round(100.0 * count / total, 1)
        await _index_event(
            es_service,
            event_id=(
                f"delay_cause_{tenant_id}_{cause}_"
                f"{reference_now.date().isoformat()}"
            ),
            tenant_id=tenant_id,
            event_type="delay_cause_analysis",
            timestamp=reference_now,
            fields={
                "delay_cause": cause,
                "metrics": {"percentage": percentage, "orders": count},
            },
        )


async def _write_regional_performance(
    es_service: ElasticsearchService,
    *,
    tenant_id: str,
    scored: List["_ScoredOrder"],
    state_boundary_detector: Any,
    reference_now: datetime,
) -> None:
    by_region: Dict[str, List["_ScoredOrder"]] = defaultdict(list)
    for s in scored:
        if s.on_time is None:
            continue
        region = None
        if s.lat is not None and s.lon is not None:
            try:
                region = state_boundary_detector.get_state(s.lat, s.lon)
            except Exception:
                region = None
        by_region[region or "UNKNOWN"].append(s)

    for region, entries in by_region.items():
        on_time_pct = round(
            100.0 * sum(1 for e in entries if e.on_time) / len(entries), 1
        )
        await _index_event(
            es_service,
            event_id=(
                f"regional_performance_{tenant_id}_{region}_"
                f"{reference_now.date().isoformat()}"
            ),
            tenant_id=tenant_id,
            event_type="regional_performance",
            timestamp=reference_now,
            fields={
                "region": region,
                "metrics": {
                    "on_time_percentage": on_time_pct,
                    "orders_scored": len(entries),
                },
            },
        )
