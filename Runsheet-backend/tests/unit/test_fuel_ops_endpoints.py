"""
Unit tests for the Fuel Ops hardening REST endpoints.

Covers ``GET /api/fuel/products`` and ``GET /api/fuel/destinations`` on the
router defined in :mod:`Agents.support.fuel_ops_endpoints`:

* Product catalog is scoped by the tenant's Region (Req 6.1.3).
* Destinations endpoint unifies fuel_stations and customer_tanks and honors
  destination_type, fuel_product (including legacy aliases), and zip_code
  filters (Req 6.2.4).
* Tenant scoping is enforced via the ``get_tenant_context`` dependency —
  the tenant_id on the returned documents must match the JWT's tenant_id.
* Filter validation for ``destination_type`` returns a structured HTTP 422
  when the value is not in the allowed literal set.

Requirements: 6.1.3, 6.2.4.
"""
from __future__ import annotations

from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    router,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(region: str = "US", tenant_id: str = "tenant-1"):
    """Return a factory that produces a TenantContext for dependency override."""

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


def _mock_es_with_responses(station_docs: List[dict], tank_docs: List[dict]):
    """Build a MagicMock ES service whose ``search_documents`` returns the
    provided fuel_stations and customer_tanks payloads, in that order.

    The DeliveryDestinationService queries fuel_stations first and
    customer_tanks second via ``asyncio.gather``, so ``side_effect`` ordering
    matters only relative to each ``await``. We set a dispatching side-effect
    on the mock so the call order does not break the test.
    """

    es = MagicMock()

    async def _search(index: str, query: dict, size: int) -> dict:
        if index == "fuel_stations":
            return {
                "hits": {
                    "hits": [{"_source": doc} for doc in station_docs],
                    "total": {"value": len(station_docs)},
                }
            }
        if index == "customer_tanks":
            return {
                "hits": {
                    "hits": [{"_source": doc} for doc in tank_docs],
                    "total": {"value": len(tank_docs)},
                }
            }
        return {"hits": {"hits": [], "total": {"value": 0}}}

    es.search_documents = AsyncMock(side_effect=_search)
    return es


def _build_app(
    *,
    region: str = "US",
    tenant_id: str = "tenant-1",
    station_docs: List[dict] | None = None,
    tank_docs: List[dict] | None = None,
):
    app = FastAPI()
    app.include_router(router)

    es = _mock_es_with_responses(station_docs or [], tank_docs or [])
    configure_fuel_ops_endpoints(es_service=es)

    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        region=region, tenant_id=tenant_id
    )
    return app, es


# ---------------------------------------------------------------------------
# GET /api/fuel/products
# ---------------------------------------------------------------------------


class TestFuelProductsEndpoint:
    def test_returns_us_catalog_for_us_tenant(self):
        app, _ = _build_app(region="US")
        client = TestClient(app)

        resp = client.get("/api/fuel/products")

        assert resp.status_code == 200
        data = resp.json()
        assert data["region"] == "US"
        assert data["total"] == len(data["items"])
        codes = {item["product_code"] for item in data["items"]}
        # US catalog contains all nine products from Req 6.1.1.
        assert {
            "DIESEL_2",
            "HEATING_OIL",
            "GASOLINE_REG",
            "GASOLINE_PREM",
            "PROPANE",
            "KEROSENE",
            "OFF_ROAD_DIESEL",
            "DEF",
            "ETHANOL_E85",
        } <= codes

    def test_returns_ng_subset_for_ng_tenant(self):
        app, _ = _build_app(region="NG")
        client = TestClient(app)

        resp = client.get("/api/fuel/products")

        assert resp.status_code == 200
        data = resp.json()
        assert data["region"] == "NG"
        codes = {item["product_code"] for item in data["items"]}
        # NG ships the four legacy-aliased products only.
        assert codes == {"DIESEL_2", "GASOLINE_REG", "PROPANE", "KEROSENE"}

    def test_ng_entries_preserve_legacy_aliases(self):
        app, _ = _build_app(region="NG")
        client = TestClient(app)

        resp = client.get("/api/fuel/products")
        data = resp.json()

        by_code = {item["product_code"]: item for item in data["items"]}
        assert "AGO" in by_code["DIESEL_2"]["aliases"]
        assert "PMS" in by_code["GASOLINE_REG"]["aliases"]
        assert "LPG" in by_code["PROPANE"]["aliases"]
        assert "ATK" in by_code["KEROSENE"]["aliases"]

    def test_product_fields_match_required_schema(self):
        app, _ = _build_app(region="US")
        client = TestClient(app)

        resp = client.get("/api/fuel/products")
        data = resp.json()

        item = data["items"][0]
        # Fields mandated by Requirement 6.1.3.
        for required_field in (
            "product_code",
            "display_name",
            "density_lbs_per_gallon",
            "tax_class",
            "aliases",
        ):
            assert required_field in item
        assert isinstance(item["density_lbs_per_gallon"], (int, float))
        assert item["density_lbs_per_gallon"] > 0

    def test_unknown_region_returns_empty_catalog(self):
        app, _ = _build_app(region="ZZ")
        client = TestClient(app)

        resp = client.get("/api/fuel/products")
        assert resp.status_code == 200
        data = resp.json()
        assert data["region"] == "ZZ"
        assert data["items"] == []
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# GET /api/fuel/destinations
# ---------------------------------------------------------------------------


def _station_doc(
    *,
    station_id: str,
    tenant_id: str = "tenant-1",
    fuel_grade: str = "AGO",
    capacity_liters: float = 30000.0,
    zip_code: str | None = None,
) -> dict:
    doc = {
        "station_id": station_id,
        "tenant_id": tenant_id,
        "name": f"Station {station_id}",
        "fuel_grade": fuel_grade,
        "capacity_liters": capacity_liters,
        "current_stock_liters": capacity_liters / 2.0,
        "latitude": 34.05,
        "longitude": -118.24,
        "status": "active",
    }
    if zip_code is not None:
        doc["zip_code"] = zip_code
    return doc


def _tank_doc(
    *,
    customer_tank_id: str,
    tenant_id: str = "tenant-1",
    fuel_product_code: str = "PROPANE",
    zip_code: str = "94103",
    capacity_gallons: float = 500.0,
) -> dict:
    return {
        "customer_tank_id": customer_tank_id,
        "tenant_id": tenant_id,
        "customer_id": f"cust-{customer_tank_id}",
        "name": f"Tank {customer_tank_id}",
        "fuel_product_code": fuel_product_code,
        "capacity_gallons": capacity_gallons,
        "current_level_gallons": capacity_gallons / 2.0,
        "location_lat": 37.77,
        "location_lon": -122.41,
        "zip_code": zip_code,
        "status": "active",
    }


class TestDeliveryDestinationsEndpoint:
    def test_returns_merged_stations_and_tanks(self):
        app, _ = _build_app(
            region="US",
            station_docs=[_station_doc(station_id="s-1")],
            tank_docs=[_tank_doc(customer_tank_id="t-1")],
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/destinations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        types = {item["destination_type"] for item in data["items"]}
        assert types == {"retail_station", "customer_tank"}

    def test_destination_type_filter_restricts_to_customer_tanks(self):
        app, es = _build_app(
            region="US",
            station_docs=[_station_doc(station_id="s-1")],
            tank_docs=[_tank_doc(customer_tank_id="t-1")],
        )
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/destinations",
            params={"destination_type": "customer_tank"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["destination_type"] == "customer_tank"
        # Only the customer_tanks index should have been queried.
        queried_indices = [call.args[0] for call in es.search_documents.await_args_list]
        assert "customer_tanks" in queried_indices
        assert "fuel_stations" not in queried_indices

    def test_fuel_product_filter_resolves_legacy_alias(self):
        """Alias ``AGO`` on the request resolves to ``DIESEL_2`` for the
        fuel_stations record whose grade was stored as the alias."""

        app, _ = _build_app(
            region="US",
            station_docs=[_station_doc(station_id="s-1", fuel_grade="AGO")],
            tank_docs=[_tank_doc(customer_tank_id="t-1", fuel_product_code="PROPANE")],
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/destinations", params={"fuel_product": "AGO"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["destination_id"] == "s-1"
        assert "DIESEL_2" in data["items"][0]["fuel_products"]

    def test_fuel_product_filter_with_canonical_code(self):
        app, _ = _build_app(
            region="US",
            station_docs=[],
            tank_docs=[
                _tank_doc(customer_tank_id="t-1", fuel_product_code="PROPANE"),
                _tank_doc(customer_tank_id="t-2", fuel_product_code="DIESEL_2"),
            ],
        )
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/destinations",
            params={"fuel_product": "PROPANE"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["destination_id"] == "t-1"

    def test_unknown_fuel_product_yields_empty_result(self):
        app, _ = _build_app(
            region="US",
            station_docs=[_station_doc(station_id="s-1")],
            tank_docs=[_tank_doc(customer_tank_id="t-1")],
        )
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/destinations",
            params={"fuel_product": "NOT_A_REAL_PRODUCT"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_zip_code_filter_scopes_customer_tanks_query(self):
        app, es = _build_app(
            region="US",
            station_docs=[],
            tank_docs=[_tank_doc(customer_tank_id="t-1", zip_code="94103")],
        )
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/destinations",
            params={"destination_type": "customer_tank", "zip_code": "94103"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        # The query should include a zip_code term clause on customer_tanks.
        calls = [
            call for call in es.search_documents.await_args_list
            if call.args[0] == "customer_tanks"
        ]
        assert calls, "customer_tanks must be queried"
        query = calls[0].args[1]
        must = query["query"]["bool"]["must"]
        assert any(
            clause.get("term", {}).get("zip_code") == "94103" for clause in must
        ), f"zip_code filter missing from query: {must}"

    def test_cross_tenant_documents_are_dropped(self):
        """If the ES index returns a doc with a different tenant_id (data
        mis-labelling), the service must drop it so the API never leaks."""

        app, _ = _build_app(
            region="US",
            tenant_id="tenant-1",
            station_docs=[_station_doc(station_id="s-1", tenant_id="tenant-1")],
            tank_docs=[_tank_doc(customer_tank_id="t-99", tenant_id="other-tenant")],
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/destinations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["tenant_id"] == "tenant-1"

    def test_tenant_id_is_injected_into_es_query(self):
        """Tenant scoping MUST come from the JWT; every ES query must filter
        by the verified tenant_id."""

        app, es = _build_app(
            region="US",
            tenant_id="tenant-abc",
            station_docs=[_station_doc(station_id="s-1", tenant_id="tenant-abc")],
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/destinations")
        assert resp.status_code == 200
        for call in es.search_documents.await_args_list:
            query = call.args[1]
            must = query["query"]["bool"]["must"]
            assert any(
                clause.get("term", {}).get("tenant_id") == "tenant-abc"
                for clause in must
            ), f"tenant_id scoping missing from query: {must}"

    def test_invalid_destination_type_returns_422(self):
        app, _ = _build_app(region="US")
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/destinations",
            params={"destination_type": "not_a_type"},
        )
        # FastAPI's Query literal validation raises 422 before our handler.
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Configuration / wiring
# ---------------------------------------------------------------------------


class TestConfigureFuelOpsEndpoints:
    def test_raises_when_not_configured(self):
        # Reset to unconfigured state.
        from fuel.api import fuel_ops_endpoints as mod

        prior = mod._destination_service
        mod._destination_service = None
        try:
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory()
            client = TestClient(app)

            # FastAPI will surface the RuntimeError as a 500.
            with pytest.raises(RuntimeError):
                client.get("/api/fuel/destinations")
        finally:
            mod._destination_service = prior

    def test_injected_service_is_used(self):
        """Callers may supply a pre-built DeliveryDestinationService for
        easier mocking."""

        from fuel.api.fuel_ops_endpoints import configure_fuel_ops_endpoints

        app = FastAPI()
        app.include_router(router)

        fake_service = MagicMock()
        fake_service.list = AsyncMock(return_value=[])

        configure_fuel_ops_endpoints(
            es_service=MagicMock(),
            destination_service=fake_service,
        )
        app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory()

        client = TestClient(app)
        resp = client.get("/api/fuel/destinations")
        assert resp.status_code == 200
        fake_service.list.assert_awaited_once()


# ---------------------------------------------------------------------------
# Response model sanity
# ---------------------------------------------------------------------------


class TestResponseModels:
    def test_fuel_products_response_shape(self):
        app, _ = _build_app(region="US")
        client = TestClient(app)

        data = client.get("/api/fuel/products").json()
        assert set(data.keys()) == {"region", "items", "total"}

    def test_destinations_response_shape(self):
        app, _ = _build_app(region="US")
        client = TestClient(app)

        data = client.get("/api/fuel/destinations").json()
        assert set(data.keys()) == {"items", "total"}
