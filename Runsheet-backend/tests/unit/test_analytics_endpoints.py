"""``/api/analytics/{metrics,routes,timeseries}`` (F3 as-of + series, F8
window, F13 error envelope)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import data_endpoints
from errors.exceptions import circuit_open
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


class _FakeES:
    def __init__(self) -> None:
        self.snapshot: Optional[Dict[str, Any]] = None
        self.raise_exc: Optional[BaseException] = None
        self.route_calls: List[Dict[str, Any]] = []
        self.series_calls: List[tuple] = []
        self.series: List[Dict[str, Any]] = []

    def _maybe_raise(self) -> None:
        if self.raise_exc is not None:
            raise self.raise_exc

    async def get_current_metrics_snapshot(self, tenant_id):
        self._maybe_raise()
        return self.snapshot

    async def get_route_performance_data(self, tenant_id, days=30):
        self._maybe_raise()
        self.route_calls.append({"tenant": tenant_id, "days": days})
        return [{"name": "RUN-1 · Ada", "route_id": "RUN-1", "performance": 50.0, "orders_scored": 2}]

    async def get_time_series_data(self, tenant_id, event_type, metric_field, time_range="7d"):
        self._maybe_raise()
        self.series_calls.append((tenant_id, event_type, metric_field, time_range))
        return self.series


@pytest.fixture
def fake_es(monkeypatch):
    fake = _FakeES()
    monkeypatch.setattr(data_endpoints, "elasticsearch_service", fake)
    return fake


@pytest.fixture
def client(fake_es):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(data_endpoints.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id="tenant-a", user_id="user-a", has_pii_access=False, roles=["dispatcher"],
    )
    return TestClient(app, raise_server_exceptions=False)


# -- /analytics/metrics ------------------------------------------------------


def test_metrics_returns_as_of_from_the_snapshot(client, fake_es):
    fake_es.snapshot = {
        "as_of": "2026-10-08T19:41:00+00:00",
        "metrics": {"delivery_performance": {"title": "Delivery Performance", "value": "25.0%"}},
    }
    body = client.get("/api/analytics/metrics").json()
    assert body["success"] is True
    assert body["as_of"] == "2026-10-08T19:41:00+00:00"
    assert body["data"]["delivery_performance"]["value"] == "25.0%"


def test_no_snapshot_is_200_with_null_data(client, fake_es):
    resp = client.get("/api/analytics/metrics")
    assert resp.status_code == 200
    assert resp.json()["data"] is None
    assert resp.json()["as_of"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/analytics/metrics",
        "/api/analytics/routes",
        "/api/analytics/timeseries?metric=delivery_performance",
    ],
)
def test_a_reader_failure_is_an_error_envelope_not_an_empty_success(client, fake_es, path):
    fake_es.raise_exc = RuntimeError("db down")
    resp = client.get(path)
    assert resp.status_code >= 500
    body = resp.json()
    assert body.get("success") is not True
    assert body["error_code"] == "INTERNAL_ERROR"
    assert "db down" not in resp.text


def test_an_app_exception_keeps_its_own_code(client, fake_es):
    fake_es.raise_exc = circuit_open()
    resp = client.get("/api/analytics/metrics")
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "CIRCUIT_OPEN"


# -- /analytics/routes --------------------------------------------------------


def test_routes_default_window_is_30_days(client, fake_es):
    body = client.get("/api/analytics/routes").json()
    assert fake_es.route_calls == [{"tenant": "tenant-a", "days": 30}]
    assert body["data"][0]["name"] == "RUN-1 · Ada"


def test_routes_pass_days_through(client, fake_es):
    assert client.get("/api/analytics/routes", params={"days": 7}).status_code == 200
    assert fake_es.route_calls == [{"tenant": "tenant-a", "days": 7}]


@pytest.mark.parametrize("days", ["0", "366", "x"])
def test_routes_reject_out_of_range_days(client, fake_es, days):
    assert client.get("/api/analytics/routes", params={"days": days}).status_code in (400, 422)
    assert fake_es.route_calls == []


# -- /analytics/timeseries ---------------------------------------------------


@pytest.mark.parametrize(
    "metric, field, unit",
    [
        ("delivery_performance", "delivery_performance_pct", "%"),
        ("average_delay", "average_delay_minutes", "minutes"),
        ("fleet_utilization", "fleet_utilization_pct", "%"),
    ],
)
def test_timeseries_maps_metric_to_field(client, fake_es, metric, field, unit):
    body = client.get("/api/analytics/timeseries", params={"metric": metric, "range": "7d"}).json()
    assert fake_es.series_calls == [("tenant-a", "daily_performance", field, "7d")]
    assert body["metric"] == metric
    assert body["unit"] == unit


def test_timeseries_default_range_is_30d(client, fake_es):
    client.get("/api/analytics/timeseries", params={"metric": "average_delay"})
    assert fake_es.series_calls[0][3] == "30d"


def test_timeseries_empty_buckets_come_back_as_null(client, fake_es):
    fake_es.series = [
        {"timestamp": "2026-10-06T00:00:00.000Z", "value": None},
        {"timestamp": "2026-10-07T00:00:00.000Z", "value": 80.0},
    ]
    body = client.get("/api/analytics/timeseries", params={"metric": "fleet_utilization"}).json()
    assert [p["value"] for p in body["data"]] == [None, 80.0]


@pytest.mark.parametrize(
    "params",
    [
        {"metric": "customer_satisfaction"},
        {"metric": "delivery_performance", "range": "24h"},
        {"metric": "delivery_performance", "range": "1y"},
        {},
    ],
)
def test_timeseries_rejects_unknown_metric_or_range(client, fake_es, params):
    assert client.get("/api/analytics/timeseries", params=params).status_code in (400, 422)
    assert fake_es.series_calls == []
