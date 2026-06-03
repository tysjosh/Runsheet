"""Read-cutover tests for the hybrid aggregates (master data + config).

With ``COMMERCE_READ_FROM_POSTGRES`` on, the master-data / config services
serve get/list from the Postgres ``document`` column (byte-identical) and the
ES client is NOT queried for those reads. With the flag off they fall through
to ES.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.read_repositories import HybridReadRepository
from persistence.repositories import CurrentStateRepository

TENANT = "demo-tenant"


@pytest.fixture
def read_from_pg(monkeypatch):
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is True
    yield
    clear_settings_cache()


def _es_raises_on_read():
    es = AsyncMock()
    es.search_documents = AsyncMock(
        side_effect=AssertionError("ES read path must not be used after cutover")
    )
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


_CONFIG_AGGREGATES = {
    "tax_jurisdiction", "tax_exemption", "price_protection_contract",
    "compliance_pricing_rule", "supplier_contract",
}


async def _seed(aggregate, doc, doc_id=None):
    from persistence.repositories import ComplianceConfigRepository
    async with session_scope() as s:
        if aggregate in _CONFIG_AGGREGATES:
            await ComplianceConfigRepository(aggregate).upsert(s, doc=doc)
        else:
            await CurrentStateRepository(aggregate).upsert(s, doc=doc, doc_id=doc_id)


# ---------------------------------------------------------------------------
# Repository-level (HybridReadRepository) coverage
# ---------------------------------------------------------------------------


async def test_hybrid_get_returns_verbatim_document(engine, read_from_pg):
    await _seed("driver", {
        "driver_id": "drv_1", "tenant_id": TENANT, "full_name": "Jane",
        "cdl_number": "CDL-1", "status": "active", "nested": {"a": [1, 2]},
    })
    async with session_scope() as s:
        doc = await HybridReadRepository("driver").get(s, TENANT, "drv_1")
    assert doc["full_name"] == "Jane"
    assert doc["nested"] == {"a": [1, 2]}


async def test_hybrid_get_tenant_isolation(engine, read_from_pg):
    await _seed("depot", {"depot_id": "depot_1", "tenant_id": TENANT, "name": "N"})
    async with session_scope() as s:
        assert await HybridReadRepository("depot").get(s, "other", "depot_1") is None
        assert await HybridReadRepository("depot").get(s, TENANT, "depot_1") is not None


async def test_hybrid_list_filter_and_paginate(engine, read_from_pg):
    for i in range(3):
        await _seed("terminal", {
            "terminal_id": f"term_{i}", "tenant_id": TENANT, "status": "active",
            "name": f"T{i}",
        })
    await _seed("terminal", {
        "terminal_id": "term_x", "tenant_id": TENANT, "status": "inactive", "name": "X",
    })
    async with session_scope() as s:
        active = await HybridReadRepository("terminal").list(
            s, TENANT, filters={"status": "active"}
        )
    assert {it["terminal_id"] for it in active["items"]} == {"term_0", "term_1", "term_2"}

    # Pagination is contiguous.
    async with session_scope() as s:
        p1 = await HybridReadRepository("terminal").list(s, TENANT, limit=1)
        assert p1["next_cursor"] is not None
        p2 = await HybridReadRepository("terminal").list(
            s, TENANT, limit=1, cursor=p1["next_cursor"]
        )
    assert p1["items"][0]["terminal_id"] != p2["items"][0]["terminal_id"]


async def test_truck_tenant_optional_get(engine, read_from_pg):
    # trucks may carry no tenant_id (legacy generic-ES path).
    await _seed("truck", {"asset_id": "TRUCK-1", "status": "active"}, doc_id="TRUCK-1")
    async with session_scope() as s:
        doc = await HybridReadRepository("truck").get(s, "anything", "TRUCK-1")
    assert doc is not None
    assert doc["asset_id"] == "TRUCK-1"


# ---------------------------------------------------------------------------
# Service-level wiring
# ---------------------------------------------------------------------------


async def test_driver_service_get_served_from_postgres(engine, read_from_pg):
    await _seed("driver", {
        "driver_id": "drv_svc", "tenant_id": TENANT, "full_name": "Bob",
        "cdl_number": "CDL-2", "status": "active",
    })
    from compliance.services.driver_qualification_service import (
        DriverQualificationService,
    )
    service = DriverQualificationService(_es_raises_on_read())
    doc = await service.get(TENANT, "drv_svc")
    assert doc["full_name"] == "Bob"

    listing = await service.list(TENANT)
    assert any(d["driver_id"] == "drv_svc" for d in listing["items"])


async def test_depot_repo_get_served_from_postgres(engine, read_from_pg):
    await _seed("depot", {
        "depot_id": "depot_svc", "tenant_id": TENANT, "name": "Depot Svc",
        "status": "active", "is_default": True, "fuel_types_supported": ["DIESEL_2"],
        "address": "1 St", "location_lat": 40.0, "location_lon": -75.0,
        "timezone": "America/New_York",
    })
    from fuel.depot_models import DepotRepository
    repo = DepotRepository(_es_raises_on_read())
    depot = await repo.get(TENANT, "depot_svc")
    assert depot is not None
    assert depot.depot_id == "depot_svc"


async def test_reads_fall_through_to_es_when_flag_off(engine, monkeypatch):
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "false")
    clear_settings_cache()

    es = AsyncMock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [{"_source": {"driver_id": "from_es",
                                                     "full_name": "ES"}}]}}
    )
    from compliance.services.driver_qualification_service import (
        DriverQualificationService,
    )
    service = DriverQualificationService(es)
    doc = await service.get(TENANT, "from_es")
    assert doc["full_name"] == "ES"
    es.search_documents.assert_awaited()
    clear_settings_cache()


# ---------------------------------------------------------------------------
# Orders / jobs current-state get reads
# ---------------------------------------------------------------------------


async def test_fuel_order_get_served_from_postgres(engine, read_from_pg):
    await _seed("fuel_order", {
        "order_id": "ord_r", "tenant_id": TENANT, "customer_id": "cust_1",
        "customer_name": "Acme", "ship_to_address": "1 St",
        "ship_to_lat": 40.0, "ship_to_lon": -75.0, "call_type": "will_call",
        "fill_to_full": True, "product_code": "DIESEL_2",
        "intake_channel": "dispatcher", "intake_channel_id": "ch_1",
        "status": "placed", "source_schema_version": "1.0", "trace_id": "t1",
        "last_event_timestamp": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    from fuel.order_repository import FuelOrderRepository
    repo = FuelOrderRepository(_es_raises_on_read())
    order = await repo.get(TENANT, "ord_r")
    assert order is not None
    assert order.order_id == "ord_r"
    assert order.status == "placed"


async def test_job_get_served_from_postgres(engine, read_from_pg):
    await _seed("job", {
        "job_id": "job_r", "tenant_id": TENANT, "job_type": "cargo_transport",
        "status": "scheduled", "asset_assigned": "TRUCK-1",
        "priority": "normal", "origin": "A", "destination": "B",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    es = _es_raises_on_read()
    # get_job also reads job_events; let that query return empty without tripping
    # the read guard (events are a different index, still on ES by design).
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})

    from scheduling.services.job_service import JobService
    # Minimal JobService — only _get_job_doc + get_job_events are exercised.
    service = JobService(es)
    doc = await service._get_job_doc("job_r", TENANT)
    assert doc["job_id"] == "job_r"
    assert doc["status"] == "scheduled"


async def test_price_protection_get_served_from_postgres(engine, read_from_pg):
    await _seed("price_protection_contract", {
        "contract_id": "ppc_r", "tenant_id": TENANT, "customer_id": "cust_1",
        "product_code": "DIESEL_2", "contract_type": "fixed_price",
        "contracted_gallons": 1000.0, "remaining_gallons": 1000.0,
        "fixed_price_cents": 300, "status": "active", "version": 0,
    })
    from commerce.api import price_protection_endpoints as ppe
    doc = await ppe._fetch_contract_or_404("ppc_r", TENANT)
    assert doc["contract_id"] == "ppc_r"
    # Missing id raises 404 from the PG path.
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await ppe._fetch_contract_or_404("ppc_missing", TENANT)
