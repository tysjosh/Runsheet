"""Read-cutover tests for the hybrid aggregates (master data + config).

With ``COMMERCE_READ_FROM_POSTGRES`` on, the master-data / config services
serve get/list from the Postgres ``document`` column (byte-identical) and the
ES client is NOT queried for those reads. With the flag off they fall through
to ES.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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


async def test_hybrid_search_text_query_contains(engine, read_from_pg):
    # Free-text "contains" search ORs ILIKE %q% across the named doc fields,
    # case-insensitively, and stays tenant-scoped.
    await _seed("fuel_order", {
        "order_id": "ORD-AAA", "tenant_id": TENANT, "status": "placed",
        "customer_name": "Acme Fuels", "customer_id": "CUST-1",
        "ship_to_address": "123 Main St", "created_at": "2026-01-01T00:00:00Z",
    }, doc_id="ORD-AAA")
    await _seed("fuel_order", {
        "order_id": "ORD-BBB", "tenant_id": TENANT, "status": "placed",
        "customer_name": "Beta Propane", "customer_id": "CUST-2",
        "ship_to_address": "9 Oak Ave", "created_at": "2026-01-02T00:00:00Z",
    }, doc_id="ORD-BBB")

    text_fields = ["order_id", "customer_name", "customer_id", "ship_to_address"]
    async with session_scope() as s:
        repo = HybridReadRepository("fuel_order")
        # Match on customer_name, case-insensitive.
        by_name = await repo.search(
            s, TENANT, text_query="acme", text_fields=text_fields
        )
        # Match on ship_to_address.
        by_addr = await repo.search(
            s, TENANT, text_query="oak", text_fields=text_fields
        )
        # No match.
        none = await repo.search(
            s, TENANT, text_query="zzz", text_fields=text_fields
        )

    assert {it["order_id"] for it in by_name["items"]} == {"ORD-AAA"}
    assert {it["order_id"] for it in by_addr["items"]} == {"ORD-BBB"}
    assert none["items"] == []


async def test_hybrid_search_text_query_escapes_like_wildcards(engine, read_from_pg):
    # A literal % in a doc value is only matched by a query containing % —
    # the LIKE metacharacters in the user input are escaped.
    await _seed("fuel_order", {
        "order_id": "ORD-PCT", "tenant_id": TENANT, "status": "placed",
        "customer_name": "50% Off Fuels", "customer_id": "CUST-9",
        "ship_to_address": "1 A St", "created_at": "2026-01-03T00:00:00Z",
    }, doc_id="ORD-PCT")
    await _seed("fuel_order", {
        "order_id": "ORD-PLAIN", "tenant_id": TENANT, "status": "placed",
        "customer_name": "Plain Fuels", "customer_id": "CUST-8",
        "ship_to_address": "2 B St", "created_at": "2026-01-04T00:00:00Z",
    }, doc_id="ORD-PLAIN")

    text_fields = ["customer_name"]
    async with session_scope() as s:
        repo = HybridReadRepository("fuel_order")
        literal = await repo.search(
            s, TENANT, text_query="50%", text_fields=text_fields
        )
    # "50%" matches only the doc literally containing "50%", not everything.
    assert {it["order_id"] for it in literal["items"]} == {"ORD-PCT"}


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


async def test_fuel_order_search_for_driver_served_from_postgres(engine, read_from_pg):
    """Driver work read: status terms + window sort + driver scoping."""
    await _seed("fuel_order", _order_doc(
        "d2", status="in_transit", assigned_driver_id="drv_1",
        delivery_window_start="2026-01-02T00:00:00+00:00"))
    await _seed("fuel_order", _order_doc(
        "d1", status="dispatched", assigned_driver_id="drv_1",
        delivery_window_start="2026-01-01T00:00:00+00:00"))
    # Excluded: another driver, and a status outside the default set.
    await _seed("fuel_order", _order_doc(
        "other_drv", status="dispatched", assigned_driver_id="drv_2",
        delivery_window_start="2026-01-01T00:00:00+00:00"))
    await _seed("fuel_order", _order_doc(
        "done", status="delivered", assigned_driver_id="drv_1",
        delivery_window_start="2026-01-01T00:00:00+00:00"))

    from fuel.order_repository import FuelOrderRepository
    repo = FuelOrderRepository(_es_raises_on_read())
    res = await repo.search_for_driver(TENANT, "drv_1")

    assert res["total"] == 2
    # delivery_window_start ascending
    assert [o.order_id for o in res["orders"]] == ["d1", "d2"]


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

# ---------------------------------------------------------------------------
# Price book / pricing rule (commerce, typed-column models)
# ---------------------------------------------------------------------------


async def _seed_price_book(price_book_id, *, status="active", name="PB",
                           tenant_id=TENANT):
    from persistence.repositories import PriceBookRepository
    async with session_scope() as s:
        await PriceBookRepository().create(
            s, price_book_id=price_book_id, tenant_id=tenant_id, name=name,
            status=status, rule_count=0,
        )


async def _seed_pricing_rule(rule_id, price_book_id, *, product_code="DIESEL_2",
                             tenant_id=TENANT):
    from persistence.repositories import PricingRuleRepository
    async with session_scope() as s:
        await PricingRuleRepository().upsert(s, rule={
            "rule_id": rule_id, "price_book_id": price_book_id, "tenant_id": tenant_id,
            "product_code": product_code, "scope_type": "all", "scope_value": "*",
            "effective_from": None, "effective_to": None,
            "min_quantity_gallons": None, "unit_price_cents": 350,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })


async def test_price_book_service_get_served_from_postgres(engine, read_from_pg):
    await _seed_price_book("pb_svc", name="Spring Book")
    await _seed_pricing_rule("rule_a", "pb_svc")

    from commerce.services.price_book_service import PriceBookService
    service = PriceBookService(_es_raises_on_read())
    book = await service.get(TENANT, "pb_svc")
    assert book["price_book_id"] == "pb_svc"
    assert book["name"] == "Spring Book"
    # Rules are embedded, also from Postgres.
    assert [r["rule_id"] for r in book["rules"]] == ["rule_a"]


async def test_price_book_service_get_missing_raises(engine, read_from_pg):
    from commerce.services.price_book_service import PriceBookService
    from errors.exceptions import AppException
    service = PriceBookService(_es_raises_on_read())
    with pytest.raises(AppException):
        await service.get(TENANT, "does_not_exist")


async def test_price_book_service_get_tenant_isolation(engine, read_from_pg):
    await _seed_price_book("pb_other", tenant_id="other-tenant")
    from commerce.services.price_book_service import PriceBookService
    from errors.exceptions import AppException
    service = PriceBookService(_es_raises_on_read())
    with pytest.raises(AppException):
        await service.get(TENANT, "pb_other")


async def test_price_book_service_list_served_from_postgres(engine, read_from_pg):
    await _seed_price_book("pb_1", status="active")
    await _seed_price_book("pb_2", status="active")
    await _seed_price_book("pb_draft", status="draft")

    from commerce.services.price_book_service import PriceBookService
    service = PriceBookService(_es_raises_on_read())

    all_books = await service.list(TENANT)
    assert {b["price_book_id"] for b in all_books["items"]} >= {"pb_1", "pb_2", "pb_draft"}

    active = await service.list(TENANT, status="active")
    assert {b["price_book_id"] for b in active["items"]} == {"pb_1", "pb_2"}


async def test_price_book_list_pagination_contiguous(engine, read_from_pg):
    for i in range(3):
        await _seed_price_book(f"pbp_{i}", status="active")
    from commerce.services.price_book_service import PriceBookService
    service = PriceBookService(_es_raises_on_read())
    p1 = await service.list(TENANT, limit=1)
    assert p1["next_cursor"] is not None
    p2 = await service.list(TENANT, limit=1, cursor=p1["next_cursor"])
    assert p1["items"][0]["price_book_id"] != p2["items"][0]["price_book_id"]


# ---------------------------------------------------------------------------
# PricingEngine (commerce pricing_rules_current candidate set)
# ---------------------------------------------------------------------------


async def _seed_pricing_rule_full(rule_id, price_book_id, *, product_code="DIESEL_2",
                                  scope_type="default", scope_value="default",
                                  unit_price_cents=350, effective_from=None,
                                  effective_to=None, min_quantity_gallons=None,
                                  tenant_id=TENANT):
    from persistence.repositories import PricingRuleRepository
    async with session_scope() as s:
        await PricingRuleRepository().upsert(s, rule={
            "rule_id": rule_id, "price_book_id": price_book_id, "tenant_id": tenant_id,
            "product_code": product_code, "scope_type": scope_type,
            "scope_value": scope_value,
            "effective_from": effective_from or "2026-01-01T00:00:00+00:00",
            "effective_to": effective_to,
            "min_quantity_gallons": min_quantity_gallons,
            "unit_price_cents": unit_price_cents,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })


async def test_pricing_engine_resolves_from_postgres(engine, read_from_pg):
    from datetime import datetime

    from commerce.models.account import Account, AccountTier
    from commerce.services.pricing_engine import PricingEngine

    await _seed_price_book("pb_eng", status="active")
    # Default rule + an account-scoped rule that should win on precedence.
    await _seed_pricing_rule_full("rule_default", "pb_eng", scope_type="default",
                                  scope_value="default", unit_price_cents=350)
    await _seed_pricing_rule_full("rule_acct", "pb_eng", scope_type="account",
                                  scope_value="acct_1", unit_price_cents=300)

    account = Account(account_id="acct_1", tenant_id=TENANT, customer_id="cust_1",
                      display_name="Acme", tier=AccountTier.GOLD)
    # No Redis → every resolve hits the candidate fetch, which serves from PG.
    # NOTE: naive ``moment`` — SQLite's DateTime drops the tz info the seeded
    # effective_from carried (PostgreSQL's DateTime(timezone=True) preserves it
    # in production), so a naive moment keeps the effective-window comparison
    # well-typed in the test without changing the production code path.
    pricing = PricingEngine(_es_raises_on_read(), redis_client=None)
    result = await pricing.resolve(
        tenant_id=TENANT, account=account, product_code="DIESEL_2",
        moment=datetime(2026, 6, 1), quantity_gallons=500.0,
    )
    assert result.rule_id == "rule_acct"
    assert result.unit_price_cents == 300
    assert result.matched_from_cache is False


async def test_pricing_engine_product_scoping_from_postgres(engine, read_from_pg):
    from datetime import datetime

    from commerce.models.account import Account, AccountTier
    from commerce.services.pricing_engine import PricingEngine, PricingError

    await _seed_price_book("pb_eng2", status="active")
    await _seed_pricing_rule_full("rule_diesel", "pb_eng2", product_code="DIESEL_2",
                                  scope_type="default", scope_value="default")
    account = Account(account_id="acct_9", tenant_id=TENANT, customer_id="cust_9",
                      display_name="Beta", tier=AccountTier.DEFAULT)
    pricing = PricingEngine(_es_raises_on_read(), redis_client=None)
    # A different product has no rules → no_rule_matched, and ES is never queried.
    with pytest.raises(PricingError):
        await pricing.resolve(
            tenant_id=TENANT, account=account, product_code="GASOLINE_REG",
            moment=datetime(2026, 6, 1), quantity_gallons=100.0,
        )


# ---------------------------------------------------------------------------
# SalesPricingEngine (compliance pricing_rules) + PriceProtectionService
# (price_protection_contracts) read-cutover
# ---------------------------------------------------------------------------


async def test_sales_pricing_engine_resolve_rule_from_postgres(engine, read_from_pg):
    from datetime import date

    from commerce.services.sales_pricing_engine import SalesPricingEngine

    await _seed("compliance_pricing_rule", {
        "rule_id": "spr_1", "tenant_id": TENANT, "customer_id": "cust_1",
        "account_id": None, "product_code": "DIESEL_2", "status": "active",
        "strategy": "posted_price", "posted_price_cents": 325, "priority": 10,
        "effective_date": "2026-01-01", "expiry_date": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    svc = SalesPricingEngine(_es_raises_on_read(), tenant_id=TENANT)
    rule = await svc.resolve_rule(
        customer_id="cust_1", account_id=None, product_code="DIESEL_2",
        effective_date=date(2026, 6, 1),
    )
    assert rule is not None
    assert rule.rule_id == "spr_1"
    assert rule.posted_price_cents == 325


async def test_sales_pricing_engine_expired_rule_excluded_from_postgres(engine, read_from_pg):
    from datetime import date

    from commerce.services.sales_pricing_engine import SalesPricingEngine

    await _seed("compliance_pricing_rule", {
        "rule_id": "spr_exp", "tenant_id": TENANT, "customer_id": "cust_2",
        "account_id": None, "product_code": "DIESEL_2", "status": "active",
        "strategy": "posted_price", "posted_price_cents": 300, "priority": 10,
        "effective_date": "2026-01-01", "expiry_date": "2026-03-01",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    svc = SalesPricingEngine(_es_raises_on_read(), tenant_id=TENANT)
    # effective_date after expiry → client-side window re-check drops it.
    rule = await svc.resolve_rule(
        customer_id="cust_2", account_id=None, product_code="DIESEL_2",
        effective_date=date(2026, 6, 1),
    )
    assert rule is None


def _ppc_doc(contract_id, **over):
    doc = {
        "contract_id": contract_id, "tenant_id": TENANT, "customer_id": "cust_1",
        "account_id": "acct_1", "product_code": "DIESEL_2",
        "contract_type": "fixed_price",
        "contracted_gallons": 1000.0, "remaining_gallons": 1000.0,
        "fixed_price_cents": 300, "status": "active", "version": 0,
        "start_date": "2026-01-01", "end_date": "2026-12-31",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    doc.update(over)
    return doc


async def test_price_protection_find_active_contract_from_postgres(engine, read_from_pg):
    from datetime import date

    from commerce.services.price_protection_service import PriceProtectionService

    await _seed("price_protection_contract", _ppc_doc("ppc_find"))
    svc = PriceProtectionService(_es_raises_on_read(), tenant_id=TENANT)
    contract = await svc.find_active_contract("cust_1", "DIESEL_2", date(2026, 6, 1))
    assert contract is not None
    assert contract.contract_id == "ppc_find"


async def test_price_protection_window_filter_from_postgres(engine, read_from_pg):
    from datetime import date

    from commerce.services.price_protection_service import PriceProtectionService

    # Active status but the effective date is outside [start_date, end_date].
    await _seed("price_protection_contract", _ppc_doc(
        "ppc_window", start_date="2026-01-01", end_date="2026-03-01"))
    svc = PriceProtectionService(_es_raises_on_read(), tenant_id=TENANT)
    contract = await svc.find_active_contract("cust_1", "DIESEL_2", date(2026, 6, 1))
    assert contract is None


async def test_price_protection_resolve_price_from_postgres(engine, read_from_pg):
    from datetime import date

    from commerce.services.price_protection_service import PriceProtectionService

    await _seed("price_protection_contract", _ppc_doc(
        "ppc_resolve", contract_type="fixed_price", fixed_price_cents=280))
    svc = PriceProtectionService(_es_raises_on_read(), tenant_id=TENANT)
    resolution = await svc.resolve_price(
        customer_id="cust_1", product_code="DIESEL_2", market_price_cents=400,
        gallons=100.0, effective_date=date(2026, 6, 1),
    )
    assert resolution.contract_id == "ppc_resolve"
    assert resolution.effective_price_cents == 280


async def test_price_protection_check_expiry_from_postgres(engine, read_from_pg):
    from datetime import date

    from commerce.services.price_protection_service import PriceProtectionService

    # One contract past its end_date (should transition to expired), one current.
    es = _es_raises_on_read()  # update_document is allowed; search_documents is not
    await _seed("price_protection_contract", _ppc_doc(
        "ppc_old", end_date="2026-03-01"))
    await _seed("price_protection_contract", _ppc_doc(
        "ppc_current", end_date="2026-12-31"))
    svc = PriceProtectionService(es, tenant_id=TENANT)
    transitioned = await svc.check_expiry(today=date(2026, 6, 1))
    assert transitioned == ["ppc_old"]
    # The status transition was written via update_document (ES write path).
    es.update_document.assert_awaited()


async def test_supplier_contract_get_served_from_postgres(engine, read_from_pg):
    await _seed("supplier_contract", {
        "contract_id": "sc_get", "tenant_id": TENANT, "supplier_name": "Marathon",
        "product_code": "DIESEL_2", "preferred_terminal_ids": ["TERM-001"],
        "minimum_lift_gallons_per_month": 10000.0,
        "contract_price_per_gallon_usd": 3.18, "branded_required": True,
        "status": "active", "effective_from": "2026-01-01",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    from fuel.terminal_models import SupplierContractRepository
    repo = SupplierContractRepository(_es_raises_on_read())
    contract = await repo.get(TENANT, "sc_get")
    assert contract is not None
    assert contract.contract_id == "sc_get"
    assert contract.supplier_name == "Marathon"


# ---------------------------------------------------------------------------
# Secondary commerce reads — AR aging / credit / dunning / background jobs
# (invoices_current + accounts_current scans, served from PG when cut over)
# ---------------------------------------------------------------------------


async def _seed_customer(customer_id, *, tenant_id=TENANT):
    from persistence.repositories import CustomerRepository
    async with session_scope() as s:
        await CustomerRepository().create(
            s, customer_id=customer_id, tenant_id=tenant_id,
            display_name=f"Cust {customer_id}",
        )


async def _seed_account(account_id, customer_id, *, tenant_id=TENANT,
                        credit_limit_cents=0, credit_state=None,
                        credit_override_expires_at=None):
    from persistence.repositories import AccountRepository
    async with session_scope() as s:
        await AccountRepository().create(
            s, account_id=account_id, tenant_id=tenant_id, customer_id=customer_id,
            display_name=f"Acct {account_id}", credit_limit_cents=credit_limit_cents,
        )
        if credit_state is not None or credit_override_expires_at is not None:
            fields = {}
            if credit_state is not None:
                fields["credit_state"] = credit_state
            if credit_override_expires_at is not None:
                fields["credit_override_expires_at"] = credit_override_expires_at
            await AccountRepository().set_fields(s, tenant_id, account_id, **fields)


async def _seed_invoice(invoice_id, account_id, customer_id, *, tenant_id=TENANT,
                        status="open", remaining_cents=10000, total_cents=10000,
                        issued_at=None, due_date=None):
    from persistence.repositories import InvoiceRepository
    repo = InvoiceRepository()
    async with session_scope() as s:
        await repo.create(
            s, invoice_id=invoice_id, tenant_id=tenant_id, customer_id=customer_id,
            account_id=account_id, line_items=[{
                "line_id": f"li_{invoice_id}", "product_code": "DIESEL_2",
                "quantity_gallons": 100.0, "unit_price_cents": 100,
                "subtotal_cents": total_cents,
            }],
            status=status, total_cents=total_cents, remaining_cents=remaining_cents,
            due_date=due_date,
        )
        fields = {"status": status}
        if issued_at is not None:
            fields["issued_at"] = issued_at
        await repo.set_fields(s, tenant_id, invoice_id, **fields)


async def test_ar_aging_account_served_from_postgres(engine, read_from_pg):
    from datetime import timedelta

    from commerce.services.ar_aging_service import ARAgingService
    from services.time_utils import utcnow

    await _seed_customer("c_ar")
    await _seed_account("a_ar", "c_ar")
    now = utcnow()
    # One invoice in 0-30 bucket, one in 90+.
    await _seed_invoice("inv_recent", "a_ar", "c_ar", status="open",
                        remaining_cents=5000,
                        issued_at=(now - timedelta(days=5)).isoformat())
    await _seed_invoice("inv_old", "a_ar", "c_ar", status="overdue",
                        remaining_cents=7000,
                        issued_at=(now - timedelta(days=120)).isoformat())

    svc = ARAgingService(_es_raises_on_read())
    aging = await svc.compute_account_aging(TENANT, "a_ar")
    assert aging["bucket_0_30_cents"] == 5000
    assert aging["bucket_90_plus_cents"] == 7000
    assert aging["total_open_cents"] == 12000


async def test_ar_aging_tenant_and_snapshot_from_postgres(engine, read_from_pg):
    from datetime import timedelta

    from commerce.services.ar_aging_service import ARAgingService
    from services.time_utils import utcnow

    await _seed_customer("c_t")
    await _seed_account("a_t1", "c_t")
    await _seed_account("a_t2", "c_t")
    now = utcnow()
    await _seed_invoice("inv_t1", "a_t1", "c_t", status="open",
                        remaining_cents=3000,
                        issued_at=(now - timedelta(days=10)).isoformat())
    await _seed_invoice("inv_t2", "a_t2", "c_t", status="partial",
                        remaining_cents=4000,
                        issued_at=(now - timedelta(days=45)).isoformat())
    # paid invoice (remaining 0) must NOT count.
    await _seed_invoice("inv_paid", "a_t1", "c_t", status="paid",
                        remaining_cents=0,
                        issued_at=(now - timedelta(days=2)).isoformat())

    svc = ARAgingService(_es_raises_on_read())
    tenant_aging = await svc.compute_tenant_aging(TENANT)
    assert tenant_aging["total_open_cents"] == 7000
    assert tenant_aging["bucket_0_30_cents"] == 3000
    assert tenant_aging["bucket_31_60_cents"] == 4000
    assert {a["account_id"] for a in tenant_aging["by_account"]} == {"a_t1", "a_t2"}

    # Snapshot writes to ES/PG (index_document allowed) and counts 2 accounts.
    es = _es_raises_on_read()
    svc2 = ARAgingService(es)
    snap = await svc2.write_daily_snapshot(TENANT)
    assert snap["total_open_cents"] == 7000
    assert snap["account_count_with_balance"] == 2
    es.index_document.assert_awaited()


async def test_credit_open_balance_served_from_postgres(engine, read_from_pg):
    from commerce.services.credit_service import CreditService

    await _seed_customer("c_cr")
    await _seed_account("a_cr", "c_cr", credit_limit_cents=100000)
    await _seed_invoice("inv_cr1", "a_cr", "c_cr", status="open",
                        remaining_cents=6000)
    await _seed_invoice("inv_cr2", "a_cr", "c_cr", status="partial",
                        remaining_cents=2500)
    await _seed_invoice("inv_cr_paid", "a_cr", "c_cr", status="paid",
                        remaining_cents=0)

    svc = CreditService(_es_raises_on_read())
    balance = await svc._compute_open_balance(TENANT, "a_cr")
    assert balance == 8500

    account = await svc._get_account(TENANT, "a_cr")
    assert account["account_id"] == "a_cr"


async def test_credit_get_account_missing_raises_from_postgres(engine, read_from_pg):
    from commerce.services.credit_service import CreditService
    from errors.exceptions import AppException

    svc = CreditService(_es_raises_on_read())
    with pytest.raises(AppException):
        await svc._get_account(TENANT, "no_such_account")


async def test_dunning_overdue_scan_served_from_postgres(engine, read_from_pg):
    from datetime import timedelta

    from commerce.services.dunning_service import DunningService
    from services.time_utils import utcnow

    await _seed_customer("c_dun")
    await _seed_account("a_dun", "c_dun")
    today = utcnow().date()
    # 40 days overdue (eligible) and 1 day overdue (below 30-day min threshold).
    await _seed_invoice("inv_due_old", "a_dun", "c_dun", status="open",
                        remaining_cents=9000, due_date=(today - timedelta(days=40)))
    await _seed_invoice("inv_due_new", "a_dun", "c_dun", status="open",
                        remaining_cents=9000, due_date=(today - timedelta(days=1)))

    svc = DunningService(_es_raises_on_read())
    cutoff = today - timedelta(days=30)
    overdue = await svc._query_overdue_invoices(TENANT, cutoff)
    assert {i["invoice_id"] for i in overdue} == {"inv_due_old"}


async def test_invoice_overdue_job_cross_tenant_from_postgres(engine, read_from_pg):
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from commerce.services.invoice_overdue_job import run_invoice_overdue_cycle
    from services.time_utils import utcnow

    today = utcnow().date()
    await _seed_customer("c_j1")
    await _seed_account("a_j1", "c_j1")
    await _seed_invoice("inv_j_due", "a_j1", "c_j1", status="open",
                        remaining_cents=5000, due_date=(today - timedelta(days=3)))
    # A second tenant's past-due invoice — the sweep is cross-tenant.
    await _seed_customer("c_j2", tenant_id="other-tenant")
    await _seed_account("a_j2", "c_j2", tenant_id="other-tenant")
    await _seed_invoice("inv_j_due2", "a_j2", "c_j2", tenant_id="other-tenant",
                        status="partial", remaining_cents=5000,
                        due_date=(today - timedelta(days=3)))
    # Not due yet — excluded.
    await _seed_invoice("inv_j_future", "a_j1", "c_j1", status="open",
                        remaining_cents=5000, due_date=(today + timedelta(days=10)))

    es = _es_raises_on_read()  # search_documents must NOT be used
    invoice_service = AsyncMock()
    invoice_service.mark_overdue = AsyncMock(return_value={})
    count = await run_invoice_overdue_cycle(es, invoice_service)
    assert count == 2
    marked = {
        call.kwargs["invoice_id"]
        for call in invoice_service.mark_overdue.await_args_list
    }
    assert marked == {"inv_j_due", "inv_j_due2"}


async def test_credit_override_expiry_job_cross_tenant_from_postgres(engine, read_from_pg):
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from commerce.models.account import CreditState
    from commerce.services.credit_override_expiry_job import (
        run_credit_override_expiry_cycle,
    )
    from services.time_utils import utcnow

    now = utcnow()
    await _seed_customer("c_ov")
    # Expired override (eligible).
    await _seed_account("a_ov_expired", "c_ov",
                        credit_state=CreditState.OVERRIDE.value,
                        credit_override_expires_at=(now - timedelta(hours=1)))
    # Future override (not yet expired).
    await _seed_account("a_ov_future", "c_ov",
                        credit_state=CreditState.OVERRIDE.value,
                        credit_override_expires_at=(now + timedelta(hours=1)))
    # Plain account (no override).
    await _seed_account("a_ov_ok", "c_ov")

    es = _es_raises_on_read()  # search_documents must NOT be used
    credit_service = AsyncMock()
    credit_service.expire_override = AsyncMock(return_value={})
    count = await run_credit_override_expiry_cycle(es, credit_service)
    assert count == 1
    expired = {
        call.kwargs["account_id"]
        for call in credit_service.expire_override.await_args_list
    }
    assert expired == {"a_ov_expired"}


# ---------------------------------------------------------------------------
# Asset-cert status transitions mirror to PG (prevents expiry-scan drift)
# ---------------------------------------------------------------------------


async def test_asset_cert_transition_mirrors_to_postgres(engine, read_from_pg, monkeypatch):
    # Dual-write must be enabled for the mirror to fire.
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    clear_settings_cache()

    await _seed("asset_certification", _cert_doc("cert_mir", "2026-02-01",
                                                 status="valid"))

    from commerce.services.commerce_persistence_bridge import (
        mirror_current_state_fields,
    )
    # Simulate the expiry sweep's status transition (ES write + PG mirror).
    await mirror_current_state_fields(
        "asset_certification", TENANT, "cert_mir",
        {"status": "expiring_soon", "updated_at": "2026-06-01T00:00:00+00:00"},
    )

    async with session_scope() as s:
        doc = await HybridReadRepository("asset_certification").get(
            s, TENANT, "cert_mir"
        )
    # Both the verbatim document and the typed column reflect the transition.
    assert doc["status"] == "expiring_soon"
    from persistence.models import AssetCertificationORM
    async with session_scope() as s:
        row = await s.get(AssetCertificationORM, "cert_mir")
        assert row.status == "expiring_soon"
    clear_settings_cache()


# ---------------------------------------------------------------------------
# Fleet dashboard reads (trucks scan + assets-alias aggregation/list/get)
# served from PG via the truck aggregate (assets is an ES alias on trucks)
# ---------------------------------------------------------------------------


async def _seed_asset(asset_id, *, asset_type=None, asset_subtype=None, status="active",
                      tenant_id=TENANT, created_at="2026-01-01T00:00:00+00:00"):
    doc = {
        "truck_id": asset_id, "asset_id": asset_id, "tenant_id": tenant_id,
        "status": status, "created_at": created_at,
    }
    if asset_type is not None:
        doc["asset_type"] = asset_type
    if asset_subtype is not None:
        doc["asset_subtype"] = asset_subtype
    await _seed("truck", doc, doc_id=asset_id)


async def test_fleet_summary_rollups_from_postgres(engine, read_from_pg):
    # Mixed asset types/subtypes + a cross-tenant row that must be excluded.
    await _seed_asset("V1", asset_type="vehicle", asset_subtype="truck",
                      status="on_time")
    await _seed_asset("V2", asset_type="vehicle", asset_subtype="truck",
                      status="delayed")
    await _seed_asset("B1", asset_type="vessel", asset_subtype="barge",
                      status="active")
    await _seed_asset("OTHER", asset_type="vehicle", asset_subtype="truck",
                      status="on_time", tenant_id="other-tenant")

    from collections import Counter

    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_fetch_for_aggregation,
    )
    docs = await read_hybrid_fetch_for_aggregation("truck", TENANT)
    assert docs is not _NOT_CUT_OVER
    # Tenant isolation: the cross-tenant row is filtered out by the endpoint.
    docs = [d for d in docs if d.get("tenant_id") == TENANT]
    assert {d["truck_id"] for d in docs} == {"V1", "V2", "B1"}

    # Truck status rollup (totalTrucks counts every doc in the index).
    assert len(docs) == 3
    assert len([d for d in docs if d.get("status") == "on_time"]) == 1
    assert len([d for d in docs if d.get("status") == "delayed"]) == 1

    # by_type / by_subtype ordering: doc_count desc then key asc.
    type_counts = Counter(d.get("asset_type") for d in docs)
    by_type = {k: c for k, c in sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))}
    assert list(by_type.items()) == [("vehicle", 2), ("vessel", 1)]


async def test_fleet_assets_filter_and_get_from_postgres(engine, read_from_pg):
    await _seed_asset("VS1", asset_type="vessel", asset_subtype="boat",
                      status="active", created_at="2026-02-01T00:00:00+00:00")
    await _seed_asset("VS2", asset_type="vessel", asset_subtype="boat",
                      status="idle", created_at="2026-03-01T00:00:00+00:00")
    await _seed_asset("EQ1", asset_type="equipment", asset_subtype="crane",
                      status="active", created_at="2026-01-01T00:00:00+00:00")

    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_fetch_for_aggregation, read_hybrid_get,
    )
    docs = await read_hybrid_fetch_for_aggregation("truck", TENANT)
    assert docs is not _NOT_CUT_OVER
    docs = [d for d in docs if d.get("tenant_id") == TENANT]

    # Filter by asset_type=vessel, sort created_at desc (the endpoint's logic).
    vessels = [d for d in docs if d.get("asset_type") == "vessel"]
    vessels.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    assert [d["asset_id"] for d in vessels] == ["VS2", "VS1"]

    # Get by id from PG.
    one = await read_hybrid_get("truck", TENANT, "EQ1")
    assert one is not _NOT_CUT_OVER
    assert one["asset_subtype"] == "crane"


# ---------------------------------------------------------------------------
# Tax endpoint list handlers (tax-jurisdictions + exemptions) served from PG
# ---------------------------------------------------------------------------


async def test_tax_jurisdiction_list_filters_from_postgres(engine, read_from_pg):
    from datetime import date

    common = {
        "tenant_id": TENANT, "jurisdiction_name": "X",
        "rate_cents_per_gallon": 184, "product_codes": ["DIESEL_2"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    # Active state row, a different tax_type, and an expired row.
    await _seed("tax_jurisdiction", {**common, "jurisdiction_id": "j_excise",
                "fips_code": "48", "tax_type": "excise",
                "effective_date": "2025-01-01"})
    await _seed("tax_jurisdiction", {**common, "jurisdiction_id": "j_ust",
                "fips_code": "48", "tax_type": "ust",
                "effective_date": "2025-01-01"})
    await _seed("tax_jurisdiction", {**common, "jurisdiction_id": "j_expired",
                "fips_code": "48", "tax_type": "excise",
                "effective_date": "2024-01-01", "expiry_date": "2025-06-01"})

    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_fetch_for_aggregation,
    )

    # tax_type filter → only excise rows (the endpoint's term filter).
    docs = await read_hybrid_fetch_for_aggregation(
        "tax_jurisdiction", TENANT, term_filters={"tax_type": "excise"},
        range_field="effective_date", range_lte="2026-01-15",
    )
    assert docs is not _NOT_CUT_OVER
    iso = date(2026, 1, 15).isoformat()
    items = [d for d in docs
             if not (d.get("expiry_date") and str(d["expiry_date"]) < iso)]
    assert {d["jurisdiction_id"] for d in items} == {"j_excise"}  # expired excluded


async def test_tax_exemption_list_blanket_and_product_from_postgres(engine, read_from_pg):
    base = {
        "tenant_id": TENANT, "exemption_type": "farm", "status": "valid",
        "expiry_date": "2026-12-31",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    # Product-specific, blanket (no product_codes), and a different-product cert.
    await _seed("tax_exemption", {**base, "exemption_id": "ex_diesel",
                "customer_id": "cust_1", "certificate_number": "CN-1",
                "product_codes": ["DIESEL_2"]})
    await _seed("tax_exemption", {**base, "exemption_id": "ex_blanket",
                "customer_id": "cust_1", "certificate_number": "CN-2"})
    await _seed("tax_exemption", {**base, "exemption_id": "ex_gas",
                "customer_id": "cust_1", "certificate_number": "CN-3",
                "product_codes": ["GASOLINE_REG"]})

    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_fetch_for_aggregation,
    )

    docs = await read_hybrid_fetch_for_aggregation(
        "tax_exemption", TENANT,
        term_filters={"customer_id": "cust_1", "status": "valid"},
        range_field="expiry_date", range_gte="2026-06-01",
    )
    assert docs is not _NOT_CUT_OVER
    # Endpoint's product_code rule: contains code OR blanket (no product_codes).
    pc = "DIESEL_2"
    items = [d for d in docs
             if not (d.get("product_codes") and pc not in d["product_codes"])]
    assert {d["exemption_id"] for d in items} == {"ex_diesel", "ex_blanket"}


# ---------------------------------------------------------------------------
# Agent lookup tools (get_all_locations / find_truck_by_id) served from PG
# ---------------------------------------------------------------------------


async def test_get_all_locations_served_from_postgres(engine, read_from_pg):
    from Agents.tools.lookup_tools import get_all_locations
    from Agents.tools._tenant_context import set_current_tenant

    await _seed("location", {"location_id": "LOC-1", "tenant_id": TENANT,
                             "location_name": "Houston Depot", "location_type": "depot",
                             "region": "TX"}, doc_id="LOC-1")
    await _seed("location", {"location_id": "LOC-2", "tenant_id": TENANT,
                             "location_name": "Dallas Warehouse", "location_type": "warehouse",
                             "region": "TX"}, doc_id="LOC-2")
    # Cross-tenant row must be excluded.
    await _seed("location", {"location_id": "LOC-X", "tenant_id": "other",
                             "location_name": "Leak", "location_type": "depot",
                             "region": "??"}, doc_id="LOC-X")

    # ES guard: the tool must NOT touch ES once cut over.
    import Agents.tools.lookup_tools as lt
    lt.elasticsearch_service.search_documents = _es_raises_on_read().search_documents

    with set_current_tenant(TENANT):
        out = await get_all_locations()
    assert "Houston Depot" in out
    assert "Dallas Warehouse" in out
    assert "Leak" not in out  # cross-tenant excluded
    assert "2 total" in out


async def test_find_truck_by_id_served_from_postgres(engine, read_from_pg):
    from Agents.tools.lookup_tools import find_truck_by_id
    from Agents.tools._tenant_context import set_current_tenant

    await _seed("truck", {"truck_id": "GI-58A", "asset_id": "GI-58A",
                          "tenant_id": TENANT, "asset_type": "vehicle",
                          "asset_subtype": "truck", "status": "on_time",
                          "plate_number": "GI-58A"}, doc_id="GI-58A")

    import Agents.tools.lookup_tools as lt
    lt.elasticsearch_service.search_documents = _es_raises_on_read().search_documents

    with set_current_tenant(TENANT):
        out = await find_truck_by_id("GI-58A")
    assert "GI-58A" in out
    assert "vehicle" in out


# ---------------------------------------------------------------------------
# Autonomous monitor agents — cross-tenant sweeps served from PG
# (job SLA, delay response, SLA guardian, truck fuel)
# ---------------------------------------------------------------------------


def _agent_es_guard():
    """ES double whose search_documents fails (read path must not be used),
    but whose write surface is permissive (agents may write/broadcast)."""
    es = _es_raises_on_read()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


async def test_hybrid_search_all_tenants_crosses_tenants(engine, read_from_pg):
    # Jobs across two tenants, both in_progress + past the threshold.
    await _seed("job", _job_doc("j_a", status="in_progress",
                                estimated_arrival="2026-01-01T00:00:00+00:00",
                                tenant_id=TENANT))
    await _seed("job", _job_doc("j_b", status="in_progress",
                                estimated_arrival="2026-01-01T00:00:00+00:00",
                                tenant_id="other-tenant"))
    await _seed("job", _job_doc("j_done", status="completed",
                                estimated_arrival="2026-01-01T00:00:00+00:00"))

    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_search_all_tenants,
    )
    rows = await read_hybrid_search_all_tenants(
        "job", term_filters={"status": "in_progress"},
        range_field="estimated_arrival", range_lte="2026-06-01T00:00:00+00:00",
        sort_field="estimated_arrival", sort_order="asc", size=200,
    )
    assert rows is not _NOT_CUT_OVER
    # Both tenants' in_progress jobs returned; completed excluded.
    assert {r["job_id"] for r in rows} == {"j_a", "j_b"}


async def test_job_sla_monitor_served_from_postgres(engine, read_from_pg):
    from datetime import datetime, timedelta, timezone

    # An in_progress job whose ETA is within the warning threshold.
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    await _seed("job", _job_doc("j_sla", status="in_progress",
                                estimated_arrival=soon))
    # A job far in the future — not at risk.
    far = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    await _seed("job", _job_doc("j_ok", status="in_progress",
                                estimated_arrival=far))

    from Agents.autonomous.job_sla_monitor import JobSLAMonitor
    agent = JobSLAMonitor(
        es_service=_agent_es_guard(),
        activity_log_service=_noop_activity_log(),
        ws_manager=_noop_ws(),
        confirmation_protocol=MagicMock(),
        feature_flag_service=None,
        signal_bus=None,
        sla_warning_threshold_minutes=30,
    )
    detections, _actions = await agent.monitor_cycle()
    assert "j_sla" in detections
    assert "j_ok" not in detections


# ``test_sla_guardian_served_from_postgres`` stood here. The ``shipment``
# aggregate was retired with the ``shipments_current`` table (rev 0007), so
# ``SLAGuardianAgent`` has no Postgres read path left to cover.


def _noop_activity_log():
    al = MagicMock()
    al.log_monitoring_cycle = AsyncMock(return_value="log-1")
    return al


def _noop_ws():
    ws = MagicMock()
    ws.broadcast_event = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# Overlay optimizer agents — jobs_current readers served from PG
# ---------------------------------------------------------------------------


def _overlay_es_guard():
    es = _es_raises_on_read()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


def _make_dispatch_optimizer():
    from Agents.overlay.dispatch_optimizer import DispatchOptimizer
    return DispatchOptimizer(
        signal_bus=MagicMock(),
        es_service=_overlay_es_guard(),
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
        execution_planner=MagicMock(),
    )


async def test_dispatch_optimizer_affected_jobs_from_postgres(engine, read_from_pg):
    await _seed("job", _job_doc("j_aff1", status="in_progress"))
    await _seed("job", _job_doc("j_aff2", status="in_progress"))
    await _seed("job", _job_doc("j_other", status="scheduled"))

    opt = _make_dispatch_optimizer()
    rows = await opt._query_affected_jobs({"j_aff1", "j_aff2", "j_other"}, TENANT)
    # Only in_progress jobs within the id set.
    assert {r["job_id"] for r in rows} == {"j_aff1", "j_aff2"}


async def test_dispatch_optimizer_available_assets_from_postgres(engine, read_from_pg):
    await _seed("truck", {"truck_id": "AV1", "asset_id": "AV1", "tenant_id": TENANT,
                          "asset_type": "vehicle", "status": "on_time"}, doc_id="AV1")
    await _seed("truck", {"truck_id": "AV2", "asset_id": "AV2", "tenant_id": TENANT,
                          "asset_type": "vehicle", "status": "delayed"}, doc_id="AV2")
    # Cross-tenant on_time truck must be excluded.
    await _seed("truck", {"truck_id": "AVX", "asset_id": "AVX", "tenant_id": "other",
                          "asset_type": "vehicle", "status": "on_time"}, doc_id="AVX")

    opt = _make_dispatch_optimizer()
    rows = await opt._query_available_assets(TENANT)
    assert {r["truck_id"] for r in rows} == {"AV1"}


async def test_job_priority_engine_active_jobs_from_postgres(engine, read_from_pg):
    await _seed("job", _job_doc("jp1", status="scheduled"))
    await _seed("job", _job_doc("jp2", status="assigned"))
    await _seed("job", _job_doc("jp3", status="in_progress"))
    await _seed("job", _job_doc("jp_done", status="completed"))

    from Agents.overlay.job_priority_engine import JobPriorityEngine
    eng = JobPriorityEngine(
        signal_bus=MagicMock(),
        es_service=_overlay_es_guard(),
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
    )
    rows = await eng._query_active_jobs(TENANT)
    assert {r["job_id"] for r in rows} == {"jp1", "jp2", "jp3"}  # completed excluded


# ---------------------------------------------------------------------------
# Route planning + delivery prioritization agents — fuel_orders_current from PG
# ---------------------------------------------------------------------------


def _fuel_order_doc(order_id, status="confirmed", tenant_id=TENANT):
    return {
        "order_id": order_id, "tenant_id": tenant_id, "customer_id": "cust_1",
        "customer_name": "Acme", "ship_to_address": "1 St",
        "ship_to_lat": 32.7, "ship_to_lon": -96.8, "call_type": "will_call",
        "fill_to_full": True, "product_code": "DIESEL_2",
        "intake_channel": "dispatcher", "intake_channel_id": "ch_1",
        "status": status, "source_schema_version": "1.0", "trace_id": "t",
        "last_event_timestamp": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


async def test_route_planning_routable_orders_from_postgres(engine, read_from_pg):
    await _seed("fuel_order", _fuel_order_doc("o_conf", status="confirmed"))
    await _seed("fuel_order", _fuel_order_doc("o_sched", status="scheduled"))
    await _seed("fuel_order", _fuel_order_doc("o_placed", status="placed"))
    await _seed("fuel_order", _fuel_order_doc("o_other", status="delivered"))

    from Agents.overlay.route_planning_agent import RoutePlanningAgent
    agent = RoutePlanningAgent(
        signal_bus=MagicMock(),
        es_service=_overlay_es_guard(),
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
    )
    rows = await agent._fetch_routable_orders(TENANT)
    # Only confirmed + scheduled are routable.
    assert {r["order_id"] for r in rows} == {"o_conf", "o_sched"}


async def test_delivery_prioritization_pending_and_discovery_from_postgres(engine, read_from_pg):
    await _seed("fuel_order", _fuel_order_doc("p_placed", status="placed"))
    await _seed("fuel_order", _fuel_order_doc("p_conf", status="confirmed"))
    await _seed("fuel_order", _fuel_order_doc("p_done", status="delivered"))
    # A second tenant's pending order (for cross-tenant discovery).
    await _seed("fuel_order", _fuel_order_doc("p_other", status="placed",
                                              tenant_id="other-tenant"))

    from Agents.overlay.delivery_prioritization_agent import (
        DeliveryPrioritizationAgent,
    )
    agent = DeliveryPrioritizationAgent(
        signal_bus=MagicMock(),
        es_service=_overlay_es_guard(),
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
    )

    # Tenant-scoped pending fetch: placed + confirmed (delivered excluded).
    pending = await agent._fetch_pending_orders(TENANT)
    assert {o["order_id"] for o in pending} == {"p_placed", "p_conf"}

    # Cross-tenant discovery includes both tenants with pending orders.
    tenants = await agent._discover_tenants_with_pending_orders()
    assert set(tenants) == {TENANT, "other-tenant"}


# ---------------------------------------------------------------------------
# Commerce pricing-rules list endpoint (compliance_pricing_rule) from PG
# ---------------------------------------------------------------------------


async def test_compliance_pricing_rule_list_filters_from_postgres(engine, read_from_pg):
    base = {
        "tenant_id": TENANT, "product_code": "DIESEL_2", "status": "active",
        "strategy": "posted_price", "posted_price_cents": 325, "priority": 10,
        "effective_date": "2026-01-01", "expiry_date": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    await _seed("compliance_pricing_rule", {**base, "rule_id": "cpr_c1",
                "customer_id": "cust_1"})
    await _seed("compliance_pricing_rule", {**base, "rule_id": "cpr_c2",
                "customer_id": "cust_2"})
    await _seed("compliance_pricing_rule", {**base, "rule_id": "cpr_gas",
                "customer_id": "cust_1", "product_code": "GASOLINE_REG"})

    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_fetch_for_aggregation,
    )

    # customer_id + product_code term filters (the endpoint's logic).
    docs = await read_hybrid_fetch_for_aggregation(
        "compliance_pricing_rule", TENANT,
        term_filters={"customer_id": "cust_1", "product_code": "DIESEL_2"},
    )
    assert docs is not _NOT_CUT_OVER
    assert {d["rule_id"] for d in docs} == {"cpr_c1"}
