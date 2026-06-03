"""Dual-write tests for master-data aggregates.

Covers the hybrid master-data tables (drivers, depots, terminals,
asset_certifications, intake_channels, trucks, locations): upserts store the
verbatim ES document + typed index columns and enqueue an outbox projection
event; the projector round-trips the document; trucks/locations tolerate a
missing tenant_id (legacy generic-ES write path).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from persistence.database import session_scope
from persistence.models import (
    AssetCertificationORM,
    DepotORM,
    DriverMasterORM,
    IntakeChannelORM,
    LocationORM,
    OutboxEventORM,
    TerminalORM,
    TruckORM,
)
from persistence.projections import _document_passthrough
from persistence.repositories import CurrentStateRepository

TENANT = "demo-tenant"


async def test_driver_upsert_and_passthrough(engine):
    repo = CurrentStateRepository("driver")
    doc = {
        "driver_id": "drv_1", "tenant_id": TENANT, "full_name": "Jane Doe",
        "cdl_number": "CDL-9", "cdl_class": "A", "status": "active",
        "cdl_expiry_date": "2027-01-01", "external_refs": {"hr": "e123"},
    }
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        assert row.cdl_number == "CDL-9"
        assert row.status == "active"
        assert _document_passthrough(row) == doc

    async with session_scope() as s:
        ev = (await s.execute(select(OutboxEventORM))).scalars().all()
    assert len(ev) == 1
    assert ev[0].aggregate_type == "driver"
    assert ev[0].target_index == "drivers"


async def test_depot_default_flag_lifted(engine):
    repo = CurrentStateRepository("depot")
    doc = {"depot_id": "depot_1", "tenant_id": TENANT, "name": "North",
           "is_default": True, "status": "active"}
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        assert row.is_default is True


async def test_terminal_and_asset_cert_and_channel(engine):
    async with session_scope() as s:
        await CurrentStateRepository("terminal").upsert(s, doc={
            "terminal_id": "term_1", "tenant_id": TENANT, "status": "active",
            "operator": "BigOil"})
        await CurrentStateRepository("asset_certification").upsert(s, doc={
            "cert_id": "cert_1", "tenant_id": TENANT, "asset_id": "TRUCK-1",
            "certification_type": "V_test", "status": "valid"})
        await CurrentStateRepository("intake_channel").upsert(s, doc={
            "channel_id": "ch_1", "tenant_id": TENANT, "channel_type": "voice",
            "enabled": True})
    async with session_scope() as s:
        assert (await s.get(TerminalORM, "term_1")).status == "active"
        assert (await s.get(AssetCertificationORM, "cert_1")).asset_id == "TRUCK-1"
        assert (await s.get(IntakeChannelORM, "ch_1")).tenant_id == TENANT


async def test_truck_with_explicit_doc_id_and_missing_tenant(engine):
    """Legacy trucks write passes doc_id explicitly and may omit tenant_id."""
    repo = CurrentStateRepository("truck")
    doc = {"asset_id": "TRUCK-9", "plate_number": "KAA-001", "status": "active"}
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc, doc_id="TRUCK-9")
        assert row.truck_id == "TRUCK-9"
        assert row.tenant_id == "unknown"  # defaulted when absent
        assert _document_passthrough(row) == doc


async def test_location_upsert(engine):
    repo = CurrentStateRepository("location")
    doc = {"location_id": "loc_1", "tenant_id": TENANT, "name": "Yard A"}
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        assert row.location_id == "loc_1"


async def test_driver_service_wiring(engine, monkeypatch):
    """DriverQualificationService.create_driver mirrors into Postgres."""
    from unittest.mock import AsyncMock

    from config.settings import clear_settings_cache
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    clear_settings_cache()

    from compliance.services.driver_qualification_service import (
        DriverQualificationService,
    )

    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    service = DriverQualificationService(es)

    from datetime import date
    result = await service.create(
        TENANT, full_name="John Q", cdl_number="CDL-1", cdl_state="CA",
        cdl_class="A", cdl_expiry_date=date(2027, 6, 1),
        medical_card_expiry_date=date(2027, 6, 1), status="active",
    )

    async with session_scope() as s:
        row = await s.get(DriverMasterORM, result["driver_id"])
    assert row is not None
    assert row.cdl_number == "CDL-1"
    clear_settings_cache()
