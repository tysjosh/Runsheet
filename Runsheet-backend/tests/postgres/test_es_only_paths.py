"""Lookups written for Elasticsearch that find nothing on the Postgres store.

Three shapes, each silently wrong on the store that ships (findings B1, B2, B3):

* ``{"term": {"_id": X}}``. ES matches the document id; the store's translator
  reads ``_id`` as a body field, which no document has, so it matches nothing.
  ``ids`` is the clause that maps to the store's doc id. Hit: PATCH, GET of
  ``/fleet/assets/{id}``, GET ``/fleet/trucks/{id}`` and the inspection
  out-of-service write.
* The ``assets`` index. It was an ES alias for ``trucks``; the store has no
  aliases, so ``assets`` is an empty index. Hit: the GPS ingest asset check,
  which also failed OPEN on any error.
* ``es.client.update`` with a painless script. There is no cluster behind
  ``.client``. Hit: cargo item status.

Each test runs the real caller over the real store, with ``trucks`` and
``jobs_current`` routed to the per-test index and every other index name routed
to its own empty per-test index, which is what an unknown index is on the store.

Real PostgreSQL required — see ``conftest.py``.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


class _RoutedFacade:
    """The facade methods callers use, over the store, with index routing.

    ``trucks`` and ``jobs_current`` map to the test's index. Any other name maps
    to a distinct empty index under the same prefix, so a read of ``assets``
    behaves exactly as it does in production: no rows. There is deliberately no
    ``client`` attribute — production's raises on any data-plane call.
    """

    _ROUTED = ("trucks", "jobs_current")

    def __init__(self, store, index_name: str) -> None:
        self._store = store
        self._index = index_name

    def _route(self, index: str) -> str:
        return self._index if index in self._ROUTED else f"{self._index}__{index}"

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100, **kw):
        return await self._store.search_documents(self._route(index), query, size)

    async def get_document(self, index: str, doc_id: str):
        return await self._store.get_document(self._route(index), doc_id)

    async def index_document(self, index: str, doc_id: str, doc: Dict[str, Any], **kw):
        return await self._store.index_document(self._route(index), doc_id, doc)

    async def update_document(self, index: str, doc_id: str, partial: Dict[str, Any]):
        return await self._store.update_document(self._route(index), doc_id, partial)

    async def atomic_update(self, index: str, doc_id: str, transform, *, upsert=None, **kw):
        return await self._store.atomic_update(
            self._route(index), doc_id, transform, upsert=upsert
        )


def _truck(asset_id: str, tenant_id: str, *, key: str = "asset_id") -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "asset_type": "vehicle",
        "asset_subtype": "truck",
        "asset_name": f"QA {asset_id}",
        "status": "active",
        "plate_number": "QA-1",
        "tenant_id": tenant_id,
        "current_location": {
            "id": "LOC-1",
            "name": "Yard",
            "type": "warehouse",
            "coordinates": {"lat": 25.0, "lng": 55.0},
            "address": "Yard",
        },
    }
    doc[key] = asset_id
    return doc


# ---------------------------------------------------------------------------
# B1 and the other ``term _id`` reads, through the HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture
def api(store, index_name):
    """The real app with ``data_endpoints`` reading the routed store."""
    from fastapi.testclient import TestClient

    from tests.support.auth_seam import auth_headers, install_test_auth

    facade = _RoutedFacade(store, index_name)
    mirror = AsyncMock()
    with patch("data_endpoints.elasticsearch_service", facade), patch(
        "commerce.services.commerce_persistence_bridge.mirror_current_state_upsert",
        mirror,
    ):
        from main import app

        install_test_auth(app)
        try:
            # Not a context manager: entering it would run main's lifespan.
            yield TestClient(app, headers=auth_headers(TENANT_A)), mirror
        finally:
            app.dependency_overrides.clear()


async def test_patch_asset_updates_an_existing_truck(api, store, index_name):
    client, mirror = api
    await store.index_document(index_name, "QA-TRK-1", _truck("QA-TRK-1", TENANT_A))

    resp = client.patch("/api/fleet/assets/QA-TRK-1", json={"name": "Renamed"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == "QA-TRK-1"
    stored = await store.get_document(index_name, "QA-TRK-1")
    assert stored["asset_name"] == "Renamed"
    mirror.assert_awaited_once()
    assert mirror.await_args.kwargs["doc_id"] == "QA-TRK-1"
    assert mirror.await_args.args[1]["asset_name"] == "Renamed"


async def test_patch_asset_of_another_tenant_is_404_and_unchanged(api, store, index_name):
    client, mirror = api
    await store.index_document(index_name, "QA-TRK-2", _truck("QA-TRK-2", TENANT_B))

    resp = client.patch("/api/fleet/assets/QA-TRK-2", json={"name": "Hijacked"})

    assert resp.status_code == 404
    assert (await store.get_document(index_name, "QA-TRK-2"))["asset_name"] == "QA QA-TRK-2"
    mirror.assert_not_awaited()


async def test_get_asset_by_id_finds_an_existing_truck(api, store, index_name):
    client, _ = api
    await store.index_document(index_name, "QA-TRK-3", _truck("QA-TRK-3", TENANT_A))

    resp = client.get("/api/fleet/assets/QA-TRK-3")

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == "QA-TRK-3"


async def test_get_asset_by_id_of_another_tenant_is_404(api, store, index_name):
    client, _ = api
    await store.index_document(index_name, "QA-TRK-4", _truck("QA-TRK-4", TENANT_B))

    assert client.get("/api/fleet/assets/QA-TRK-4").status_code == 404


async def test_get_truck_by_id_finds_an_existing_truck(api, store, index_name):
    client, _ = api
    await store.index_document(
        index_name, "QA-TRK-5", _truck("QA-TRK-5", TENANT_A, key="truck_id")
    )

    resp = client.get("/api/fleet/trucks/QA-TRK-5")

    assert resp.status_code == 200, resp.text


async def test_asset_list_reads_the_trucks_index(api, store, index_name):
    client, _ = api
    await store.index_document(index_name, "QA-TRK-6", _truck("QA-TRK-6", TENANT_A))
    await store.index_document(index_name, "QA-TRK-7", _truck("QA-TRK-7", TENANT_B))

    resp = client.get("/api/fleet/assets")

    assert resp.status_code == 200, resp.text
    assert [a["id"] for a in resp.json()["data"]] == ["QA-TRK-6"]


# ---------------------------------------------------------------------------
# Inspection out-of-service write
# ---------------------------------------------------------------------------


async def test_inspection_marks_an_existing_truck_out_of_service(store, index_name):
    from driver.services.inspection_service import InspectionService

    await store.index_document(index_name, "QA-TRK-8", _truck("QA-TRK-8", TENANT_A))
    svc = InspectionService.__new__(InspectionService)
    svc._es_service = _RoutedFacade(store, index_name)

    assert await svc._set_asset_out_of_service(tenant_id=TENANT_A, asset_id="QA-TRK-8") is True
    assert (await store.get_document(index_name, "QA-TRK-8"))["operational_state"] == "out_of_service"


async def test_inspection_cannot_touch_another_tenants_truck(store, index_name):
    from driver.services.inspection_service import InspectionService

    await store.index_document(index_name, "QA-TRK-9", _truck("QA-TRK-9", TENANT_B))
    svc = InspectionService.__new__(InspectionService)
    svc._es_service = _RoutedFacade(store, index_name)

    assert await svc._set_asset_out_of_service(tenant_id=TENANT_A, asset_id="QA-TRK-9") is False
    assert "operational_state" not in await store.get_document(index_name, "QA-TRK-9")


# ---------------------------------------------------------------------------
# B2: GPS ingest asset check
# ---------------------------------------------------------------------------


async def test_gps_asset_check_finds_a_truck_by_asset_id(store, index_name):
    from ingestion.service import DataIngestionService

    await store.index_document(index_name, "QA-GPS-1", _truck("QA-GPS-1", TENANT_A))
    svc = DataIngestionService(es_service=_RoutedFacade(store, index_name))

    assert await svc.validate_asset_exists("QA-GPS-1", tenant_id=TENANT_A) is True


async def test_gps_asset_check_finds_a_legacy_truck_by_truck_id(store, index_name):
    from ingestion.service import DataIngestionService

    await store.index_document(
        index_name, "QA-GPS-2", _truck("QA-GPS-2", TENANT_A, key="truck_id")
    )
    svc = DataIngestionService(es_service=_RoutedFacade(store, index_name))

    assert await svc.validate_asset_exists("QA-GPS-2", tenant_id=TENANT_A) is True


async def test_gps_asset_check_is_false_for_another_tenants_truck(store, index_name):
    from ingestion.service import DataIngestionService

    await store.index_document(index_name, "QA-GPS-3", _truck("QA-GPS-3", TENANT_B))
    svc = DataIngestionService(es_service=_RoutedFacade(store, index_name))

    assert await svc.validate_asset_exists("QA-GPS-3", tenant_id=TENANT_A) is False


async def test_gps_asset_check_is_false_for_an_unknown_truck(store, index_name):
    from ingestion.service import DataIngestionService

    svc = DataIngestionService(es_service=_RoutedFacade(store, index_name))

    assert await svc.validate_asset_exists("QA-GPS-NOPE", tenant_id=TENANT_A) is False


# ---------------------------------------------------------------------------
# B3: cargo item status
# ---------------------------------------------------------------------------


def _cargo_job(job_id: str) -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "job_type": "cargo_transport",
        "status": "in_progress",
        "tenant_id": TENANT_A,
        "cargo_manifest": [
            {"item_id": "QA-C1", "description": "Pipes", "weight_kg": 10.0, "item_status": "pending"},
            {"item_id": "QA-C2", "description": "Bags", "weight_kg": 5.0, "item_status": "loaded"},
        ],
    }


async def test_cargo_item_status_changes_only_the_target_item(store, index_name):
    from scheduling.models import CargoItemStatus
    from scheduling.services.cargo_service import CargoService

    await store.index_document(index_name, "QA-JOB-1", _cargo_job("QA-JOB-1"))
    svc = CargoService(es_service=_RoutedFacade(store, index_name))

    result = await svc.update_cargo_item_status(
        "QA-JOB-1", "QA-C1", CargoItemStatus.IN_TRANSIT, TENANT_A, actor_id="QA-user"
    )

    assert result["item_id"] == "QA-C1"
    assert result["item_status"] == "in_transit"
    stored = await store.get_document(index_name, "QA-JOB-1")
    statuses = {i["item_id"]: i["item_status"] for i in stored["cargo_manifest"]}
    assert statuses == {"QA-C1": "in_transit", "QA-C2": "loaded"}
    assert stored["cargo_manifest"][0]["description"] == "Pipes"


async def test_cargo_item_status_for_another_tenants_job_is_404(store, index_name):
    from errors.exceptions import AppException
    from scheduling.models import CargoItemStatus
    from scheduling.services.cargo_service import CargoService

    await store.index_document(index_name, "QA-JOB-2", _cargo_job("QA-JOB-2"))
    svc = CargoService(es_service=_RoutedFacade(store, index_name))

    with pytest.raises(AppException) as exc_info:
        await svc.update_cargo_item_status(
            "QA-JOB-2", "QA-C1", CargoItemStatus.DELIVERED, TENANT_B
        )

    assert exc_info.value.status_code == 404
    stored = await store.get_document(index_name, "QA-JOB-2")
    assert stored["cargo_manifest"][0]["item_status"] == "pending"
