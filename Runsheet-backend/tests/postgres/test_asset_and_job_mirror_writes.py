"""GPS updates keep an asset findable, and job writes reach the Postgres row.

Two defects from the fleet / fuel-ops staging re-test, both run here against the
real document store *and* the real relational mirror with reads cut over
(``COMMERCE_READ_FROM_POSTGRES``), which is how staging runs:

* N-FF-1. ``DataIngestionService.process_location_update`` wrote the location
  with ``index_document`` — a full replace — so the ``trucks`` document kept only
  the location fields. It lost ``asset_id`` / ``truck_id`` / ``asset_type`` and
  fell out of every asset search: the next ping 404'd, job-create with the asset
  404'd and the ops-driver truck assignment 422'd.
* N-FF-2. The driver accept/reject endpoints and the cargo-item status write
  changed ``jobs_current`` in the document store only. ``GET /jobs/{id}`` reads
  the Postgres row, so it showed the old status / item, and the driver's ack
  (which checks the status on that row) was refused.

Every fixed index is routed under this test's unique prefix (the conftest
cleanup removes those rows); the relational rows use a per-test tenant and are
deleted by it. Real PostgreSQL required — see ``conftest.py``.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from commerce.services.commerce_persistence_bridge import (
    mirror_current_state_upsert,
    read_hybrid_get,
)
from config.settings import clear_settings_cache
from persistence import database
from persistence.database import session_scope
from tests.support.auth_seam import auth_headers, install_test_auth

_INDEX_METHODS = {
    "get_document", "index_document", "update_document", "delete_document",
    "atomic_update", "create_document", "search_documents", "document_exists",
    "count",
}


class _Namespaced:
    """The real ``PostgresDocumentStore`` with every index under ``prefix``."""

    def __init__(self, inner: Any, prefix: str) -> None:
        self._inner = inner
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in _INDEX_METHODS:
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            if "index" in kwargs:
                kwargs["index"] = self._prefix + kwargs["index"]
            else:
                args = (self._prefix + args[0], *args[1:])
            return await attr(*args, **kwargs)

        return call


@pytest.fixture
async def mirrored(postgres_url, monkeypatch):
    """Dual-write on and reads cut over to Postgres, as on staging."""
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    await database.dispose_engine()
    try:
        async with session_scope() as session:
            await session.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        await database.dispose_engine()
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    yield
    await database.dispose_engine()
    clear_settings_cache()


@pytest.fixture
async def tenant(mirrored):
    tenant_id = f"pytest-ffmirror-{uuid.uuid4().hex[:12]}"
    yield tenant_id
    async with session_scope() as session:
        for table in ("jobs_current", "trucks", "truck_compartments", "outbox_events"):
            await session.execute(
                text(f"delete from {table} where tenant_id = :t"), {"t": tenant_id}
            )


@pytest.fixture
def es(store, index_name) -> _Namespaced:
    return _Namespaced(store, f"{index_name}__")


def _ping(asset_id: str, tenant_id: str, lat: float):
    from ingestion.service import LocationUpdate

    return LocationUpdate(
        asset_id=asset_id, latitude=lat, longitude=-95.3,
        timestamp="2026-10-06T12:00:00Z", tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# N-FF-1: a GPS update merges into the asset; it never replaces it
# ---------------------------------------------------------------------------


async def test_gps_update_keeps_the_asset_searchable_and_usable(es, tenant, monkeypatch):
    from fuel.api import driver_endpoints as ops_driver_endpoints
    from ingestion.service import DataIngestionService
    from scheduling.models import CreateJob, JobType
    from scheduling.services.job_service import JobService
    from services.ref_loaders import make_asset_loader
    from services.ref_resolver import RefResolver

    asset_id = f"QA-TRK-{uuid.uuid4().hex[:8]}"
    truck = {
        "asset_id": asset_id, "truck_id": asset_id, "tenant_id": tenant,
        "asset_type": "vehicle", "asset_subtype": "truck", "name": "QA Truck",
        "status": "active",
    }
    # As POST /api/fleet/assets does: create in the store, mirror to Postgres.
    assert await es.create_document("trucks", asset_id, dict(truck))
    await mirror_current_state_upsert("truck", dict(truck), doc_id=asset_id)

    ingest = DataIngestionService(es_service=es)
    first = await ingest.process_location_update(_ping(asset_id, tenant, 29.7))
    assert first.success is True

    # Still found, by either id field, with its identity intact.
    for field in ("asset_id", "truck_id"):
        resp = await es.search_documents(
            "trucks", {"query": {"bool": {"filter": [
                {"term": {field: asset_id}}, {"term": {"tenant_id": tenant}},
            ]}}, "size": 1}, 1,
        )
        assert resp["hits"]["total"]["value"] == 1, field
    stored = await es.get_document("trucks", asset_id)
    assert stored["asset_type"] == "vehicle"
    assert stored["name"] == "QA Truck"
    assert stored["current_location"]["coordinates"]["lat"] == 29.7

    # The Postgres row is the merged asset too, not just the location fields.
    pg_truck = await read_hybrid_get("truck", tenant, asset_id)
    assert pg_truck["asset_type"] == "vehicle"
    assert pg_truck["current_location"]["coordinates"]["lat"] == 29.7

    # The second ping used to 404: the asset check could no longer find it.
    second = await ingest.process_location_update(_ping(asset_id, tenant, 29.8))
    assert second.success is True
    assert (await es.get_document("trucks", asset_id))["current_location"][
        "coordinates"]["lat"] == 29.8

    # Job create with the asset used to 404 on the compatibility lookup.
    job = await JobService(es).create_job(
        CreateJob(
            job_type=JobType.PASSENGER_TRANSPORT, origin="QA Depot", destination="QA Site",
            scheduled_time="2026-10-07T09:00:00Z", asset_assigned=asset_id,
        ),
        tenant, actor_id="qa-dispatcher",
    )
    assert job.asset_assigned == asset_id

    # The ops-driver truck assignment used to 422 on the same lookup.
    resolver = RefResolver()
    resolver.register("asset", make_asset_loader(es))
    monkeypatch.setattr(ops_driver_endpoints, "_ref_resolver", resolver)
    await ops_driver_endpoints._validate_assigned_truck(tenant, asset_id)


# ---------------------------------------------------------------------------
# N-FF-2: driver and cargo job writes reach the Postgres row GET reads
# ---------------------------------------------------------------------------


def _scheduling_app(es) -> FastAPI:
    from errors.handlers import register_exception_handlers
    from scheduling.api import driver_endpoints, endpoints
    from scheduling.services.cargo_service import CargoService
    from scheduling.services.delay_detection_service import DelayDetectionService
    from scheduling.services.job_service import JobService

    job_service = JobService(es)
    endpoints.configure_scheduling_api(
        job_service=job_service,
        cargo_service=CargoService(es),
        delay_service=DelayDetectionService(es_service=es, ws_manager=None),
    )
    driver_endpoints.configure_driver_endpoints(job_service=job_service)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(endpoints.router)
    app.include_router(driver_endpoints.router)
    install_test_auth(app)
    return app


async def test_driver_accept_then_ack_and_cargo_status_are_visible_on_get(es, tenant):
    from scheduling.models import CargoItem, CreateJob, JobType
    from scheduling.services.job_service import JobService

    driver_id = f"QA-DRV-{uuid.uuid4().hex[:6]}"
    job = await JobService(es).create_job(
        CreateJob(
            job_type=JobType.CARGO_TRANSPORT, origin="QA Depot", destination="QA Site",
            scheduled_time="2026-10-07T09:00:00Z",
            cargo_manifest=[CargoItem(item_id="QA-ITEM-1", description="QA pallet", weight_kg=10)],
        ),
        tenant, actor_id="qa-dispatcher",
    )
    job_id = job.job_id
    driver = auth_headers(tenant, sub="qa-driver", roles=["driver"], driver_id=driver_id)
    dispatcher = auth_headers(tenant, sub="qa-dispatcher", roles=["dispatcher"])

    transport = httpx.ASGITransport(app=_scheduling_app(es))
    async with httpx.AsyncClient(transport=transport, base_url="http://qa") as client:
        accept = await client.post(f"/api/scheduling/jobs/{job_id}/accept", headers=driver)
        assert accept.status_code == 200, accept.text
        assert accept.json()["data"]["new_status"] == "assigned"

        got = await client.get(f"/api/scheduling/jobs/{job_id}", headers=dispatcher)
        assert got.status_code == 200, got.text
        assert got.json()["data"]["job"]["status"] == "assigned"

        # Refused with the stale row: ack is only allowed from ``assigned``.
        ack = await client.post(
            f"/api/scheduling/jobs/{job_id}/ack", headers=driver,
            json={"device_id": "QA-DEVICE-1"},
        )
        assert ack.status_code == 200, ack.text

        patched = await client.patch(
            f"/api/scheduling/jobs/{job_id}/cargo/QA-ITEM-1/status", headers=dispatcher,
            json={"item_id": "QA-ITEM-1", "item_status": "loaded"},
        )
        assert patched.status_code == 200, patched.text

        got = await client.get(f"/api/scheduling/jobs/{job_id}", headers=dispatcher)
        assert got.status_code == 200, got.text
        items = got.json()["data"]["job"]["cargo_manifest"]
        assert [i["item_status"] for i in items if i["item_id"] == "QA-ITEM-1"] == ["loaded"]

    # The row GET reads, directly.
    pg_job = await read_hybrid_get("job", tenant, job_id)
    assert pg_job["status"] == "assigned"
    assert pg_job["assigned_driver_id"] == driver_id


async def test_cargo_item_status_patch_is_visible_on_get(es, tenant):
    """The cargo write on its own, so it is proven independently of accept."""
    from scheduling.models import CargoItem, CreateJob, JobType
    from scheduling.services.job_service import JobService

    job = await JobService(es).create_job(
        CreateJob(
            job_type=JobType.CARGO_TRANSPORT, origin="QA Depot", destination="QA Site",
            scheduled_time="2026-10-07T09:00:00Z",
            cargo_manifest=[
                CargoItem(item_id="QA-ITEM-A", description="QA pallet A", weight_kg=10),
                CargoItem(item_id="QA-ITEM-B", description="QA pallet B", weight_kg=12),
            ],
        ),
        tenant, actor_id="qa-dispatcher",
    )
    dispatcher = auth_headers(tenant, sub="qa-dispatcher", roles=["dispatcher"])

    transport = httpx.ASGITransport(app=_scheduling_app(es))
    async with httpx.AsyncClient(transport=transport, base_url="http://qa") as client:
        patched = await client.patch(
            f"/api/scheduling/jobs/{job.job_id}/cargo/QA-ITEM-B/status", headers=dispatcher,
            json={"item_id": "QA-ITEM-B", "item_status": "in_transit"},
        )
        assert patched.status_code == 200, patched.text
        got = await client.get(f"/api/scheduling/jobs/{job.job_id}", headers=dispatcher)
        assert got.status_code == 200, got.text
        statuses = {i["item_id"]: i["item_status"] for i in got.json()["data"]["job"]["cargo_manifest"]}
        assert statuses == {"QA-ITEM-A": "pending", "QA-ITEM-B": "in_transit"}


async def test_driver_reject_is_visible_on_get(es, tenant):
    from scheduling.models import CreateJob, JobType
    from scheduling.services.job_service import JobService

    driver_id = f"QA-DRV-{uuid.uuid4().hex[:6]}"
    job = await JobService(es).create_job(
        CreateJob(
            job_type=JobType.PASSENGER_TRANSPORT, origin="QA Depot", destination="QA Site",
            scheduled_time="2026-10-07T09:00:00Z",
        ),
        tenant, actor_id="qa-dispatcher",
    )
    driver = auth_headers(tenant, sub="qa-driver", roles=["driver"], driver_id=driver_id)
    dispatcher = auth_headers(tenant, sub="qa-dispatcher", roles=["dispatcher"])

    transport = httpx.ASGITransport(app=_scheduling_app(es))
    async with httpx.AsyncClient(transport=transport, base_url="http://qa") as client:
        accept = await client.post(f"/api/scheduling/jobs/{job.job_id}/accept", headers=driver)
        assert accept.status_code == 200, accept.text
        reject = await client.post(
            f"/api/scheduling/jobs/{job.job_id}/reject", headers=driver,
            json={"reason": "QA reject"},
        )
        assert reject.status_code == 200, reject.text
        got = await client.get(f"/api/scheduling/jobs/{job.job_id}", headers=dispatcher)
        assert got.json()["data"]["job"]["status"] == "scheduled"


# ---------------------------------------------------------------------------
# Other full-replace writes to existing ids found by the N-FF-1 sweep
# ---------------------------------------------------------------------------


async def test_reconfiguring_a_compartment_keeps_what_it_last_carried(es, tenant, monkeypatch):
    """PUT /compartments replaced the doc and erased the contamination history."""
    from Agents.support import mvp_endpoints

    truck_id = f"QA-TRK-{uuid.uuid4().hex[:8]}"
    doc_id = f"{truck_id}_C1"
    await es.index_document("truck_compartments", doc_id, {
        "compartment_id": "C1", "truck_id": truck_id, "tenant_id": tenant,
        "capacity_liters": 5000.0, "allowed_grades": ["DIESEL_2"], "position_index": 0,
        "state": "loaded", "last_loaded_product": "DIESEL_2",
        "last_loaded_at": "2026-10-05T08:00:00+00:00",
    })
    monkeypatch.setattr(mvp_endpoints, "_es_service", es)
    monkeypatch.setattr(mvp_endpoints, "_fleet_registration_service", None)
    app = FastAPI()
    app.include_router(mvp_endpoints.router)
    install_test_auth(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://qa") as client:
        resp = await client.put(
            f"/api/fuel/mvp/compartments/{truck_id}", headers=auth_headers(tenant),
            json={"compartments": [{
                "compartment_id": "C1", "capacity_liters": 6000.0,
                "allowed_grades": ["DIESEL_2", "GASOLINE_REG"], "position_index": 0,
            }]},
        )
        assert resp.status_code == 200, resp.text

    stored = await es.get_document("truck_compartments", doc_id)
    assert stored["capacity_liters"] == 6000.0
    assert stored["allowed_grades"] == ["DIESEL_2", "GASOLINE_REG"]
    assert stored["state"] == "loaded"
    assert stored["last_loaded_product"] == "DIESEL_2"
    assert stored["last_loaded_at"] == "2026-10-05T08:00:00+00:00"


async def test_fleet_registration_never_replaces_an_existing_truck(es, tenant):
    """The create branch runs when the existence read fails; it must not clobber."""
    from fuel.services.fleet_registration_service import FleetRegistrationService

    truck_id = f"QA-TRK-{uuid.uuid4().hex[:8]}"
    truck = {
        "asset_id": truck_id, "truck_id": truck_id, "tenant_id": tenant,
        "asset_type": "fuel_tanker", "name": "QA Tanker", "plate_number": "QA-1",
    }
    await es.index_document("trucks", truck_id, dict(truck))

    await FleetRegistrationService(es)._create_fleet_document(truck_id, tenant, 9000.0)

    stored = await es.get_document("trucks", truck_id)
    assert stored["asset_id"] == truck_id
    assert stored["name"] == "QA Tanker"
    assert stored["plate_number"] == "QA-1"
