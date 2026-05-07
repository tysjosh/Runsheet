"""
Unit tests for ``GET /api/fuel/rack-prices`` (Task 7.5, Req 8.2.6).

The endpoint lives on :data:`fuel.api.fuel_ops_endpoints.router` and
returns the latest persisted ``rack_prices`` rows for the requesting
tenant, filtered by ``terminal_id``, ``product_code``, and
``branded_flag``. Tests use an in-memory :class:`_FakeESService` stub
that honours the ``bool.must`` + ``term`` ES query shape the endpoint
issues so we exercise the real filter plumbing without a real
Elasticsearch cluster.

Coverage:
    * Tenant isolation on the read path (cross-tenant rows never leak).
    * ``terminal_id`` / ``product_code`` / ``branded_flag`` filters.
    * Legacy-alias canonicalization on ``product_code`` (e.g. ``AGO``
      → ``DIESEL_2``) via the fuel product catalog.
    * Unknown ``product_code`` short-circuits to an empty list.
    * Ordering: ``effective_at`` descending so the freshest row wins.
    * Pagination shape (``items`` / ``total`` / ``page`` / ``page_size``
      / ``has_next``).
    * Corrupt rows are dropped with a warning (not a 500).

Validates: Requirement 8.2.6.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub for the ``rack_prices`` index.

    Supports the ``bool.must`` + ``term`` shape the endpoint issues plus
    sort on ``effective_at`` / ``retrieved_at`` (both descending).
    ``from`` + ``size`` pagination and a ``total.value`` count are
    honoured so the endpoint's ``has_next`` computation is exercised
    end-to-end.
    """

    def __init__(self) -> None:
        # Keyed by ``(index, rack_price_id)`` so the stub can host
        # multiple indices during a single test without collisions.
        self.docs: Dict[tuple, Dict[str, Any]] = {}

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.docs[(index, doc_id)] = dict(document)

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        must = query.get("query", {}).get("bool", {}).get("must", [])
        equality: Dict[str, Any] = {}
        for clause in must:
            term = clause.get("term") if isinstance(clause, dict) else None
            if not term:
                continue
            for field, value in term.items():
                equality[field] = value

        matches: List[Dict[str, Any]] = []
        for (doc_index, _), doc in self.docs.items():
            if doc_index != index:
                continue
            if any(doc.get(k) != v for k, v in equality.items()):
                continue
            matches.append(dict(doc))

        sort = query.get("sort") or []

        def _sort_key(row: Dict[str, Any]) -> tuple:
            key = []
            for entry in sort:
                if not isinstance(entry, dict):
                    continue
                for field, spec in entry.items():
                    order = (
                        spec.get("order", "asc")
                        if isinstance(spec, dict)
                        else "asc"
                    )
                    value = row.get(field) or ""
                    # Python sorts descending by negating string compare
                    # through ``reverse=True`` on the outer sort call.
                    key.append((field, order, value))
            return tuple(v for _, _, v in key)

        # Descending sort when any sort entry specifies ``desc``. The
        # endpoint only ever asks for descending on both fields, so this
        # simple branch suffices.
        reverse = any(
            isinstance(entry, dict)
            and any(
                isinstance(spec, dict) and spec.get("order") == "desc"
                for spec in entry.values()
            )
            for entry in sort
        )
        matches.sort(key=_sort_key, reverse=reverse)

        total_value = len(matches)

        start = int(query.get("from", 0) or 0)
        effective_size = int(query.get("size", size) or size)
        window = matches[start : start + effective_size]

        return {
            "hits": {
                "hits": [{"_source": dict(row)} for row in window],
                "total": {"value": total_value},
            }
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-1", region: str = "US"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["dispatcher"],
            region=region,
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(tenant_id: str = "tenant-1") -> tuple[FastAPI, _FakeESService]:
    es = _FakeESService()
    configure_fuel_ops_endpoints(es_service=es)

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es


def _seed_rack_price(
    es: _FakeESService,
    *,
    rack_price_id: str,
    tenant_id: str,
    terminal_id: str,
    product_code: str,
    price_per_gallon_usd: float,
    branded_flag: bool = False,
    supplier_brand: str | None = None,
    provider: str = "opis",
    effective_at: datetime | None = None,
    retrieved_at: datetime | None = None,
) -> None:
    """Insert a well-formed RackPrice source into the fake index."""

    if effective_at is None:
        effective_at = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    if retrieved_at is None:
        retrieved_at = effective_at
    es.docs[("rack_prices", rack_price_id)] = {
        "rack_price_id": rack_price_id,
        "tenant_id": tenant_id,
        "terminal_id": terminal_id,
        "product_code": product_code,
        "price_per_gallon_usd": price_per_gallon_usd,
        "branded_flag": branded_flag,
        "supplier_brand": supplier_brand,
        "provider": provider,
        "effective_at": effective_at.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListRackPricesTenantIsolation:
    def test_returns_only_calling_tenants_rows(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_rack_price(
            es,
            rack_price_id="rp-A1",
            tenant_id="tenant-A",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-B1",
            tenant_id="tenant-B",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.40,
        )

        with TestClient(app) as client:
            resp = client.get("/api/fuel/rack-prices")

        assert resp.status_code == 200
        body = resp.json()
        ids = [row["rack_price_id"] for row in body["items"]]
        assert ids == ["rp-A1"]
        assert body["total"] == 1

    def test_tenant_mismatched_row_is_dropped_defensively(self):
        """A row whose tenant_id does not match must never leak.

        Even if the ES term filter were bypassed, the endpoint
        re-validates every row's tenant_id before surfacing it.
        """

        app, es = _build_app(tenant_id="tenant-A")

        # Seed two rows with the same doc id pattern. The stub's
        # search filters on the ``term`` clause the endpoint sends, so
        # this already excludes tenant-B. The extra row exists so any
        # regression in the ES filter surfaces as an extra element.
        _seed_rack_price(
            es,
            rack_price_id="rp-A1",
            tenant_id="tenant-A",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-B1",
            tenant_id="tenant-B",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.40,
        )

        with TestClient(app) as client:
            resp = client.get("/api/fuel/rack-prices")

        ids = {row["rack_price_id"] for row in resp.json()["items"]}
        assert "rp-B1" not in ids


class TestListRackPricesFilters:
    def test_terminal_id_filter(self):
        app, es = _build_app()
        _seed_rack_price(
            es,
            rack_price_id="rp-1",
            tenant_id="tenant-1",
            terminal_id="t-alpha",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-2",
            tenant_id="tenant-1",
            terminal_id="t-beta",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.40,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices", params={"terminal_id": "t-alpha"}
            )

        assert resp.status_code == 200
        ids = [row["rack_price_id"] for row in resp.json()["items"]]
        assert ids == ["rp-1"]

    def test_product_code_filter_canonical(self):
        app, es = _build_app()
        _seed_rack_price(
            es,
            rack_price_id="rp-1",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-2",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="GASOLINE_REG",
            price_per_gallon_usd=2.99,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices",
                params={"product_code": "DIESEL_2"},
            )

        assert resp.status_code == 200
        ids = [row["rack_price_id"] for row in resp.json()["items"]]
        assert ids == ["rp-1"]

    def test_product_code_filter_resolves_legacy_alias(self):
        """``AGO`` must canonicalize to ``DIESEL_2`` before the ES query."""

        app, es = _build_app()
        _seed_rack_price(
            es,
            rack_price_id="rp-1",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices", params={"product_code": "AGO"}
            )

        assert resp.status_code == 200
        ids = [row["rack_price_id"] for row in resp.json()["items"]]
        assert ids == ["rp-1"]

    def test_unknown_product_code_returns_empty_list(self):
        app, es = _build_app()
        _seed_rack_price(
            es,
            rack_price_id="rp-1",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices",
                params={"product_code": "UNOBTAINIUM"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["has_next"] is False

    def test_branded_flag_filter_true(self):
        app, es = _build_app()
        _seed_rack_price(
            es,
            rack_price_id="rp-branded",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.55,
            branded_flag=True,
            supplier_brand="Shell",
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-unbranded",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
            branded_flag=False,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices", params={"branded_flag": "true"}
            )

        assert resp.status_code == 200
        ids = [row["rack_price_id"] for row in resp.json()["items"]]
        assert ids == ["rp-branded"]

    def test_branded_flag_filter_false(self):
        app, es = _build_app()
        _seed_rack_price(
            es,
            rack_price_id="rp-branded",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.55,
            branded_flag=True,
            supplier_brand="Shell",
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-unbranded",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
            branded_flag=False,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices", params={"branded_flag": "false"}
            )

        assert resp.status_code == 200
        ids = [row["rack_price_id"] for row in resp.json()["items"]]
        assert ids == ["rp-unbranded"]

    def test_combined_filters_are_anded(self):
        app, es = _build_app()
        _seed_rack_price(
            es,
            rack_price_id="rp-hit",
            tenant_id="tenant-1",
            terminal_id="t-alpha",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.55,
            branded_flag=True,
            supplier_brand="Shell",
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-wrong-terminal",
            tenant_id="tenant-1",
            terminal_id="t-beta",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.55,
            branded_flag=True,
            supplier_brand="Shell",
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-wrong-branded",
            tenant_id="tenant-1",
            terminal_id="t-alpha",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
            branded_flag=False,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices",
                params={
                    "terminal_id": "t-alpha",
                    "product_code": "DIESEL_2",
                    "branded_flag": "true",
                },
            )

        assert resp.status_code == 200
        ids = [row["rack_price_id"] for row in resp.json()["items"]]
        assert ids == ["rp-hit"]


class TestListRackPricesOrderingAndPagination:
    def test_orders_by_effective_at_descending(self):
        app, es = _build_app()
        now = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)
        _seed_rack_price(
            es,
            rack_price_id="rp-old",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.10,
            effective_at=now - timedelta(hours=2),
        )
        _seed_rack_price(
            es,
            rack_price_id="rp-new",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.20,
            effective_at=now,
        )

        with TestClient(app) as client:
            resp = client.get("/api/fuel/rack-prices")

        ids = [row["rack_price_id"] for row in resp.json()["items"]]
        assert ids == ["rp-new", "rp-old"]

    def test_pagination_reports_has_next(self):
        app, es = _build_app()
        base = datetime(2025, 4, 1, 12, 0, tzinfo=timezone.utc)
        for i in range(3):
            _seed_rack_price(
                es,
                rack_price_id=f"rp-{i}",
                tenant_id="tenant-1",
                terminal_id="t-1",
                product_code="DIESEL_2",
                price_per_gallon_usd=3.00 + i * 0.05,
                effective_at=base - timedelta(minutes=i),
            )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices", params={"page": 1, "size": 2}
            )

        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 2
        assert body["total"] == 3
        assert body["has_next"] is True
        assert [row["rack_price_id"] for row in body["items"]] == [
            "rp-0",
            "rp-1",
        ]

    def test_pagination_has_next_false_on_last_page(self):
        app, es = _build_app()
        for i in range(2):
            _seed_rack_price(
                es,
                rack_price_id=f"rp-{i}",
                tenant_id="tenant-1",
                terminal_id="t-1",
                product_code="DIESEL_2",
                price_per_gallon_usd=3.00,
            )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices", params={"page": 1, "size": 5}
            )

        body = resp.json()
        assert body["total"] == 2
        assert body["has_next"] is False


class TestListRackPricesRobustness:
    def test_response_shape_matches_envelope(self):
        app, _ = _build_app()

        with TestClient(app) as client:
            resp = client.get("/api/fuel/rack-prices")

        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "items",
            "total",
            "page",
            "page_size",
            "has_next",
        }

    def test_endpoint_rejects_unauthenticated_size_out_of_bounds(self):
        """``size`` is bounded at 500 by the Query validator."""

        app, _ = _build_app()

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/rack-prices", params={"size": 10000}
            )

        assert resp.status_code == 422

    def test_corrupt_row_is_dropped_not_fatal(self):
        """A row that fails :class:`RackPrice` validation is skipped."""

        app, es = _build_app()
        # Seed a good row and a malformed row (missing price).
        _seed_rack_price(
            es,
            rack_price_id="rp-good",
            tenant_id="tenant-1",
            terminal_id="t-1",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.25,
        )
        es.docs[("rack_prices", "rp-bad")] = {
            "rack_price_id": "rp-bad",
            "tenant_id": "tenant-1",
            "terminal_id": "t-1",
            "product_code": "DIESEL_2",
            # price_per_gallon_usd intentionally missing — must drop.
            "branded_flag": False,
            "provider": "opis",
            "effective_at": "2025-01-15T12:00:00+00:00",
            "retrieved_at": "2025-01-15T12:00:00+00:00",
        }

        with TestClient(app) as client:
            resp = client.get("/api/fuel/rack-prices")

        assert resp.status_code == 200
        ids = [row["rack_price_id"] for row in resp.json()["items"]]
        assert ids == ["rp-good"]
