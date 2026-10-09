"""Reader behaviour of the analytics methods on ``ElasticsearchService``
(F7, F8, F3 time series).

The real methods run against a stubbed ``search_documents`` so the
Python-side aggregation is exercised exactly as in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from services.elasticsearch_service import ElasticsearchService

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _reader(response: Dict[str, Any], captured: List[Dict[str, Any]]) -> ElasticsearchService:
    svc = ElasticsearchService.__new__(ElasticsearchService)

    async def _search(index, query, size=100):
        captured.append(query)
        return response

    svc.search_documents = _search
    return svc


def _route_doc(key: str, pct: float, orders: Any, *, label: str = None, at: datetime = NOW):
    src: Dict[str, Any] = {
        "event_type": "route_performance",
        "route_name": key,
        "timestamp": at.isoformat(),
        "metrics": {"performance_pct": pct},
    }
    if orders is not None:
        src["metrics"]["orders_scored"] = orders
    if label:
        src["route_label"] = label
    return {"_source": src}


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch):
    import services.elasticsearch_service as mod

    monkeypatch.setattr(mod, "utcnow", lambda: NOW)


@pytest.mark.asyncio
async def test_route_performance_is_weighted_by_orders_scored():
    """100% on a 1-order day and 0% on a 3-order day is 25%, not 50%."""
    hits = [_route_doc("RUN-1", 0.0, 3), _route_doc("RUN-1", 100.0, 1, at=NOW - timedelta(days=1))]
    captured: List[Dict[str, Any]] = []
    routes = await _reader({"hits": {"hits": hits}}, captured).get_route_performance_data("t")
    assert routes == [{"name": "RUN-1", "route_id": "RUN-1", "performance": 25.0, "orders_scored": 4}]


@pytest.mark.asyncio
async def test_route_window_parameter_is_applied_to_the_query():
    captured: List[Dict[str, Any]] = []
    await _reader({"hits": {"hits": []}}, captured).get_route_performance_data("t", days=7)
    (query,) = captured
    must = query["query"]["bool"]["must"]
    assert {"range": {"timestamp": {"gte": (NOW - timedelta(days=7)).isoformat()}}} in must
    assert {"term": {"tenant_id": "t"}} in query["query"]["bool"]["filter"]


@pytest.mark.asyncio
async def test_default_window_is_30_days():
    captured: List[Dict[str, Any]] = []
    await _reader({"hits": {"hits": []}}, captured).get_route_performance_data("t")
    must = captured[0]["query"]["bool"]["must"]
    assert {"range": {"timestamp": {"gte": (NOW - timedelta(days=30)).isoformat()}}} in must


@pytest.mark.asyncio
async def test_newest_route_label_is_preferred_and_legacy_docs_weigh_one():
    hits = [
        _route_doc("RUN-1", 50.0, 2, label="RUN-1 · Ada"),
        _route_doc("RUN-1", 100.0, None, label="old", at=NOW - timedelta(days=2)),
        _route_doc("D7", 0.0, 1),
    ]
    routes = await _reader({"hits": {"hits": hits}}, []).get_route_performance_data("t")
    assert routes[0] == {
        "name": "RUN-1 · Ada", "route_id": "RUN-1",
        "performance": round((50 * 2 + 100 * 1) / 3, 1), "orders_scored": 3,
    }
    assert routes[1]["name"] == "D7"


@pytest.mark.asyncio
async def test_top_10_by_orders_scored():
    hits = [_route_doc(f"R{i}", 50.0, i + 1) for i in range(12)]
    routes = await _reader({"hits": {"hits": hits}}, []).get_route_performance_data("t")
    assert len(routes) == 10
    assert routes[0]["route_id"] == "R11"
    assert "R0" not in {r["route_id"] for r in routes}


@pytest.mark.asyncio
async def test_regional_unknown_bucket_is_filtered():
    resp = {"aggregations": {"regions": {"buckets": [
        {"key": "TX", "avg_on_time": {"value": 50.0}},
        {"key": "UNKNOWN", "avg_on_time": {"value": 80.0}},
    ]}}}
    regions = await _reader(resp, []).get_regional_performance_data("t")
    assert regions == [{"name": "TX", "onTimePercentage": 50.0}]


@pytest.mark.asyncio
async def test_time_series_empty_bucket_is_null_not_zero():
    resp = {"aggregations": {"time_series": {"buckets": [
        {"key_as_string": "2026-10-05T00:00:00Z", "avg_metric": {"value": None}},
        {"key_as_string": "2026-10-06T00:00:00Z", "avg_metric": {"value": 0.0}},
        {"key_as_string": "2026-10-07T00:00:00Z", "avg_metric": {"value": 87.456}},
    ]}}}
    series = await _reader(resp, []).get_time_series_data(
        "t", "daily_performance", "delivery_performance_pct", "7d"
    )
    assert [p["value"] for p in series] == [None, 0.0, 87.46]


@pytest.mark.asyncio
async def test_current_metrics_snapshot_and_legacy_shape():
    doc = {"timestamp": "2026-10-08T09:00:00+00:00", "metrics": {
        "delivery_performance_pct": None, "average_delay_minutes": 30,
        "fleet_utilization_pct": 80.0,
    }}
    svc = _reader({"hits": {"hits": [{"_source": doc}]}}, [])
    snap = await svc.get_current_metrics_snapshot("t")
    assert snap["as_of"] == "2026-10-08T09:00:00+00:00"
    assert snap["metrics"]["delivery_performance"]["value"] is None
    assert snap["metrics"]["average_delay"]["value"] == "0.5 hrs"
    assert await svc.get_current_metrics("t") == snap["metrics"]


def _legacy_unknown_only_reader() -> ElasticsearchService:
    """Store holding only a legacy ``UNKNOWN`` regional bucket."""
    resp = {
        "hits": {"hits": []},
        "aggregations": {
            "causes": {"buckets": []},
            "regions": {"buckets": [{"key": "UNKNOWN", "avg_on_time": {"value": 100.0}}]},
        },
    }
    return _reader(resp, [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module, tool_name",
    [
        ("Agents.tools.summary_tools", "get_performance_insights"),
        ("Agents.tools.report_tools", "generate_performance_report"),
    ],
)
async def test_agent_reports_never_show_unknown_as_a_region(monkeypatch, module, tool_name):
    import importlib

    from Agents.tools._tenant_context import set_current_tenant

    mod = importlib.import_module(module)
    monkeypatch.setattr(mod, "elasticsearch_service", _legacy_unknown_only_reader())
    tool = getattr(mod, tool_name)
    fn = getattr(tool, "_tool_func", None) or getattr(tool, "original_function", None) or tool
    with set_current_tenant("t"):
        out = await fn()
    assert "UNKNOWN" not in out
    assert not out.startswith("Error"), out
    if tool_name == "get_performance_insights":
        assert "No regional performance data available yet" in out


@pytest.mark.asyncio
async def test_no_snapshot_is_none():
    svc = _reader({"hits": {"hits": []}}, [])
    assert await svc.get_current_metrics_snapshot("t") is None
    assert await svc.get_current_metrics("t") is None
