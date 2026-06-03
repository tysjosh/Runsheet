"""Read-cutover tests for the scheduling metrics/analytics endpoints.

The metrics endpoints aggregate over the migrated ``jobs_current`` index. With
``COMMERCE_READ_FROM_POSTGRES`` on, they aggregate the matching Postgres job
rows in Python (``job_metrics_aggregator``) instead of issuing ES aggregations.
These tests cover the pure aggregator AND the service-level wiring, and assert
the ES read path is NOT touched after cutover.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.repositories import CurrentStateRepository
from scheduling.services import job_metrics_aggregator as agg

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


def _job(job_id, **over):
    doc = {
        "job_id": job_id, "tenant_id": TENANT, "job_type": "cargo_transport",
        "status": "scheduled", "asset_assigned": None, "delayed": False,
        "scheduled_time": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    doc.update(over)
    return doc


async def _seed(doc):
    async with session_scope() as s:
        await CurrentStateRepository("job").upsert(s, doc=doc)


# ---------------------------------------------------------------------------
# Pure aggregator
# ---------------------------------------------------------------------------


def test_bucket_over_time_fills_empty_interior_buckets():
    jobs = [
        _job("a", scheduled_time="2026-01-01T00:00:00+00:00", status="scheduled"),
        _job("b", scheduled_time="2026-01-03T00:00:00+00:00", status="completed",
             job_type="fuel_delivery"),
    ]
    out = agg.bucket_jobs_over_time(jobs, "1d")
    # Jan 1, Jan 2 (empty, min_doc_count=0), Jan 3.
    assert [b["timestamp"] for b in out] == [
        "2026-01-01T00:00:00.000Z",
        "2026-01-02T00:00:00.000Z",
        "2026-01-03T00:00:00.000Z",
    ]
    assert out[0]["total"] == 1
    assert out[0]["counts_by_status"] == {"scheduled": 1}
    assert out[1]["total"] == 0 and out[1]["counts_by_status"] == {}
    assert out[2]["counts_by_type"] == {"fuel_delivery": 1}


def test_bucket_over_time_hourly_and_skips_unparseable():
    jobs = [
        _job("a", scheduled_time="2026-01-01T00:15:00+00:00"),
        _job("b", scheduled_time="2026-01-01T00:45:00+00:00"),
        _job("c", scheduled_time=None),
    ]
    out = agg.bucket_jobs_over_time(jobs, "1h")
    assert len(out) == 1
    assert out[0]["timestamp"] == "2026-01-01T00:00:00.000Z"
    assert out[0]["total"] == 2


def test_completion_metrics_rate_and_avg_and_ordering():
    jobs = [
        _job("c1", job_type="cargo_transport", status="completed",
             started_at="2026-01-01T00:00:00+00:00",
             completed_at="2026-01-01T01:00:00+00:00"),
        _job("c2", job_type="cargo_transport", status="scheduled"),
        _job("f1", job_type="fuel_delivery", status="completed",
             started_at="2026-01-01T00:00:00+00:00",
             completed_at="2026-01-01T00:30:00+00:00"),
    ]
    out = agg.completion_metrics(jobs)
    # cargo_transport has 2 (doc_count desc → first), fuel_delivery has 1.
    assert [m["job_type"] for m in out] == ["cargo_transport", "fuel_delivery"]
    cargo = out[0]
    assert cargo["total"] == 2 and cargo["completed"] == 1
    assert cargo["completion_rate"] == 50.0
    assert cargo["avg_completion_minutes"] == 60.0
    assert out[1]["avg_completion_minutes"] == 30.0


def test_asset_utilization_active_and_idle():
    jobs = [
        _job("j1", asset_assigned="TRUCK-1", status="completed",
             started_at="2026-01-01T00:00:00+00:00",
             completed_at="2026-01-01T02:00:00+00:00"),
        _job("j2", asset_assigned="TRUCK-1", status="assigned"),
        _job("j3", asset_assigned=None, status="scheduled"),
    ]
    out = agg.asset_utilization(
        jobs, "2026-01-01T00:00:00+00:00", "2026-01-01T10:00:00+00:00")
    assert len(out) == 1  # job without asset excluded
    m = out[0]
    assert m["asset_id"] == "TRUCK-1"
    assert m["total_jobs"] == 2 and m["active_jobs"] == 1 and m["completed_jobs"] == 1
    assert m["total_active_hours"] == 2.0
    assert m["idle_hours"] == 8.0  # 10h range - 2h active


def test_delay_metrics_avg_and_by_type():
    jobs = [
        _job("d1", job_type="cargo_transport", delayed=True, delay_duration_minutes=10),
        _job("d2", job_type="cargo_transport", delayed=True, delay_duration_minutes=30),
        _job("d3", job_type="fuel_delivery", delayed=True, delay_duration_minutes=20),
    ]
    out = agg.delay_metrics(jobs)
    assert out["total_delayed"] == 3
    assert out["avg_delay_minutes"] == 20.0
    assert out["delays_by_job_type"][0]["job_type"] == "cargo_transport"
    assert out["delays_by_job_type"][0]["count"] == 2
    assert out["delays_by_job_type"][0]["avg_delay_minutes"] == 20.0


# ---------------------------------------------------------------------------
# Service-level wiring (fetch from Postgres, ES guard must not trip)
# ---------------------------------------------------------------------------


async def test_job_metrics_endpoint_served_from_postgres(engine, read_from_pg):
    await _seed(_job("m1", scheduled_time="2026-01-01T00:00:00+00:00",
                     status="scheduled"))
    await _seed(_job("m2", scheduled_time="2026-01-01T05:00:00+00:00",
                     status="completed", job_type="fuel_delivery"))

    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER, read_hybrid_fetch_for_aggregation,
    )
    jobs = await read_hybrid_fetch_for_aggregation("job", TENANT)
    assert jobs is not _NOT_CUT_OVER
    buckets = agg.bucket_jobs_over_time(jobs, "1h")
    # 6 hourly buckets from 00:00 to 05:00 inclusive, gaps filled.
    assert len(buckets) == 6
    assert buckets[0]["counts_by_status"] == {"scheduled": 1}
    assert buckets[-1]["counts_by_type"] == {"fuel_delivery": 1}


async def test_delay_metrics_service_served_from_postgres(engine, read_from_pg):
    await _seed(_job("dd1", delayed=True, delay_duration_minutes=15,
                     job_type="cargo_transport"))
    await _seed(_job("dd2", delayed=False))  # excluded by delayed filter

    from scheduling.services.delay_detection_service import DelayDetectionService
    svc = DelayDetectionService(_es_raises_on_read())
    metrics = await svc.get_delay_metrics(TENANT)
    assert metrics["total_delayed"] == 1
    assert metrics["avg_delay_minutes"] == 15.0
    assert metrics["delays_by_job_type"][0]["job_type"] == "cargo_transport"


async def test_fetch_for_aggregation_respects_date_range_and_tenant(engine, read_from_pg):
    await _seed(_job("in", scheduled_time="2026-02-15T00:00:00+00:00"))
    await _seed(_job("out_early", scheduled_time="2026-01-15T00:00:00+00:00"))
    await _seed(_job("other", scheduled_time="2026-02-15T00:00:00+00:00",
                     tenant_id="other"))
    from commerce.services.commerce_persistence_bridge import (
        read_hybrid_fetch_for_aggregation,
    )
    jobs = await read_hybrid_fetch_for_aggregation(
        "job", TENANT,
        range_field="scheduled_time",
        range_gte="2026-02-01T00:00:00+00:00",
        range_lte="2026-02-28T00:00:00+00:00",
    )
    assert {j["job_id"] for j in jobs} == {"in"}
