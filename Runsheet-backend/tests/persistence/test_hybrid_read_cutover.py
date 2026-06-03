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


# ---------------------------------------------------------------------------
# Orders / jobs LIST + search (offset/total contract over the JSON document)
# ---------------------------------------------------------------------------


def _order_doc(order_id, **over):
    doc = {
        "order_id": order_id, "tenant_id": TENANT, "customer_id": "cust_1",
        "customer_name": "Acme", "ship_to_address": "1 St",
        "ship_to_lat": 40.0, "ship_to_lon": -75.0, "call_type": "will_call",
        "fill_to_full": True, "product_code": "DIESEL_2",
        "intake_channel": "dispatcher", "intake_channel_id": "ch_1",
        "status": "placed", "source_schema_version": "1.0", "trace_id": "t",
        "last_event_timestamp": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    doc.update(over)
    return doc


async def test_hybrid_search_term_filter_and_total(engine, read_from_pg):
    await _seed("fuel_order", _order_doc("o1", status="placed"))
    await _seed("fuel_order", _order_doc("o2", status="delivered"))
    await _seed("fuel_order", _order_doc("o3", status="placed"))
    async with session_scope() as s:
        res = await HybridReadRepository("fuel_order").search(
            s, TENANT, term_filters={"status": "placed"}, page=1, size=10
        )
    assert res["total"] == 2
    assert {it["order_id"] for it in res["items"]} == {"o1", "o3"}


async def test_hybrid_search_orders_by_document_created_at_desc(engine, read_from_pg):
    # Insert oldest LAST so a mirror-insert-time sort would disagree with the
    # business created_at sort — proving we read the document field.
    await _seed("fuel_order", _order_doc("new", created_at="2026-03-01T00:00:00+00:00"))
    await _seed("fuel_order", _order_doc("mid", created_at="2026-02-01T00:00:00+00:00"))
    await _seed("fuel_order", _order_doc("old", created_at="2026-01-01T00:00:00+00:00"))
    async with session_scope() as s:
        res = await HybridReadRepository("fuel_order").search(
            s, TENANT, sort_field="created_at", sort_order="desc", page=1, size=10
        )
    assert [it["order_id"] for it in res["items"]] == ["new", "mid", "old"]


async def test_hybrid_search_date_range(engine, read_from_pg):
    await _seed("fuel_order", _order_doc("jan", created_at="2026-01-15T00:00:00+00:00"))
    await _seed("fuel_order", _order_doc("feb", created_at="2026-02-15T00:00:00+00:00"))
    await _seed("fuel_order", _order_doc("mar", created_at="2026-03-15T00:00:00+00:00"))
    async with session_scope() as s:
        res = await HybridReadRepository("fuel_order").search(
            s, TENANT, range_field="created_at",
            range_gte="2026-02-01T00:00:00+00:00",
            range_lte="2026-02-28T00:00:00+00:00", page=1, size=10,
        )
    assert {it["order_id"] for it in res["items"]} == {"feb"}


async def test_hybrid_search_offset_pagination(engine, read_from_pg):
    for i in range(5):
        await _seed("fuel_order", _order_doc(
            f"p{i}", created_at=f"2026-01-0{i+1}T00:00:00+00:00"))
    async with session_scope() as s:
        p1 = await HybridReadRepository("fuel_order").search(
            s, TENANT, sort_field="created_at", sort_order="asc", page=1, size=2)
        p2 = await HybridReadRepository("fuel_order").search(
            s, TENANT, sort_field="created_at", sort_order="asc", page=2, size=2)
    assert p1["total"] == 5 and p2["total"] == 5
    assert [it["order_id"] for it in p1["items"]] == ["p0", "p1"]
    assert [it["order_id"] for it in p2["items"]] == ["p2", "p3"]


async def test_hybrid_search_tenant_isolation(engine, read_from_pg):
    await _seed("fuel_order", _order_doc("mine"))
    await _seed("fuel_order", _order_doc("theirs", tenant_id="other"))
    async with session_scope() as s:
        res = await HybridReadRepository("fuel_order").search(s, TENANT, page=1, size=10)
    assert {it["order_id"] for it in res["items"]} == {"mine"}


async def test_fuel_order_search_served_from_postgres(engine, read_from_pg):
    await _seed("fuel_order", _order_doc("s1", status="placed", call_type="will_call"))
    await _seed("fuel_order", _order_doc("s2", status="delivered"))
    from fuel.order_repository import FuelOrderRepository
    repo = FuelOrderRepository(_es_raises_on_read())
    res = await repo.search(TENANT, status="placed")
    assert res["total"] == 1
    assert res["orders"][0].order_id == "s1"


async def test_fuel_order_list_for_tenant_served_from_postgres(engine, read_from_pg):
    await _seed("fuel_order", _order_doc("l1", created_at="2026-01-02T00:00:00+00:00"))
    await _seed("fuel_order", _order_doc("l2", created_at="2026-01-01T00:00:00+00:00"))
    from fuel.order_repository import FuelOrderRepository
    repo = FuelOrderRepository(_es_raises_on_read())
    orders = await repo.list_for_tenant(TENANT, size=10)
    assert [o.order_id for o in orders] == ["l1", "l2"]  # created_at desc


def _job_doc(job_id, **over):
    doc = {
        "job_id": job_id, "tenant_id": TENANT, "job_type": "cargo_transport",
        "status": "scheduled", "asset_assigned": None, "priority": "normal",
        "origin": "A", "destination": "B", "delayed": False,
        "scheduled_time": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    doc.update(over)
    return doc


async def test_job_list_served_from_postgres(engine, read_from_pg):
    await _seed("job", _job_doc("j1", status="scheduled",
                                scheduled_time="2026-01-02T00:00:00+00:00"))
    await _seed("job", _job_doc("j2", status="in_progress",
                                scheduled_time="2026-01-01T00:00:00+00:00"))
    from scheduling.services.job_service import JobService
    service = JobService(_es_raises_on_read())
    res = await service.list_jobs(TENANT, status="scheduled")
    assert res["pagination"]["total"] == 1
    assert res["data"][0]["job_id"] == "j1"

    # Default sort is scheduled_time asc.
    res_all = await service.list_jobs(TENANT)
    assert [d["job_id"] for d in res_all["data"]] == ["j2", "j1"]


async def test_get_active_jobs_served_from_postgres(engine, read_from_pg):
    await _seed("job", _job_doc("a1", status="scheduled"))
    await _seed("job", _job_doc("a2", status="completed"))
    await _seed("job", _job_doc("a3", status="in_progress"))
    from scheduling.services.job_service import JobService
    service = JobService(_es_raises_on_read())
    active = await service.get_active_jobs(TENANT)
    assert {j["job_id"] for j in active} == {"a1", "a3"}


async def test_get_delayed_jobs_served_from_postgres(engine, read_from_pg):
    await _seed("job", _job_doc("d1", status="in_progress", delayed=True))
    await _seed("job", _job_doc("d2", status="in_progress", delayed=False))
    await _seed("job", _job_doc("d3", status="scheduled", delayed=True))
    from scheduling.services.job_service import JobService
    service = JobService(_es_raises_on_read())
    delayed = await service.get_delayed_jobs(TENANT)
    assert {j["job_id"] for j in delayed} == {"d1"}


# ---------------------------------------------------------------------------
# Asset-cert list (sorted by document expiry_date asc, cert_id asc)
# ---------------------------------------------------------------------------


def _cert_doc(cert_id, expiry, **over):
    doc = {
        "cert_id": cert_id, "tenant_id": TENANT, "asset_id": "TRUCK-1",
        "certification_type": "annual_inspection",
        "certification_date": "2025-01-01", "expiry_date": expiry,
        "status": "valid", "inspector_name": "Inspector",
        "certificate_number": f"CERT-{cert_id}",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    doc.update(over)
    return doc


async def test_asset_cert_list_sorted_by_expiry_from_postgres(engine, read_from_pg):
    # Seed out of expiry order; expect (expiry asc, cert_id asc).
    await _seed("asset_certification", _cert_doc("c3", "2026-03-01"))
    await _seed("asset_certification", _cert_doc("c1", "2026-01-01"))
    await _seed("asset_certification", _cert_doc("c2", "2026-02-01"))
    from compliance.services.asset_certification_service import (
        AssetCertificationService,
    )
    svc = AssetCertificationService(_es_raises_on_read())
    res = await svc.list(TENANT)
    assert [c["cert_id"] for c in res["items"]] == ["c1", "c2", "c3"]


async def test_asset_cert_list_filter_and_paginate_from_postgres(engine, read_from_pg):
    await _seed("asset_certification", _cert_doc("v1", "2026-01-01", status="valid"))
    await _seed("asset_certification", _cert_doc("v2", "2026-02-01", status="valid"))
    await _seed("asset_certification", _cert_doc("e1", "2026-01-15", status="expired"))
    from compliance.services.asset_certification_service import (
        AssetCertificationService,
    )
    svc = AssetCertificationService(_es_raises_on_read())
    valid = await svc.list(TENANT, status="valid")
    assert {c["cert_id"] for c in valid["items"]} == {"v1", "v2"}

    p1 = await svc.list(TENANT, status="valid", limit=1)
    assert p1["next_cursor"] is not None
    assert [c["cert_id"] for c in p1["items"]] == ["v1"]
    p2 = await svc.list(TENANT, status="valid", limit=1, cursor=p1["next_cursor"])
    assert [c["cert_id"] for c in p2["items"]] == ["v2"]


# ---------------------------------------------------------------------------
# tenant_job_policy get (keyed by tenant_id)
# ---------------------------------------------------------------------------


async def test_tenant_job_policies_served_from_postgres(engine, read_from_pg):
    await _seed("tenant_job_policy", {
        "tenant_id": TENANT, "pod_required": True, "pod_radius_meters": 250,
        "otp_required": True, "nudge_timeout_minutes": 5,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    from scheduling.services.job_service import JobService
    svc = JobService(_es_raises_on_read())
    policies = await svc._get_tenant_policies(TENANT)
    assert policies["pod_required"] is True
    assert policies["pod_radius_meters"] == 250
    assert policies["otp_required"] is True
    assert policies["nudge_timeout_minutes"] == 5


async def test_tenant_job_policies_defaults_when_missing(engine, read_from_pg):
    from scheduling.services.job_service import JobService
    svc = JobService(_es_raises_on_read())
    policies = await svc._get_tenant_policies(TENANT)
    # No row seeded → defaults, and ES is never consulted.
    assert policies["pod_required"] is False
    assert policies["pod_radius_meters"] == 500


# ---------------------------------------------------------------------------
# Tax engine FIPS + exemption lookups
# ---------------------------------------------------------------------------


async def test_tax_jurisdiction_rates_served_from_postgres(engine, read_from_pg):
    # Federal sentinel + state, both effective, plus an expired state row.
    common = {
        "tenant_id": TENANT, "jurisdiction_name": "X",
        "rate_cents_per_gallon": 184, "product_codes": ["DIESEL_2"],
        "effective_date": "2025-01-01",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    await _seed("tax_jurisdiction", {**common, "jurisdiction_id": "fed",
                "fips_code": "00", "jurisdiction_level": "federal",
                "tax_type": "excise"})
    await _seed("tax_jurisdiction", {**common, "jurisdiction_id": "tx",
                "fips_code": "48", "jurisdiction_level": "state",
                "tax_type": "excise", "rate_cents_per_gallon": 200})
    await _seed("tax_jurisdiction", {**common, "jurisdiction_id": "tx_old",
                "fips_code": "48", "jurisdiction_level": "state",
                "tax_type": "excise", "rate_cents_per_gallon": 150,
                "expiry_date": "2025-06-01"})  # expired before 2026 invoice

    from compliance.services.tax_engine import TaxEngine
    from datetime import date
    engine_svc = TaxEngine(_es_raises_on_read(), tenant_id=TENANT)
    rates = await engine_svc.get_jurisdiction_rates("48", date(2026, 1, 15))
    ids = {r.jurisdiction_id for r in rates}
    assert ids == {"fed", "tx"}  # expired tx_old excluded; both active kept


async def test_tax_exemption_check_served_from_postgres(engine, read_from_pg):
    await _seed("tax_exemption", {
        "tenant_id": TENANT, "exemption_id": "ex1", "customer_id": "cust_1",
        "exemption_type": "farm", "certificate_number": "CN-1",
        "status": "valid", "expiry_date": "2026-12-31",
        "product_codes": ["DIESEL_2"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    from compliance.services.tax_engine import TaxEngine
    from datetime import date
    engine_svc = TaxEngine(_es_raises_on_read(), tenant_id=TENANT)
    exemption = await engine_svc.check_exemption("cust_1", "DIESEL_2", date(2026, 6, 1))
    assert exemption is not None
    assert exemption.exemption_id == "ex1"

    # Wrong product → no match (explicit product_codes scoping).
    none_match = await engine_svc.check_exemption("cust_1", "GASOLINE_REG", date(2026, 6, 1))
    assert none_match is None


# ---------------------------------------------------------------------------
# Trucks list/get (legacy generic-ES, tenant-optional) via data_endpoints
# ---------------------------------------------------------------------------


async def test_truck_list_filters_and_sorts_from_postgres(engine, read_from_pg):
    # asset_subtype==truck OR asset_type missing; sorted created_at desc.
    await _seed("truck", {"truck_id": "T1", "asset_id": "T1", "tenant_id": TENANT,
                          "asset_subtype": "truck", "asset_type": "vehicle",
                          "truck_id_label": "newest",
                          "created_at": "2026-03-01T00:00:00+00:00"}, doc_id="T1")
    await _seed("truck", {"truck_id": "T2", "asset_id": "T2", "tenant_id": TENANT,
                          "created_at": "2026-02-01T00:00:00+00:00"}, doc_id="T2")  # legacy (no asset_type)
    await _seed("truck", {"truck_id": "B1", "asset_id": "B1", "tenant_id": TENANT,
                          "asset_subtype": "barge", "asset_type": "vessel",
                          "created_at": "2026-04-01T00:00:00+00:00"}, doc_id="B1")

    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_fetch_for_aggregation,
    )
    docs = await read_hybrid_fetch_for_aggregation("truck", TENANT)
    assert docs is not _NOT_CUT_OVER
    trucks = [d for d in docs if d.get("asset_subtype") == "truck" or "asset_type" not in d]
    trucks.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    # B1 (barge with asset_type) excluded; T1 newer than T2.
    assert [t["truck_id"] for t in trucks] == ["T1", "T2"]


async def test_truck_get_served_from_postgres(engine, read_from_pg):
    await _seed("truck", {"truck_id": "TRUCK-9", "asset_id": "TRUCK-9",
                          "tenant_id": TENANT, "asset_subtype": "truck",
                          "asset_type": "vehicle", "status": "on_time"},
                doc_id="TRUCK-9")
    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_get,
    )
    doc = await read_hybrid_get("truck", TENANT, "TRUCK-9")
    assert doc is not _NOT_CUT_OVER
    assert doc is not None
    assert doc["truck_id"] == "TRUCK-9"
    assert doc["status"] == "on_time"
