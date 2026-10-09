"""
Unit tests for DelayDetectionService.

Tests delay detection (check_delays), ETA retrieval (get_eta),
and delay metrics aggregation (get_delay_metrics).

Requirements: 7.1-7.6
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from scheduling.services.delay_detection_service import DelayDetectionService
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.get_document = AsyncMock(return_value=None)  # merged read-back (OI-31)
    return es


def _make_ws_mock() -> AsyncMock:
    """Return a mock WebSocket manager."""
    ws = AsyncMock()
    ws.broadcast = AsyncMock()
    return ws


def _make_service(es_mock=None, ws_mock=None) -> DelayDetectionService:
    """Create a DelayDetectionService with mocked dependencies."""
    if es_mock is None:
        es_mock = _make_es_mock()
    if ws_mock is None:
        ws_mock = _make_ws_mock()
    return DelayDetectionService(es_service=es_mock, ws_manager=ws_mock)


def _overdue_job_hit(
    job_id: str = "JOB_1",
    tenant_id: str = "tenant_a",
    minutes_overdue: int = 30,
    job_type: str = "cargo_transport",
) -> dict:
    """Return an ES hit for an in_progress job that is overdue."""
    eta = datetime.now(timezone.utc) - timedelta(minutes=minutes_overdue)
    return {
        "_source": {
            "job_id": job_id,
            "job_type": job_type,
            "status": "in_progress",
            "tenant_id": tenant_id,
            "asset_assigned": "TRUCK_001",
            "origin": "Port Harcourt",
            "destination": "Lagos",
            "estimated_arrival": eta.isoformat(),
            "delayed": False,
            "delay_duration_minutes": 0,
            "scheduled_time": (eta - timedelta(hours=4)).isoformat(),
        }
    }


# ---------------------------------------------------------------------------
# Test: check_delays marks overdue jobs as delayed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_delays_marks_overdue_jobs_as_delayed():
    """check_delays should set delayed=true for in_progress jobs past ETA.

    Validates: Requirement 7.3
    """
    es = _make_es_mock()
    ws = _make_ws_mock()
    hit = _overdue_job_hit(minutes_overdue=45)

    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [hit], "total": {"value": 1}}}
    )
    svc = _make_service(es, ws)

    result = await svc.check_delays(tenant_id="tenant_a")

    assert len(result) == 1
    assert result[0]["delayed"] is True

    # Verify update_document was called with delayed=True
    es.update_document.assert_awaited_once()
    call_args = es.update_document.await_args
    assert call_args.args[0] == "jobs_current"
    assert call_args.args[1] == "JOB_1"
    update_fields = call_args.args[2]
    assert update_fields["delayed"] is True
    assert "updated_at" in update_fields


@pytest.mark.asyncio
async def test_check_delays_no_overdue_jobs_returns_empty():
    """check_delays should return empty list when no jobs are overdue.

    Validates: Requirement 7.3
    """
    es = _make_es_mock()
    ws = _make_ws_mock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    svc = _make_service(es, ws)

    result = await svc.check_delays()

    assert result == []
    es.update_document.assert_not_awaited()
    ws.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_delays_broadcasts_delay_alert():
    """check_delays should broadcast a delay_alert via WebSocket for each delayed job.

    Validates: Requirement 7.4
    """
    es = _make_es_mock()
    ws = _make_ws_mock()
    hit = _overdue_job_hit(minutes_overdue=20)

    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [hit], "total": {"value": 1}}}
    )
    svc = _make_service(es, ws)

    await svc.check_delays()

    ws.broadcast.assert_awaited_once()
    broadcast_args = ws.broadcast.await_args
    assert broadcast_args.args[0] == "delay_alert"
    payload = broadcast_args.args[1]
    assert payload["job_id"] == "JOB_1"
    assert payload["job_type"] == "cargo_transport"
    assert payload["asset_assigned"] == "TRUCK_001"
    assert "delay_duration_minutes" in payload


@pytest.mark.asyncio
async def test_check_delays_without_tenant_checks_all():
    """check_delays with tenant_id=None should not include a tenant filter.

    Validates: Requirement 7.3
    """
    es = _make_es_mock()
    ws = _make_ws_mock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    svc = _make_service(es, ws)

    await svc.check_delays(tenant_id=None)

    query_body = es.search_documents.await_args.args[1]
    must_clauses = query_body["query"]["bool"]["must"]
    # Should NOT have a tenant_id term clause
    tenant_clauses = [c for c in must_clauses if "term" in c and "tenant_id" in c.get("term", {})]
    assert len(tenant_clauses) == 0


# ---------------------------------------------------------------------------
# Test: check_delays calculates delay_duration_minutes correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_delays_calculates_delay_duration_minutes():
    """check_delays should calculate delay_duration_minutes as (now - ETA) in minutes.

    Validates: Requirement 7.3
    """
    es = _make_es_mock()
    ws = _make_ws_mock()
    minutes_overdue = 60
    hit = _overdue_job_hit(minutes_overdue=minutes_overdue)

    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [hit], "total": {"value": 1}}}
    )
    svc = _make_service(es, ws)

    result = await svc.check_delays()

    assert len(result) == 1
    update_fields = es.update_document.await_args.args[2]
    # Allow a small tolerance since time passes between hit creation and check
    calculated_delay = update_fields["delay_duration_minutes"]
    assert abs(calculated_delay - minutes_overdue) <= 2


@pytest.mark.asyncio
async def test_check_delays_handles_multiple_overdue_jobs():
    """check_delays should process multiple overdue jobs and mark each as delayed.

    Validates: Requirement 7.3
    """
    es = _make_es_mock()
    ws = _make_ws_mock()
    hit1 = _overdue_job_hit(job_id="JOB_1", minutes_overdue=30)
    hit2 = _overdue_job_hit(job_id="JOB_2", minutes_overdue=90, job_type="vessel_movement")

    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [hit1, hit2], "total": {"value": 2}}}
    )
    svc = _make_service(es, ws)

    result = await svc.check_delays()

    assert len(result) == 2
    assert es.update_document.await_count == 2
    assert ws.broadcast.await_count == 2


# ---------------------------------------------------------------------------
# Test: get_eta returns estimated_arrival
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_eta_returns_estimated_arrival():
    """get_eta should return the ETA fields for a job.

    Validates: Requirement 7.2
    """
    es = _make_es_mock()
    eta_time = "2026-03-12T14:00:00+00:00"
    es.search_documents = AsyncMock(return_value={
        "hits": {
            "hits": [{
                "_source": {
                    "job_id": "JOB_10",
                    "estimated_arrival": eta_time,
                    "delayed": False,
                    "delay_duration_minutes": None,
                    "status": "in_progress",
                    "scheduled_time": "2026-03-12T10:00:00+00:00",
                }
            }],
            "total": {"value": 1},
        }
    })
    svc = _make_service(es)

    result = await svc.get_eta("JOB_10", "tenant_a")

    assert result["job_id"] == "JOB_10"
    assert result["estimated_arrival"] == eta_time
    assert result["delayed"] is False
    assert result["status"] == "in_progress"
    assert result["scheduled_time"] == "2026-03-12T10:00:00+00:00"


def _eta_hit(status: str, estimated_arrival):
    return {
        "hits": {
            "hits": [{
                "_source": {
                    "job_id": "JOB_11",
                    "estimated_arrival": estimated_arrival,
                    "delayed": False,
                    "delay_duration_minutes": None,
                    "status": status,
                    "scheduled_time": "2026-03-12T10:00:00+00:00",
                }
            }],
            "total": {"value": 1},
        }
    }


@pytest.mark.asyncio
async def test_get_eta_in_progress_includes_eta_minutes():
    """An in_progress job gets eta_minutes, rounded up (OI-49)."""
    es = _make_es_mock()
    arrival = datetime.now(timezone.utc) + timedelta(minutes=41, seconds=30)
    es.search_documents = AsyncMock(
        return_value=_eta_hit("in_progress", arrival.isoformat())
    )
    result = await _make_service(es).get_eta("JOB_11", "tenant_a")
    assert result["eta_minutes"] == 42


@pytest.mark.asyncio
async def test_get_eta_overdue_in_progress_floors_eta_minutes_at_zero():
    es = _make_es_mock()
    arrival = datetime.now(timezone.utc) - timedelta(minutes=10)
    es.search_documents = AsyncMock(
        return_value=_eta_hit("in_progress", arrival.isoformat())
    )
    result = await _make_service(es).get_eta("JOB_11", "tenant_a")
    assert result["eta_minutes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,arrival",
    [
        ("scheduled", "future"),
        ("assigned", "future"),
        ("in_progress", None),
        ("in_progress", "not-a-date"),
    ],
)
async def test_get_eta_omits_eta_minutes_without_a_live_eta(status, arrival):
    """Not in_progress, or no parseable estimated_arrival: no eta_minutes."""
    if arrival == "future":
        arrival = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value=_eta_hit(status, arrival))
    result = await _make_service(es).get_eta("JOB_11", "tenant_a")
    assert "eta_minutes" not in result


@pytest.mark.asyncio
async def test_get_eta_not_found_raises_404():
    """get_eta should raise 404 when the job is not found.

    Validates: Requirement 7.2
    """
    es = _make_es_mock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    svc = _make_service(es)

    with pytest.raises(AppException) as exc_info:
        await svc.get_eta("JOB_MISSING", "tenant_a")

    assert exc_info.value.status_code == 404
    assert "JOB_MISSING" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test: get_delay_metrics returns correct counts and averages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_delay_metrics_returns_correct_counts_and_averages():
    """get_delay_metrics should return total_delayed, avg_delay_minutes, and by-type breakdown.

    The ES path fetches delayed jobs and aggregates them with the same code as
    the PG path (F10), so completed jobs keep their stored minutes.

    Validates: Requirement 7.5
    """
    def _job(job_type, minutes):
        return {"_source": {
            "job_id": f"J-{job_type}-{minutes}", "job_type": job_type,
            "status": "completed", "delay_duration_minutes": minutes,
        }}

    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [
            _job("cargo_transport", 30), _job("cargo_transport", 35),
            _job("cargo_transport", 40), _job("vessel_movement", 50),
            _job("vessel_movement", 57.5),
        ], "total": {"value": 5}},
    })
    svc = _make_service(es)

    result = await svc.get_delay_metrics("tenant_a")

    assert result["total_delayed"] == 5
    assert result["avg_delay_minutes"] == 42.5
    assert len(result["delays_by_job_type"]) == 2
    assert result["delays_by_job_type"][0]["job_type"] == "cargo_transport"
    assert result["delays_by_job_type"][0]["count"] == 3
    assert result["delays_by_job_type"][0]["avg_delay_minutes"] == 35.0
    assert result["delays_by_job_type"][1]["job_type"] == "vessel_movement"
    assert result["delays_by_job_type"][1]["count"] == 2
    assert result["delays_by_job_type"][1]["avg_delay_minutes"] == 53.75


@pytest.mark.asyncio
async def test_open_delayed_job_delay_is_computed_at_read_time():
    """F10: an in-progress job flagged at ~0 min whose ETA was 7 h ago reads
    420 min; a completed delayed job keeps its stored value."""
    now = datetime.now(timezone.utc)
    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [
            {"_source": {
                "job_id": "JOB_30", "job_type": "fuel_delivery", "status": "in_progress",
                "estimated_arrival": (now - timedelta(hours=7, seconds=30)).isoformat(),
                "delay_duration_minutes": 0,
            }},
        ], "total": {"value": 1}},
    })
    result = await _make_service(es).get_delay_metrics("tenant_a")
    assert result["avg_delay_minutes"] == 420.0

    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [
            {"_source": {
                "job_id": "JOB_9", "job_type": "fuel_delivery", "status": "completed",
                "estimated_arrival": (now - timedelta(days=3)).isoformat(),
                "delay_duration_minutes": 25,
            }},
        ], "total": {"value": 1}},
    })
    result = await _make_service(es).get_delay_metrics("tenant_a")
    assert result["avg_delay_minutes"] == 25.0


def test_effective_delay_minutes_rules():
    from scheduling.services.job_metrics_aggregator import effective_delay_minutes

    now = datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc)
    eta = "2026-10-08T16:00:00Z"
    assert effective_delay_minutes(
        {"status": "in_progress", "estimated_arrival": eta, "delay_duration_minutes": 0}, now
    ) == 420.0
    # A stored value larger than the live one is kept (never shrinks).
    assert effective_delay_minutes(
        {"status": "in_progress", "estimated_arrival": eta, "delay_duration_minutes": 500}, now
    ) == 500.0
    # ETA in the future (should not happen for a delayed job) floors at 0.
    assert effective_delay_minutes(
        {"status": "in_progress", "estimated_arrival": "2026-10-09T00:00:00Z"}, now
    ) == 0.0
    assert effective_delay_minutes(
        {"status": "completed", "estimated_arrival": eta, "delay_duration_minutes": 12}, now
    ) == 12.0
    assert effective_delay_minutes({"status": "completed"}, now) is None
    assert effective_delay_minutes(
        {"status": "in_progress", "estimated_arrival": "garbage", "delay_duration_minutes": 3}, now
    ) == 3.0


@pytest.mark.asyncio
async def test_get_delay_metrics_with_no_delays():
    """get_delay_metrics should return zeros when no delayed jobs exist.

    Validates: Requirement 7.5
    """
    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "avg_delay": {"value": None},
            "delays_by_job_type": {"buckets": []},
        },
    })
    svc = _make_service(es)

    result = await svc.get_delay_metrics("tenant_a")

    assert result["total_delayed"] == 0
    assert result["avg_delay_minutes"] == 0.0
    assert result["delays_by_job_type"] == []


@pytest.mark.asyncio
async def test_get_delay_metrics_with_date_range():
    """get_delay_metrics should include date range filter when start_date/end_date provided.

    Validates: Requirement 7.5
    """
    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 2}},
        "aggregations": {
            "avg_delay": {"value": 15.0},
            "delays_by_job_type": {"buckets": []},
        },
    })
    svc = _make_service(es)

    await svc.get_delay_metrics(
        "tenant_a",
        start_date="2026-01-01T00:00:00Z",
        end_date="2026-01-31T23:59:59Z",
    )

    query_body = es.search_documents.await_args.args[1]
    must_clauses = query_body["query"]["bool"]["must"]
    range_clauses = [c for c in must_clauses if "range" in c and "scheduled_time" in c.get("range", {})]
    assert len(range_clauses) == 1
    date_range = range_clauses[0]["range"]["scheduled_time"]
    assert date_range["gte"] == "2026-01-01T00:00:00Z"
    assert date_range["lte"] == "2026-01-31T23:59:59Z"


@pytest.mark.asyncio
async def test_get_delayed_jobs_reports_live_minutes():
    """F10: /scheduling/jobs/delayed shows the delay as of now, not the ~0
    minutes frozen when the sweep flagged the job."""
    from scheduling.services.job_service import JobService

    now = datetime.now(timezone.utc)
    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": [{"_source": {
        "job_id": "JOB_30", "status": "in_progress", "delayed": True,
        "estimated_arrival": (now - timedelta(hours=2, seconds=10)).isoformat(),
        "delay_duration_minutes": 0, "tenant_id": "tenant_a",
    }}], "total": {"value": 1}}})
    with patch("scheduling.services.job_service.get_settings") as settings:
        settings.return_value = MagicMock(scheduling_default_eta_hours=4)
        svc = JobService(es_service=es, redis_url=None)

    (job,) = await svc.get_delayed_jobs("tenant_a")
    assert job["delay_duration_minutes"] == 120
