"""Read-cutover tests for the ops shipment reads + metrics.

With ``COMMERCE_READ_FROM_POSTGRES`` on, the ops shipment list/get/sla-breaches
/failures serve from the Postgres ``shipments_current`` rows and the metrics
endpoints aggregate over them in Python (``shipment_metrics_aggregator``).
These cover the pure aggregator AND the repository search/get wiring.
"""

from __future__ import annotations

import pytest

from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.read_repositories import HybridReadRepository
from persistence.repositories import CurrentStateRepository
from ops.services import shipment_metrics_aggregator as sagg

TENANT = "demo-tenant"


@pytest.fixture
def read_from_pg(monkeypatch):
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is True
    yield
    clear_settings_cache()


def _ship(shipment_id, **over):
    doc = {
        "shipment_id": shipment_id, "tenant_id": TENANT, "status": "in_transit",
        "rider_id": "RIDER-1", "updated_at": "2026-01-01T00:00:00+00:00",
        "last_event_timestamp": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    doc.update(over)
    return doc


async def _seed(doc):
    async with session_scope() as s:
        await CurrentStateRepository("shipment").upsert(s, doc=doc)


# ---------------------------------------------------------------------------
# Pure aggregator
# ---------------------------------------------------------------------------


def test_status_buckets_fill_gaps_and_count():
    docs = [
        _ship("s1", status="in_transit", updated_at="2026-01-01T00:00:00+00:00"),
        _ship("s2", status="delivered", updated_at="2026-01-03T00:00:00+00:00"),
    ]
    out = sagg.shipment_status_buckets(docs, "1d")
    assert [b["timestamp"] for b in out] == [
        "2026-01-01T00:00:00.000Z",
        "2026-01-02T00:00:00.000Z",
        "2026-01-03T00:00:00.000Z",
    ]
    assert out[0]["values"] == {"in_transit": 1, "total": 1}
    assert out[1]["values"] == {"total": 0}
    assert out[2]["values"] == {"delivered": 1, "total": 1}


def test_sla_buckets_breach_and_compliance():
    docs = [
        # breached: last_event after estimated_delivery
        _ship("b1", updated_at="2026-01-01T00:00:00+00:00",
              estimated_delivery="2026-01-01T05:00:00+00:00",
              last_event_timestamp="2026-01-01T06:00:00+00:00"),
        # compliant: last_event before estimated_delivery
        _ship("c1", updated_at="2026-01-01T01:00:00+00:00",
              estimated_delivery="2026-01-01T10:00:00+00:00",
              last_event_timestamp="2026-01-01T08:00:00+00:00"),
    ]
    out = sagg.shipment_sla_buckets(docs, "1d")
    assert len(out) == 1
    v = out[0]["values"]
    assert v["total"] == 2 and v["breached"] == 1 and v["compliant"] == 1
    assert v["compliance_pct"] == 50.0


def test_failure_buckets_by_reason():
    docs = [
        _ship("f1", status="failed", failure_reason="address_not_found",
              updated_at="2026-01-01T00:00:00+00:00"),
        _ship("f2", status="failed", failure_reason="address_not_found",
              updated_at="2026-01-01T01:00:00+00:00"),
        _ship("f3", status="failed", failure_reason="customer_unavailable",
              updated_at="2026-01-01T02:00:00+00:00"),
    ]
    out = sagg.shipment_failure_buckets(docs, "1d")
    assert len(out) == 1
    v = out[0]["values"]
    assert v["total_failures"] == 3
    assert v["address_not_found"] == 2
    assert v["customer_unavailable"] == 1


# ---------------------------------------------------------------------------
# Repository wiring (list/get/search) from Postgres
# ---------------------------------------------------------------------------


async def test_shipment_search_filters_and_paginates(engine, read_from_pg):
    await _seed(_ship("s1", status="in_transit",
                      updated_at="2026-01-03T00:00:00+00:00"))
    await _seed(_ship("s2", status="delivered",
                      updated_at="2026-01-02T00:00:00+00:00"))
    await _seed(_ship("s3", status="in_transit",
                      updated_at="2026-01-01T00:00:00+00:00"))
    async with session_scope() as s:
        res = await HybridReadRepository("shipment").search(
            s, TENANT, term_filters={"status": "in_transit"},
            sort_field="updated_at", sort_order="desc", page=1, size=10,
        )
    assert res["total"] == 2
    assert [it["shipment_id"] for it in res["items"]] == ["s1", "s3"]


async def test_shipment_sla_breach_search_exists_and_range(engine, read_from_pg):
    # Only b1 has estimated_delivery in the past.
    await _seed(_ship("b1", estimated_delivery="2020-01-01T00:00:00+00:00"))
    await _seed(_ship("f1", estimated_delivery="2999-01-01T00:00:00+00:00"))
    await _seed(_ship("n1"))  # no estimated_delivery
    async with session_scope() as s:
        res = await HybridReadRepository("shipment").search(
            s, TENANT, exists_fields=["estimated_delivery"],
            range_field="estimated_delivery", range_lt="2026-06-01T00:00:00+00:00",
            sort_field="estimated_delivery", sort_order="asc", page=1, size=10,
        )
    assert {it["shipment_id"] for it in res["items"]} == {"b1"}


async def test_shipment_get_served_from_postgres(engine, read_from_pg):
    await _seed(_ship("s_get", status="delivered"))
    async with session_scope() as s:
        doc = await HybridReadRepository("shipment").get(s, TENANT, "s_get")
    assert doc is not None
    assert doc["shipment_id"] == "s_get"
    assert doc["status"] == "delivered"


async def test_shipment_metrics_fetch_served_from_postgres(engine, read_from_pg):
    await _seed(_ship("m1", status="in_transit",
                      updated_at="2026-01-01T00:00:00+00:00"))
    await _seed(_ship("m2", status="delivered",
                      updated_at="2026-01-01T05:00:00+00:00"))
    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_fetch_for_aggregation,
    )
    docs = await read_hybrid_fetch_for_aggregation("shipment", TENANT)
    assert docs is not _NOT_CUT_OVER
    buckets = sagg.shipment_status_buckets(docs, "1h")
    assert len(buckets) == 6  # 00:00..05:00 inclusive
    assert buckets[0]["values"] == {"in_transit": 1, "total": 1}
    assert buckets[-1]["values"] == {"delivered": 1, "total": 1}
