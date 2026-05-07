"""
Unit tests for DeliveryDestinationService — unified view over fuel_stations
and customer_tanks.

Covers Capability 6 (Requirements 6.2.1 and 6.2.4):

* :class:`DeliveryDestination` field shape and validation.
* ``list`` merges parallel queries over both indices, filtered by tenant.
* Filters by ``destination_type``, ``fuel_product`` (with legacy-alias
  resolution), and ``zip_code``.
* Unit normalization: legacy fuel_stations liters are converted to gallons.
* Tenant-isolation guard drops documents with mismatched ``tenant_id``.
* ``get`` resolves by ``destination_type`` and returns ``None`` for missing
  or cross-tenant documents.

The ElasticsearchService dependency is replaced with a recording mock so
tests never touch a real cluster.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from fuel.services.delivery_destination_service import (
    CUSTOMER_TANKS_INDEX,
    FUEL_STATIONS_INDEX,
    DeliveryDestination,
    DeliveryDestinationFilters,
    DeliveryDestinationService,
    Location,
)
from services.unit_conversion import GAL_TO_L


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeESService:
    """Recording async mock for ElasticsearchService.

    Stores the documents each index holds as raw ``_source`` payloads and
    returns results in the canonical ES response shape when
    ``search_documents`` is invoked.
    """

    def __init__(self) -> None:
        self.fuel_stations: List[Dict[str, Any]] = []
        self.customer_tanks: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int = 100
    ) -> Dict[str, Any]:
        self.calls.append({"index": index, "query": query, "size": size})
        if index == FUEL_STATIONS_INDEX:
            docs = self.fuel_stations
        elif index == CUSTOMER_TANKS_INDEX:
            docs = self.customer_tanks
        else:  # pragma: no cover - defensive
            docs = []

        matched = [doc for doc in docs if _matches_query(doc, query)]
        return {"hits": {"hits": [{"_source": dict(doc)} for doc in matched]}}


def _matches_query(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
    """Minimal ES boolean-filter matcher supporting ``term`` clauses."""

    must = (
        query.get("query", {})
        .get("bool", {})
        .get("must", [])
    )
    for clause in must:
        if "term" in clause:
            for field, expected in clause["term"].items():
                if doc.get(field) != expected:
                    return False
    return True


def _fuel_station_doc(
    *,
    station_id: str,
    tenant_id: str,
    name: str = "Test Station",
    latitude: float = 40.0,
    longitude: float = -74.0,
    capacity_liters: float = 37_854.11784,  # 10,000 gallons
    current_stock_liters: float = 18_927.05892,  # 5,000 gallons
    fuel_type: str = "AGO",
    status: str = "open",
    location_name: str = "123 Main St",
    updated_at: str = "2024-05-01T10:00:00Z",
    zip_code: str | None = None,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "station_id": station_id,
        "tenant_id": tenant_id,
        "name": name,
        "latitude": latitude,
        "longitude": longitude,
        "capacity_liters": capacity_liters,
        "current_stock_liters": current_stock_liters,
        "fuel_type": fuel_type,
        "status": status,
        "location_name": location_name,
        "updated_at": updated_at,
    }
    if zip_code is not None:
        doc["zip_code"] = zip_code
    return doc


def _customer_tank_doc(
    *,
    customer_tank_id: str,
    tenant_id: str,
    customer_id: str = "CUST-1",
    fuel_product_code: str = "PROPANE",
    capacity_gallons: float = 500.0,
    current_level_gallons: float = 250.0,
    location_lat: float = 41.5,
    location_lon: float = -72.5,
    zip_code: str = "06001",
    status: str = "active",
    name: str | None = None,
    address: str = "1 Hillside Lane",
    updated_at: str = "2024-05-05T08:30:00Z",
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "customer_tank_id": customer_tank_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "fuel_product_code": fuel_product_code,
        "capacity_gallons": capacity_gallons,
        "current_level_gallons": current_level_gallons,
        "location_lat": location_lat,
        "location_lon": location_lon,
        "zip_code": zip_code,
        "status": status,
        "address": address,
        "updated_at": updated_at,
    }
    if name:
        doc["name"] = name
    return doc


@pytest.fixture
def es() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def service(es: _FakeESService) -> DeliveryDestinationService:
    return DeliveryDestinationService(es_service=es)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestModels:
    def test_location_rejects_out_of_range_values(self):
        with pytest.raises(Exception):
            Location(lat=100.0, lon=0.0)
        with pytest.raises(Exception):
            Location(lat=0.0, lon=-200.0)

    def test_delivery_destination_requires_core_fields(self):
        dest = DeliveryDestination(
            destination_id="D1",
            destination_type="retail_station",
            tenant_id="T1",
            name="Station 1",
        )
        assert dest.fuel_products == []
        assert dest.customer_id is None
        assert dest.raw is None

    def test_delivery_destination_rejects_unknown_type(self):
        with pytest.raises(Exception):
            DeliveryDestination(
                destination_id="D1",
                destination_type="warehouse",  # type: ignore[arg-type]
                tenant_id="T1",
                name="X",
            )


# ---------------------------------------------------------------------------
# list()
# ---------------------------------------------------------------------------


class TestList:
    @pytest.mark.asyncio
    async def test_list_merges_both_indices(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """Req 6.2.1 — unified view includes retail_station and customer_tank."""
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T1"))
        es.customer_tanks.append(
            _customer_tank_doc(customer_tank_id="CT1", tenant_id="T1")
        )

        destinations = await service.list("T1")

        assert len(destinations) == 2
        types = {d.destination_type for d in destinations}
        assert types == {"retail_station", "customer_tank"}
        # Retail station should appear before customer tank (stable order).
        assert destinations[0].destination_type == "retail_station"
        assert destinations[1].destination_type == "customer_tank"

    @pytest.mark.asyncio
    async def test_list_normalizes_retail_station_liters_to_gallons(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """Legacy fuel_stations liters are converted to US gallons on read."""
        es.fuel_stations.append(
            _fuel_station_doc(
                station_id="S1",
                tenant_id="T1",
                capacity_liters=GAL_TO_L * 1000.0,  # exactly 1,000 gallons
                current_stock_liters=GAL_TO_L * 250.0,  # exactly 250 gallons
            )
        )

        destinations = await service.list("T1")

        [station] = destinations
        assert station.capacity_gallons is not None
        assert math.isclose(station.capacity_gallons, 1000.0, rel_tol=1e-9)
        assert station.current_level_gallons is not None
        assert math.isclose(station.current_level_gallons, 250.0, rel_tol=1e-9)

    @pytest.mark.asyncio
    async def test_list_canonicalizes_legacy_nigerian_aliases(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """AGO → DIESEL_2, LPG → PROPANE via the fuel product catalog."""
        es.fuel_stations.append(
            _fuel_station_doc(station_id="S1", tenant_id="T1", fuel_type="AGO")
        )
        es.fuel_stations.append(
            _fuel_station_doc(
                station_id="S2", tenant_id="T1", fuel_type="LPG", name="LPG Station"
            )
        )

        destinations = await service.list("T1")

        products_by_id = {d.destination_id: d.fuel_products for d in destinations}
        assert products_by_id["S1"] == ["DIESEL_2"]
        assert products_by_id["S2"] == ["PROPANE"]

    @pytest.mark.asyncio
    async def test_list_handles_comma_separated_fuel_types(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """fuel_types="diesel_2,gasoline_reg" splits into two canonical codes."""
        doc = _fuel_station_doc(station_id="S1", tenant_id="T1")
        doc.pop("fuel_type")
        doc["fuel_types"] = "DIESEL_2, GASOLINE_REG"
        es.fuel_stations.append(doc)

        destinations = await service.list("T1")

        [station] = destinations
        assert station.fuel_products == ["DIESEL_2", "GASOLINE_REG"]

    @pytest.mark.asyncio
    async def test_list_scopes_by_tenant(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """Documents belonging to other tenants are excluded."""
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T1"))
        es.fuel_stations.append(_fuel_station_doc(station_id="S2", tenant_id="T2"))
        es.customer_tanks.append(
            _customer_tank_doc(customer_tank_id="CT1", tenant_id="T2")
        )

        destinations = await service.list("T1")

        assert len(destinations) == 1
        assert destinations[0].destination_id == "S1"
        assert destinations[0].tenant_id == "T1"

    @pytest.mark.asyncio
    async def test_list_filters_by_destination_type_retail_only(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """destination_type='retail_station' skips the customer_tanks query."""
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T1"))
        es.customer_tanks.append(
            _customer_tank_doc(customer_tank_id="CT1", tenant_id="T1")
        )

        destinations = await service.list(
            "T1", filters={"destination_type": "retail_station"}
        )

        assert len(destinations) == 1
        assert destinations[0].destination_type == "retail_station"
        # Only the fuel_stations index should have been queried.
        assert all(call["index"] == FUEL_STATIONS_INDEX for call in es.calls)

    @pytest.mark.asyncio
    async def test_list_filters_by_destination_type_customer_tank_only(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T1"))
        es.customer_tanks.append(
            _customer_tank_doc(customer_tank_id="CT1", tenant_id="T1")
        )

        destinations = await service.list(
            "T1",
            filters=DeliveryDestinationFilters(destination_type="customer_tank"),
        )

        assert len(destinations) == 1
        assert destinations[0].destination_type == "customer_tank"
        assert all(call["index"] == CUSTOMER_TANKS_INDEX for call in es.calls)

    @pytest.mark.asyncio
    async def test_list_filters_by_fuel_product_alias(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """Filter 'AGO' matches destinations carrying canonical DIESEL_2."""
        es.fuel_stations.append(
            _fuel_station_doc(station_id="S1", tenant_id="T1", fuel_type="AGO")
        )
        es.fuel_stations.append(
            _fuel_station_doc(
                station_id="S2", tenant_id="T1", fuel_type="PMS"
            )
        )
        es.customer_tanks.append(
            _customer_tank_doc(
                customer_tank_id="CT1",
                tenant_id="T1",
                fuel_product_code="DIESEL_2",
            )
        )

        destinations = await service.list("T1", filters={"fuel_product": "AGO"})

        ids = sorted(d.destination_id for d in destinations)
        assert ids == ["CT1", "S1"]

    @pytest.mark.asyncio
    async def test_list_unknown_fuel_product_returns_empty(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T1"))
        destinations = await service.list(
            "T1", filters={"fuel_product": "UNOBTANIUM"}
        )
        assert destinations == []

    @pytest.mark.asyncio
    async def test_list_filters_by_zip_code_on_customer_tanks(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """zip_code filter narrows customer_tanks at ES layer."""
        es.customer_tanks.append(
            _customer_tank_doc(
                customer_tank_id="CT1", tenant_id="T1", zip_code="06001"
            )
        )
        es.customer_tanks.append(
            _customer_tank_doc(
                customer_tank_id="CT2", tenant_id="T1", zip_code="06002"
            )
        )

        destinations = await service.list("T1", filters={"zip_code": "06001"})

        ids = [d.destination_id for d in destinations if d.destination_type == "customer_tank"]
        assert ids == ["CT1"]

    @pytest.mark.asyncio
    async def test_list_runs_queries_in_parallel(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """Both indices should be queried when no destination_type is set."""
        await service.list("T1")
        indices = {call["index"] for call in es.calls}
        assert indices == {FUEL_STATIONS_INDEX, CUSTOMER_TANKS_INDEX}

    @pytest.mark.asyncio
    async def test_list_empty_tenant_id_raises(
        self, service: DeliveryDestinationService
    ):
        with pytest.raises(ValueError):
            await service.list("")
        with pytest.raises(ValueError):
            await service.list("   ")

    @pytest.mark.asyncio
    async def test_list_include_raw_preserves_source_payload(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        station = _fuel_station_doc(station_id="S1", tenant_id="T1")
        es.fuel_stations.append(station)

        destinations = await service.list("T1", include_raw=True)

        [record] = destinations
        assert record.raw is not None
        assert record.raw["station_id"] == "S1"

    @pytest.mark.asyncio
    async def test_list_omits_raw_by_default(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T1"))
        destinations = await service.list("T1")
        assert destinations[0].raw is None

    @pytest.mark.asyncio
    async def test_list_drops_mismatched_tenant_documents(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """Defensive: even if ES returns a wrong-tenant doc, it is dropped."""

        class _LeakyES:
            calls: List[Dict[str, Any]] = []

            async def search_documents(
                self, index: str, query: Dict[str, Any], size: int = 100
            ) -> Dict[str, Any]:
                self.calls.append({"index": index})
                if index == FUEL_STATIONS_INDEX:
                    return {
                        "hits": {
                            "hits": [
                                {
                                    "_source": _fuel_station_doc(
                                        station_id="S1", tenant_id="T2"
                                    )
                                }
                            ]
                        }
                    }
                return {"hits": {"hits": []}}

        leaky = _LeakyES()
        leaky_service = DeliveryDestinationService(es_service=leaky)

        destinations = await leaky_service.list("T1")

        assert destinations == []

    @pytest.mark.asyncio
    async def test_list_parses_string_location_and_datetime(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """The seeded fuel_stations data uses "lat,lon" combined strings."""
        doc = _fuel_station_doc(station_id="S1", tenant_id="T1")
        doc.pop("latitude")
        doc.pop("longitude")
        doc["location"] = "40.7128,-74.0060"
        es.fuel_stations.append(doc)

        destinations = await service.list("T1")

        [station] = destinations
        assert station.location is not None
        assert math.isclose(station.location.lat, 40.7128, rel_tol=1e-9)
        assert math.isclose(station.location.lon, -74.0060, rel_tol=1e-9)
        assert station.updated_at is not None
        assert station.updated_at.replace(tzinfo=None) == datetime(
            2024, 5, 1, 10, 0, 0
        )

    @pytest.mark.asyncio
    async def test_list_skips_documents_missing_identifiers(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """A document without station_id / customer_tank_id is silently dropped."""
        es.fuel_stations.append({"tenant_id": "T1", "name": "No ID"})
        es.customer_tanks.append({"tenant_id": "T1", "fuel_product_code": "PROPANE"})

        destinations = await service.list("T1")

        assert destinations == []


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_get_returns_retail_station(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T1"))

        record = await service.get("T1", "retail_station", "S1")

        assert record is not None
        assert record.destination_id == "S1"
        assert record.destination_type == "retail_station"

    @pytest.mark.asyncio
    async def test_get_returns_customer_tank(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        es.customer_tanks.append(
            _customer_tank_doc(customer_tank_id="CT1", tenant_id="T1")
        )

        record = await service.get("T1", "customer_tank", "CT1")

        assert record is not None
        assert record.destination_id == "CT1"
        assert record.destination_type == "customer_tank"
        assert record.customer_id == "CUST-1"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        record = await service.get("T1", "customer_tank", "does-not-exist")
        assert record is None

    @pytest.mark.asyncio
    async def test_get_cross_tenant_returns_none(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """Req 6.2.1 + 10.1 — cross-tenant access must not leak data."""
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T2"))

        record = await service.get("T1", "retail_station", "S1")

        assert record is None

    @pytest.mark.asyncio
    async def test_get_rejects_invalid_destination_type(
        self, service: DeliveryDestinationService
    ):
        with pytest.raises(ValueError):
            await service.get("T1", "warehouse", "W1")

    @pytest.mark.asyncio
    async def test_get_empty_inputs_raise(
        self, service: DeliveryDestinationService
    ):
        with pytest.raises(ValueError):
            await service.get("", "retail_station", "S1")
        with pytest.raises(ValueError):
            await service.get("T1", "retail_station", "")

    @pytest.mark.asyncio
    async def test_get_destination_type_case_insensitive(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        es.customer_tanks.append(
            _customer_tank_doc(customer_tank_id="CT1", tenant_id="T1")
        )

        record = await service.get("T1", "Customer_Tank", "CT1")
        assert record is not None


# ---------------------------------------------------------------------------
# Filter normalization
# ---------------------------------------------------------------------------


class TestFilterNormalization:
    @pytest.mark.asyncio
    async def test_list_accepts_empty_string_filter_values(
        self, service: DeliveryDestinationService, es: _FakeESService
    ):
        """Empty-string values from query strings should be treated as absent."""
        es.fuel_stations.append(_fuel_station_doc(station_id="S1", tenant_id="T1"))

        destinations = await service.list(
            "T1",
            filters={"destination_type": None, "zip_code": "", "fuel_product": ""},
        )

        assert len(destinations) == 1

    @pytest.mark.asyncio
    async def test_list_rejects_bad_filter_type(
        self, service: DeliveryDestinationService
    ):
        with pytest.raises(TypeError):
            await service.list("T1", filters="not-a-dict")  # type: ignore[arg-type]
