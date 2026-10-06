"""Product list filters match any resolved code on the real store (F12/S6).

The depot and station list filters resolve a code, alias or category name to a
list of catalog codes and query them with ``terms``. On the Postgres store that
goes through the query translator, where ``terms`` against an array field
(``fuel_types_supported``) and a scalar field (station ``fuel_type``) must both
match any element. Real PostgreSQL required — see ``conftest.py``.
"""
from __future__ import annotations

from typing import Any, Dict

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


class _IndexFacade:
    """Route every index name to the per-test index so rows stay under the prefix."""

    def __init__(self, store, index_name: str) -> None:
        self._store = store
        self._index = index_name

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100, **kw):
        return await self._store.search_documents(self._index, query, size)


def _depot(depot_id: str, tenant_id: str, codes: list[str]) -> Dict[str, Any]:
    return {
        "depot_id": depot_id,
        "tenant_id": tenant_id,
        "name": f"Depot {depot_id}",
        "location_lat": 40.0,
        "location_lon": -74.0,
        "address": "1 QA Lane",
        "timezone": "UTC",
        "fuel_types_supported": codes,
        "status": "active",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:00:00+00:00",
    }


def _station(station_id: str, tenant_id: str, fuel_type: str) -> Dict[str, Any]:
    return {
        "station_id": station_id,
        "name": f"Station {station_id}",
        "fuel_type": fuel_type,
        "capacity_liters": 1000.0,
        "current_stock_liters": 500.0,
        "daily_consumption_rate": 0.0,
        "days_until_empty": 99999.0,
        "alert_threshold_pct": 20.0,
        "status": "normal",
        "location": None,
        "location_name": "QA",
        "tenant_id": tenant_id,
        "last_updated": "2026-10-01T00:00:00+00:00",
    }


async def test_depot_category_filter_matches_any_code(store, index_name):
    from fuel.depot_models import DepotRepository
    from fuel.services.fuel_product_catalog import resolve_product_filter

    for depot_id, tenant, codes in [
        ("QA-DEP-REG", TENANT_A, ["GASOLINE_REG", "DIESEL_2"]),
        ("QA-DEP-PREM", TENANT_A, ["GASOLINE_PREM"]),
        ("QA-DEP-DSL", TENANT_A, ["DIESEL_2"]),
        ("QA-DEP-B", TENANT_B, ["GASOLINE_REG"]),
    ]:
        await store.index_document(index_name, depot_id, _depot(depot_id, tenant, codes))

    repo = DepotRepository(store, index_name=index_name)
    found = await repo.list_for_tenant(
        TENANT_A, fuel_type=resolve_product_filter("gasoline")
    )
    assert sorted(d.depot_id for d in found) == ["QA-DEP-PREM", "QA-DEP-REG"]

    diesel = await repo.list_for_tenant(TENANT_A, fuel_type=resolve_product_filter("diesel"))
    assert sorted(d.depot_id for d in diesel) == ["QA-DEP-DSL", "QA-DEP-REG"]


async def test_station_code_filter_matches_canonical_and_legacy_rows(store, index_name):
    from fuel.services.fuel_product_catalog import resolve_product_filter
    from fuel.services.fuel_service import FuelService

    for station_id, tenant, fuel_type in [
        ("QA-STN-CANON", TENANT_A, "DIESEL_2"),
        ("QA-STN-LEGACY", TENANT_A, "AGO"),
        ("QA-STN-PMS", TENANT_A, "GASOLINE_REG"),
        ("QA-STN-B", TENANT_B, "DIESEL_2"),
    ]:
        await store.index_document(
            index_name, f"{station_id}::{fuel_type}", _station(station_id, tenant, fuel_type)
        )

    svc = FuelService(_IndexFacade(store, index_name))
    result = await svc.list_stations(TENANT_A, fuel_type=resolve_product_filter("diesel"))
    assert sorted(s.station_id for s in result.data) == ["QA-STN-CANON", "QA-STN-LEGACY"]
