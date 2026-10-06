"""B6 and info items on ``data_endpoints.py``.

- ``/api/search`` ``limit`` is bounded to 1..100 (B6).
- ``/api/analytics/metrics`` no longer declares the unused ``timeRange``; a
  client still sending it gets 200 because FastAPI ignores unknown params.
- ``/api/search/universal`` logs a failing entity group and returns ``[]``
  for it (regression guard; the behaviour already existed).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import data_endpoints
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


class _FakeES:
    def __init__(self) -> None:
        self.semantic_sizes: List[int] = []

    async def semantic_search(self, tenant_id, index, text, fields, size=10):
        self.semantic_sizes.append(size)
        return []

    async def get_current_metrics(self, tenant_id) -> Dict[str, Any]:
        return {"tenant": tenant_id}


@pytest.fixture
def fake_es(monkeypatch):
    fake = _FakeES()
    monkeypatch.setattr(data_endpoints, "elasticsearch_service", fake)
    return fake


@pytest.fixture
def client(fake_es):
    app = FastAPI()
    app.include_router(data_endpoints.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id="tenant-a",
        user_id="user-a",
        has_pii_access=False,
        roles=["dispatcher"],
    )
    return TestClient(app)


@pytest.mark.parametrize("limit", ["0", "-1", "101"])
def test_search_limit_out_of_bounds_is_422(client, fake_es, limit):
    response = client.get("/api/search", params={"q": "x", "limit": limit})

    assert response.status_code == 422
    assert fake_es.semantic_sizes == []


@pytest.mark.parametrize("limit", ["1", "100"])
def test_search_limit_in_bounds_is_passed_through(client, fake_es, limit):
    response = client.get("/api/search", params={"q": "x", "limit": limit})

    assert response.status_code == 200
    assert fake_es.semantic_sizes == [int(limit)]


def test_search_limit_default_is_10(client, fake_es):
    assert client.get("/api/search", params={"q": "x"}).status_code == 200
    assert fake_es.semantic_sizes == [10]


def test_metrics_ignores_a_legacy_time_range_param(client):
    response = client.get("/api/analytics/metrics", params={"timeRange": "bogus"})

    assert response.status_code == 200
    assert response.json()["data"] == {"tenant": "tenant-a"}


def test_universal_search_logs_a_failing_group_and_returns_empty(
    client, monkeypatch, caplog
):
    async def _boom(tenant_id, q, limit):
        raise RuntimeError("commerce disabled")

    async def _empty(tenant_id, q, limit):
        return []

    monkeypatch.setattr(data_endpoints, "_universal_search_customers", _boom)
    monkeypatch.setattr(data_endpoints, "_universal_search_orders", _empty)
    monkeypatch.setattr(data_endpoints, "_universal_search_assets", _empty)

    with caplog.at_level(logging.WARNING, logger=data_endpoints.logger.name):
        response = client.get("/api/search/universal", params={"q": "acme"})

    assert response.status_code == 200
    assert response.json()["data"]["customers"] == []
    assert any(
        "customers lookup failed" in record.getMessage() for record in caplog.records
    )
