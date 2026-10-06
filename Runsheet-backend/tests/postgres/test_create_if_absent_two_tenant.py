"""Creates are create-if-absent across tenants (findings B8, B9, F5, S7).

The store key is ``(index_name, doc_id)`` with no tenant. Before the fix, each
of these creates called ``index_document``, which is an upsert: tenant B posting
an id tenant A already owns replaced A's body and rewrote its ``tenant_id``, so
A's driver, depot or station silently became B's. These tests run the real
repositories and service over the real store: tenant A creates id X, tenant B's
create of X is refused with 409 ``RESOURCE_ALREADY_EXISTS``, and A's row is
byte-for-byte unchanged with no mirror write for B.

The asset endpoint has no repository; its ``create_document`` call goes through
the facade, which ``test_document_store_create.py`` covers, and its 409 mapping is
covered in ``tests/unit/test_create_asset_endpoint.py``.

Real PostgreSQL required — see ``conftest.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from errors.codes import ErrorCode
from errors.exceptions import AppException

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


class _IndexFacade:
    """Route every index name to the per-test index so rows stay under the prefix."""

    def __init__(self, store, index_name: str) -> None:
        self._store = store
        self._index = index_name

    async def index_document(self, index: str, doc_id: str, doc: Dict[str, Any], **kw):
        return await self._store.index_document(self._index, doc_id, doc)

    async def create_document(self, index: str, doc_id: str, doc: Dict[str, Any]):
        return await self._store.create_document(self._index, doc_id, doc)

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100, **kw):
        return await self._store.search_documents(self._index, query, size)

    async def get_document(self, index: str, doc_id: str):
        return await self._store.get_document(self._index, doc_id)

    async def update_document(self, index: str, doc_id: str, partial: Dict[str, Any]):
        return await self._store.update_document(self._index, doc_id, partial)


@pytest.fixture
def mirror_calls():
    """Record dual-writes instead of touching the relational tables."""
    calls: List[tuple] = []

    async def _record(aggregate, doc, *, doc_id=None):
        calls.append((aggregate, doc.get("tenant_id"), doc_id))

    with patch(
        "commerce.services.commerce_persistence_bridge.mirror_current_state_upsert",
        _record,
    ), patch("fuel.services.fuel_service.mirror_current_state_upsert", _record):
        yield calls


def _driver(driver_id: str, name: str) -> Dict[str, Any]:
    return {
        "driver_id": driver_id,
        "driver_name": name,
        "status": "active",
        "last_event_timestamp": "2026-10-01T10:00:00+00:00",
        "source_schema_version": "1.0",
        "trace_id": f"drv_{driver_id}",
    }


def _assert_already_exists(exc: AppException) -> None:
    assert exc.error_code is ErrorCode.RESOURCE_ALREADY_EXISTS
    assert exc.status_code == 409
    rendered = repr(exc.to_dict())
    assert TENANT_A not in rendered


async def test_driver_create_cannot_take_over_another_tenants_id(store, index_name):
    from fuel.driver_repository import DriverRepository

    repo = DriverRepository(store, drivers_index=index_name)
    await repo.create(TENANT_A, _driver("QA-DRV-X", "A's driver"))
    before = await store.get_document(index_name, "QA-DRV-X")

    with pytest.raises(AppException) as exc_info:
        await repo.create(TENANT_B, _driver("QA-DRV-X", "B's driver"))

    _assert_already_exists(exc_info.value)
    assert await store.get_document(index_name, "QA-DRV-X") == before
    assert before["tenant_id"] == TENANT_A
    assert await repo.get(TENANT_A, "QA-DRV-X") is not None
    assert await repo.get(TENANT_B, "QA-DRV-X") is None


async def test_depot_create_cannot_take_over_another_tenants_id(
    store, index_name, mirror_calls
):
    from fuel.depot_models import DepotRepository

    repo = DepotRepository(store, index_name=index_name)
    payload = {
        "depot_id": "QA-DEPOT-X",
        "name": "A's depot",
        "location_lat": 40.0,
        "location_lon": -74.0,
        "address": "1 QA Lane",
        "timezone": "UTC",
        "status": "active",
    }
    await repo.create(TENANT_A, dict(payload, tenant_id=TENANT_A))
    before = await store.get_document(index_name, "QA-DEPOT-X")
    mirror_calls.clear()

    with pytest.raises(AppException) as exc_info:
        await repo.create(TENANT_B, dict(payload, tenant_id=TENANT_B, name="B's depot"))

    _assert_already_exists(exc_info.value)
    assert await store.get_document(index_name, "QA-DEPOT-X") == before
    assert before["tenant_id"] == TENANT_A
    assert mirror_calls == []


async def test_station_create_cannot_take_over_another_tenants_id(
    store, index_name, mirror_calls
):
    from fuel.models import CreateFuelStation
    from fuel.services.fuel_service import FuelService

    svc = FuelService(_IndexFacade(store, index_name))
    station = CreateFuelStation(
        station_id="QA-ST-X",
        name="A's station",
        fuel_type="AGO",
        capacity_liters=10000.0,
        initial_stock_liters=5000.0,
    )
    await svc.create_station(station, TENANT_A)
    doc_id = svc._make_doc_id("QA-ST-X", svc._canonical_fuel_type("AGO"))
    before = await store.get_document(index_name, doc_id)
    mirror_calls.clear()

    with pytest.raises(AppException) as exc_info:
        await svc.create_station(
            station.model_copy(update={"name": "B's station", "initial_stock_liters": 0.0}),
            TENANT_B,
        )

    _assert_already_exists(exc_info.value)
    assert await store.get_document(index_name, doc_id) == before
    assert before["tenant_id"] == TENANT_A
    assert before["current_stock_liters"] == 5000.0
    assert mirror_calls == []


async def test_same_tenant_duplicate_is_also_refused(store, index_name):
    from fuel.driver_repository import DriverRepository

    repo = DriverRepository(store, drivers_index=index_name)
    await repo.create(TENANT_A, _driver("QA-DRV-Y", "first"))

    with pytest.raises(AppException) as exc_info:
        await repo.create(TENANT_A, _driver("QA-DRV-Y", "second"))

    assert exc_info.value.error_code is ErrorCode.RESOURCE_ALREADY_EXISTS
    assert (await store.get_document(index_name, "QA-DRV-Y"))["driver_name"] == "first"
