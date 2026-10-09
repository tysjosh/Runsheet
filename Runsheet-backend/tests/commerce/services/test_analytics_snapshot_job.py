"""Tests for ``commerce/services/analytics_snapshot_job.py`` (F14).

The scenario is the ``qa-tenant-b`` synthetic dataset from the 2026-10-08
analytics verification report, whose expected numbers were confirmed on
staging: 25.0% delivery performance, 60 min ("1.0 hrs") average delay,
66.7% fleet utilization, routes RUN-1 50.0 / RUN-2 0.0, delay causes
traffic 50 / customer_unavailable 25 / unreported 25.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from commerce.services.analytics_snapshot_job import (
    ANALYTICS_EVENTS_INDEX,
    run_analytics_snapshot_cycle,
)
from driver.services.driver_es_mappings import DRIVER_EXCEPTIONS_INDEX
from fuel.services.order_es_mappings import (
    DRIVERS_CURRENT_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TENANT = "qa-tenant-b"
OTHER = "qa-tenant-z"

TX = (32.78, -96.80)
OK = (35.47, -97.52)


def _order(
    order_id: str,
    *,
    tenant: str = TENANT,
    status: str = "delivered",
    run: Optional[str] = None,
    driver: Optional[str] = None,
    window_end: Optional[str] = "2026-10-08T10:00:00+00:00",
    delivered_at: Optional[str] = None,
    coords: Optional[tuple] = None,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "order_id": order_id,
        "tenant_id": tenant,
        "status": status,
        "updated_at": "2026-10-08T11:45:00+00:00",
        "assigned_run_id": run,
        "assigned_driver_id": driver,
        "delivery_window_end": window_end,
    }
    if delivered_at:
        doc["delivery_result"] = {"delivered_at": delivered_at}
    if coords:
        doc["ship_to_lat"], doc["ship_to_lon"] = coords
    return doc


QA_TENANT_B_ORDERS = [
    _order("QA-O1", run="RUN-1", driver="D1", delivered_at="2026-10-08T09:50:00+00:00", coords=TX),
    _order("QA-O2", run="RUN-1", driver="D1", delivered_at="2026-10-08T10:30:00+00:00", coords=TX),
    _order("QA-O3", run="RUN-2", driver="D2", delivered_at="2026-10-08T11:30:00+00:00", coords=OK),
    _order("QA-O4", run="RUN-2", driver="D2", status="failed", coords=OK),
    _order("QA-O5", run="RUN-3", driver="D3", window_end=None,
           delivered_at="2026-10-08T11:00:00+00:00", coords=TX),
]

QA_TENANT_B_DRIVERS = [
    {"driver_id": "D1", "tenant_id": TENANT, "driver_name": "Ada", "status": "active"},
    {"driver_id": "D2", "tenant_id": TENANT, "driver_name": "Bo", "status": "idle",
     "active_order_count": 1},
    {"driver_id": "D3", "tenant_id": TENANT, "driver_name": "Cy", "status": "idle"},
]

QA_TENANT_B_EXCEPTIONS = [
    {"tenant_id": TENANT, "order_id": "QA-O2", "exception_type": "traffic"},
    {"tenant_id": TENANT, "order_id": "QA-O3", "exception_type": "traffic"},
    {"tenant_id": TENANT, "order_id": "QA-O3", "exception_type": "customer_unavailable"},
]


def _tenant_of(query: Dict[str, Any]) -> Optional[str]:
    for clause in query.get("query", {}).get("bool", {}).get("must", []):
        if "term" in clause and "tenant_id" in clause["term"]:
            return clause["term"]["tenant_id"]
    return None


def _terms(query: Dict[str, Any], field: str) -> Optional[List[Any]]:
    for clause in query.get("query", {}).get("bool", {}).get("must", []):
        if "terms" in clause and field in clause["terms"]:
            return clause["terms"][field]
    return None


class _FakeES:
    """Dispatches ``search_documents`` by index and captures writes."""

    def __init__(self, orders, drivers, exceptions, *, fail_scan: bool = False,
                 fail_tenants: tuple = ()):
        self.orders = orders
        self.drivers = drivers
        self.exceptions = exceptions
        self.fail_scan = fail_scan
        self.fail_tenants = set(fail_tenants)
        self.writes: Dict[str, Dict[str, Any]] = {}

    async def search_documents(self, index, query, size=100):
        if index == FUEL_ORDERS_CURRENT_INDEX and "aggs" in query:
            if self.fail_scan:
                raise RuntimeError("db down")
            tenants = sorted({o["tenant_id"] for o in self.orders})
            return {"aggregations": {"tenants": {"buckets": [{"key": t} for t in tenants]}}}
        tenant = _tenant_of(query)
        if tenant in self.fail_tenants:
            raise RuntimeError(f"boom {tenant}")
        if index == FUEL_ORDERS_CURRENT_INDEX:
            statuses = _terms(query, "status") or []
            rows = [o for o in self.orders if o["tenant_id"] == tenant and o["status"] in statuses]
        elif index == DRIVER_EXCEPTIONS_INDEX:
            ids = set(_terms(query, "order_id") or [])
            rows = [e for e in self.exceptions if e["tenant_id"] == tenant and e["order_id"] in ids]
        elif index == DRIVERS_CURRENT_INDEX:
            rows = [d for d in self.drivers if d["tenant_id"] == tenant]
        else:  # pragma: no cover - unexpected index
            raise AssertionError(index)
        return {"hits": {"hits": [{"_source": r} for r in rows]}}

    async def index_document(self, index, doc_id, doc):
        assert index == ANALYTICS_EVENTS_INDEX
        self.writes[doc_id] = doc

    def of(self, event_type: str, tenant: str = TENANT) -> List[Dict[str, Any]]:
        return [
            d for d in self.writes.values()
            if d["event_type"] == event_type and d["tenant_id"] == tenant
        ]


class _Detector:
    def get_state(self, lat, lon):
        return {TX: "TX", OK: "OK"}.get((lat, lon))


async def _run(es: _FakeES) -> int:
    return await run_analytics_snapshot_cycle(es, state_boundary_detector=_Detector(), now=NOW)


@pytest.fixture
def es() -> _FakeES:
    return _FakeES(QA_TENANT_B_ORDERS, QA_TENANT_B_DRIVERS, QA_TENANT_B_EXCEPTIONS)


@pytest.mark.asyncio
async def test_daily_performance_matches_the_qa_tenant_b_hand_computation(es):
    assert await _run(es) == 1
    (daily,) = es.of("daily_performance")
    assert daily["metrics"] == {
        "delivery_performance_pct": 25.0,  # 1 of 4 orders with a window
        "average_delay_minutes": 60.0,  # mean(30, 90); failed order excluded
        "fleet_utilization_pct": 66.7,  # D1 active, D2 has an active order
        "orders_scored": 4,
        "orders_total": 5,
    }
    assert daily["event_id"] == f"daily_performance_{TENANT}_2026-10-08"


@pytest.mark.asyncio
async def test_current_metrics_reader_renders_the_snapshot(es):
    from services.elasticsearch_service import ElasticsearchService

    await _run(es)
    (daily,) = es.of("daily_performance")
    reader = ElasticsearchService.__new__(ElasticsearchService)

    async def _search(index, query, size=100):
        return {"hits": {"hits": [{"_source": daily}]}}

    reader.search_documents = _search
    snapshot = await reader.get_current_metrics_snapshot(TENANT)
    assert snapshot["as_of"] == NOW.isoformat()
    assert snapshot["metrics"]["delivery_performance"]["value"] == "25.0%"
    assert snapshot["metrics"]["average_delay"]["value"] == "1.0 hrs"
    assert snapshot["metrics"]["fleet_utilization"]["value"] == "66.7%"


@pytest.mark.asyncio
async def test_route_performance_and_human_labels(es):
    await _run(es)
    routes = {d["route_name"]: d for d in es.of("route_performance")}
    assert set(routes) == {"RUN-1", "RUN-2"}  # RUN-3's only order had no window
    assert routes["RUN-1"]["metrics"] == {"performance_pct": 50.0, "orders_scored": 2}
    assert routes["RUN-2"]["metrics"] == {"performance_pct": 0.0, "orders_scored": 2}
    assert routes["RUN-1"]["route_label"] == "RUN-1 · Ada"
    assert routes["RUN-2"]["route_label"] == "RUN-2 · Bo"
    assert routes["RUN-1"]["event_id"].endswith("_2026-10-08")


@pytest.mark.asyncio
async def test_driver_keyed_route_is_labelled_with_the_driver_name():
    orders = [_order("QA-O1", driver="D1", delivered_at="2026-10-08T09:00:00+00:00")]
    es = _FakeES(orders, QA_TENANT_B_DRIVERS, [])
    await _run(es)
    (route,) = es.of("route_performance")
    assert route["route_name"] == "D1"
    assert route["route_label"] == "Ada"


@pytest.mark.asyncio
async def test_delay_causes(es):
    await _run(es)
    causes = {d["delay_cause"]: d["metrics"]["percentage"] for d in es.of("delay_cause_analysis")}
    assert causes == {"traffic": 50.0, "customer_unavailable": 25.0, "unreported": 25.0}


@pytest.mark.asyncio
async def test_regional_performance_never_writes_unknown(es):
    await _run(es)
    regions = {d["region"]: d["metrics"]["on_time_percentage"] for d in es.of("regional_performance")}
    assert regions == {"TX": 50.0, "OK": 0.0}


@pytest.mark.asyncio
async def test_an_order_without_coordinates_gets_no_regional_doc():
    orders = [
        _order("QA-O1", run="RUN-1", driver="D1", delivered_at="2026-10-08T09:00:00+00:00"),
        _order("QA-O2", run="RUN-1", driver="D1", delivered_at="2026-10-08T09:00:00+00:00",
               coords=(0.0, 0.0)),  # detector cannot resolve
    ]
    es = _FakeES(orders, QA_TENANT_B_DRIVERS, [])
    await _run(es)
    assert es.of("regional_performance") == []
    assert not any("UNKNOWN" in doc_id for doc_id in es.writes)
    # The orders still count everywhere else.
    (daily,) = es.of("daily_performance")
    assert daily["metrics"]["delivery_performance_pct"] == 100.0


@pytest.mark.asyncio
async def test_tenants_are_snapshotted_in_isolation():
    other = [_order("QA-Z1", tenant=OTHER, run="RUN-9", driver="DZ",
                    delivered_at="2026-10-08T09:00:00+00:00")]
    es = _FakeES(QA_TENANT_B_ORDERS + other, QA_TENANT_B_DRIVERS, QA_TENANT_B_EXCEPTIONS)
    assert await _run(es) == 2
    (mine,) = es.of("daily_performance")
    (theirs,) = es.of("daily_performance", tenant=OTHER)
    assert mine["metrics"]["delivery_performance_pct"] == 25.0
    assert theirs["metrics"]["delivery_performance_pct"] == 100.0
    assert theirs["metrics"]["fleet_utilization_pct"] is None  # no drivers on record
    assert {d["route_name"] for d in es.of("route_performance", tenant=OTHER)} == {"RUN-9"}


@pytest.mark.asyncio
async def test_tenant_scan_failure_raises(es):
    es.fail_scan = True
    with pytest.raises(RuntimeError, match="tenant scan failed"):
        await _run(es)


@pytest.mark.asyncio
async def test_partial_tenant_failure_is_a_logged_success(monkeypatch):
    import commerce.services.analytics_snapshot_job as job

    other = [_order("QA-Z1", tenant=OTHER, delivered_at="2026-10-08T09:00:00+00:00")]
    es = _FakeES(QA_TENANT_B_ORDERS + other, QA_TENANT_B_DRIVERS, QA_TENANT_B_EXCEPTIONS)
    original = job._write_daily_performance

    async def _write(es_service, **kwargs):
        if kwargs["tenant_id"] == OTHER:
            raise RuntimeError("write failed")
        await original(es_service, **kwargs)

    monkeypatch.setattr(job, "_write_daily_performance", _write)
    assert await _run(es) == 1
    assert len(es.of("daily_performance")) == 1


@pytest.mark.asyncio
async def test_every_tenant_failing_raises(monkeypatch, es):
    import commerce.services.analytics_snapshot_job as job

    async def _fail(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(job, "_write_daily_performance", _fail)
    with pytest.raises(RuntimeError, match="all 1 tenant"):
        await _run(es)
