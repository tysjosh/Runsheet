"""Dual-write tests for orders/jobs current-state aggregates.

Covers the hybrid current-state tables (fuel_orders_current, jobs_current,
tenant_job_policies): upserts store the verbatim ES
document + typed index columns and enqueue an outbox projection event, the
stale-event guard discards out-of-order writes (mirroring the ES scripted
upsert), and the projector round-trips the document byte-for-byte.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from persistence.database import session_scope
from persistence.models import (
    FuelOrderCurrentORM,
    JobCurrentORM,
    OutboxEventORM,
    TenantJobPolicyORM,
)
from persistence.projections import _document_passthrough
from persistence.repositories import CurrentStateRepository

TENANT = "demo-tenant"


async def test_fuel_order_upsert_and_passthrough(engine):
    repo = CurrentStateRepository("fuel_order")
    doc = {
        "order_id": "ord_1", "tenant_id": TENANT, "customer_id": "cust_1",
        "product_code": "DSL", "gallons_requested": 500.0, "status": "received",
        "assigned_driver_id": None, "ship_to_geo": {"lat": 40.0, "lon": -75.0},
        "intake_metadata": {"call_id": "c1", "agent_confidence": 0.9},
        "last_event_timestamp": "2026-01-01T10:00:00+00:00",
        "created_at": "2026-01-01T10:00:00+00:00",
        "updated_at": "2026-01-01T10:00:00+00:00",
    }
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        assert row.status == "received"
        assert row.customer_id == "cust_1"
        # Verbatim document incl. nested geo + intake_metadata.
        assert _document_passthrough(row) == doc

    async with session_scope() as s:
        outbox = (await s.execute(select(OutboxEventORM))).scalars().all()
    assert len(outbox) == 1
    assert outbox[0].aggregate_type == "fuel_order"
    assert outbox[0].target_index == "fuel_orders_current"


async def test_fuel_order_stale_event_discarded(engine):
    repo = CurrentStateRepository("fuel_order")
    base = {
        "order_id": "ord_2", "tenant_id": TENANT, "status": "en_route",
        "last_event_timestamp": "2026-01-02T12:00:00+00:00",
    }
    async with session_scope() as s:
        await repo.upsert(s, doc=base)

    # Older event -> discarded, status unchanged.
    stale = {**base, "status": "received",
             "last_event_timestamp": "2026-01-02T11:00:00+00:00"}
    async with session_scope() as s:
        result = await repo.upsert(s, doc=stale)
        assert result is None
    async with session_scope() as s:
        row = await s.get(FuelOrderCurrentORM, "ord_2")
        assert row.status == "en_route"  # not regressed

    # Newer event -> applied.
    newer = {**base, "status": "delivered",
             "last_event_timestamp": "2026-01-02T13:00:00+00:00"}
    async with session_scope() as s:
        result = await repo.upsert(s, doc=newer)
        assert result is not None
    async with session_scope() as s:
        row = await s.get(FuelOrderCurrentORM, "ord_2")
        assert row.status == "delivered"


async def test_job_upsert(engine):
    repo = CurrentStateRepository("job")
    doc = {
        "job_id": "job_1", "tenant_id": TENANT, "job_type": "delivery",
        "status": "scheduled", "asset_assigned": "TRUCK-1",
        "cargo_manifest": [{"item_id": "i1", "weight_kg": 10.0}],
        "last_event_timestamp": "2026-01-01T00:00:00+00:00",
    }
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        assert row.status == "scheduled"
        assert _document_passthrough(row)["cargo_manifest"][0]["item_id"] == "i1"


# ``test_shipment_upsert`` stood here. The ``shipment`` aggregate was retired
# with the ``shipments_current`` table (rev 0007).


async def test_tenant_job_policy_keyed_by_tenant(engine):
    repo = CurrentStateRepository("tenant_job_policy")
    doc = {
        "tenant_id": TENANT, "pod_required": True, "pod_radius_meters": 100,
        "otp_required": False, "nudge_timeout_minutes": 30,
    }
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        # policy_id PK is the tenant_id.
        assert row.policy_id == TENANT
        assert _document_passthrough(row)["pod_required"] is True
    # Re-upsert (same tenant) updates rather than duplicating.
    doc["nudge_timeout_minutes"] = 45
    async with session_scope() as s:
        await repo.upsert(s, doc=doc)
    async with session_scope() as s:
        count = await s.scalar(select(func.count()).select_from(TenantJobPolicyORM))
        row = await s.get(TenantJobPolicyORM, TENANT)
    assert count == 1
    assert row.document["nudge_timeout_minutes"] == 45


async def test_unknown_current_state_type_rejected(engine):
    with pytest.raises(ValueError):
        CurrentStateRepository("not_a_state")


async def test_service_wiring_fuel_order_create(engine, monkeypatch):
    """FuelOrderRepository.create mirrors into Postgres when dual-write is on."""
    from unittest.mock import AsyncMock

    from config.settings import clear_settings_cache
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    clear_settings_cache()

    from fuel.order_repository import FuelOrderRepository

    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    repo = FuelOrderRepository(es)

    order = {
        "order_id": "ord_svc", "tenant_id": TENANT, "customer_id": "cust_1",
        "customer_name": "Acme", "ship_to_address": "1 Main St",
        "ship_to_lat": 40.0, "ship_to_lon": -75.0, "call_type": "will_call",
        "fill_to_full": True, "product_code": "DIESEL_2",
        "intake_channel": "dispatcher", "intake_channel_id": "ch_1",
        "status": "placed", "source_schema_version": "1.0", "trace_id": "t1",
        "last_event_timestamp": "2026-01-01T00:00:00+00:00",
    }
    await repo.create(TENANT, order)

    async with session_scope() as s:
        row = await s.get(FuelOrderCurrentORM, "ord_svc")
    assert row is not None
    assert row.status == "placed"
    clear_settings_cache()
