"""
Unit tests for JobService - status transitions.

Tests valid/invalid status transitions, in_progress setting started_at and
estimated_arrival, completed setting completed_at and recording delay duration,
failed requiring failure_reason, and event appending for every transition.

Requirements: 4.1-4.8
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from scheduling.models import StatusTransition, JobStatus, VALID_TRANSITIONS
from scheduling.services.job_service import JobService
from errors.exceptions import AppException
from pydantic import ValidationError


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
    return es


def _make_service(es_mock: MagicMock) -> JobService:
    """Create a JobService with mocked dependencies."""
    with patch("scheduling.services.job_service.get_settings") as mock_settings:
        settings_obj = MagicMock()
        settings_obj.scheduling_default_eta_hours = 4
        mock_settings.return_value = settings_obj
        svc = JobService(es_service=es_mock, redis_url=None)
    svc._id_gen = MagicMock()
    svc._id_gen.next_id = AsyncMock(return_value="JOB_1")
    return svc


def _job_doc(
    job_id="JOB_1",
    job_type="cargo_transport",
    status="scheduled",
    tenant_id="t1",
    asset_assigned=None,
    scheduled_time="2026-03-12T10:00:00Z",
    estimated_arrival=None,
    delayed=False,
) -> dict:
    """Return a minimal job document with configurable status and fields."""
    return {
        "job_id": job_id,
        "job_type": job_type,
        "status": status,
        "tenant_id": tenant_id,
        "asset_assigned": asset_assigned,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": scheduled_time,
        "estimated_arrival": estimated_arrival,
        "started_at": None,
        "completed_at": None,
        "created_at": "2026-03-12T00:00:00Z",
        "updated_at": "2026-03-12T00:00:00Z",
        "created_by": None,
        "priority": "normal",
        "delayed": delayed,
        "delay_duration_minutes": None,
        "failure_reason": None,
        "notes": None,
        "cargo_manifest": [
            {
                "item_id": "ITEM_1",
                "description": "Steel",
                "weight_kg": 500,
                "item_status": "pending",
            }
        ],
    }


def _job_hit(doc: dict) -> dict:
    """Wrap a job document in an ES search response."""
    return {
        "hits": {
            "hits": [{"_source": doc}],
            "total": {"value": 1},
        }
    }


# ---------------------------------------------------------------------------
# Test: All valid transitions succeed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_transition_scheduled_to_assigned():
    """scheduled → assigned should succeed.

    Validates: Requirement 4.2
    """
    es = _make_es_mock()
    doc = _job_doc(status="scheduled")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.ASSIGNED)
    result = await svc.transition_status("JOB_1", transition, "t1", actor_id="op_1")

    assert result.status == JobStatus.ASSIGNED
    es.update_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_transition_scheduled_to_cancelled():
    """scheduled → cancelled should succeed.

    Validates: Requirement 4.2
    """
    es = _make_es_mock()
    doc = _job_doc(status="scheduled")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.CANCELLED)
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_valid_transition_assigned_to_in_progress():
    """assigned → in_progress should succeed when asset is assigned.

    Validates: Requirement 4.2, 4.4
    """
    es = _make_es_mock()
    doc = _job_doc(status="assigned", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.IN_PROGRESS)
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.status == JobStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_valid_transition_assigned_to_cancelled():
    """assigned → cancelled should succeed.

    Validates: Requirement 4.2
    """
    es = _make_es_mock()
    doc = _job_doc(status="assigned", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.CANCELLED)
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_valid_transition_in_progress_to_completed():
    """in_progress → completed should succeed.

    Validates: Requirement 4.2, 4.5
    """
    es = _make_es_mock()
    doc = _job_doc(status="in_progress", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.COMPLETED)
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_valid_transition_in_progress_to_failed():
    """in_progress → failed should succeed with failure_reason.

    Validates: Requirement 4.2, 4.6
    """
    es = _make_es_mock()
    doc = _job_doc(status="in_progress", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.FAILED, failure_reason="Engine failure")
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_valid_transition_in_progress_to_cancelled():
    """in_progress → cancelled should succeed.

    Validates: Requirement 4.2
    """
    es = _make_es_mock()
    doc = _job_doc(status="in_progress", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.CANCELLED)
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.status == JobStatus.CANCELLED


# ---------------------------------------------------------------------------
# Test: All invalid transitions return 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_transition_scheduled_to_completed():
    """scheduled → completed should fail with 400.

    Validates: Requirement 4.3
    """
    es = _make_es_mock()
    doc = _job_doc(status="scheduled")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.COMPLETED)
    with pytest.raises(AppException) as exc_info:
        await svc.transition_status("JOB_1", transition, "t1")

    assert exc_info.value.status_code == 400
    assert "Cannot transition" in exc_info.value.message


@pytest.mark.asyncio
async def test_invalid_transition_scheduled_to_in_progress():
    """scheduled → in_progress should fail with 400.

    Validates: Requirement 4.3
    """
    es = _make_es_mock()
    doc = _job_doc(status="scheduled")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.IN_PROGRESS)
    with pytest.raises(AppException) as exc_info:
        await svc.transition_status("JOB_1", transition, "t1")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_invalid_transition_scheduled_to_failed():
    """scheduled → failed should fail with 400.

    Validates: Requirement 4.3
    """
    es = _make_es_mock()
    doc = _job_doc(status="scheduled")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.FAILED, failure_reason="reason")
    with pytest.raises(AppException) as exc_info:
        await svc.transition_status("JOB_1", transition, "t1")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_invalid_transition_completed_to_in_progress():
    """completed → in_progress should fail with 400 (terminal state).

    Validates: Requirement 4.3
    """
    es = _make_es_mock()
    doc = _job_doc(status="completed", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.IN_PROGRESS)
    with pytest.raises(AppException) as exc_info:
        await svc.transition_status("JOB_1", transition, "t1")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_invalid_transition_cancelled_to_assigned():
    """cancelled → assigned should fail with 400 (terminal state).

    Validates: Requirement 4.3
    """
    es = _make_es_mock()
    doc = _job_doc(status="cancelled")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.ASSIGNED)
    with pytest.raises(AppException) as exc_info:
        await svc.transition_status("JOB_1", transition, "t1")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_invalid_transition_failed_to_completed():
    """failed → completed should fail with 400 (terminal state).

    Validates: Requirement 4.3
    """
    es = _make_es_mock()
    doc = _job_doc(status="failed", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.COMPLETED)
    with pytest.raises(AppException) as exc_info:
        await svc.transition_status("JOB_1", transition, "t1")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test: in_progress sets started_at and estimated_arrival
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_progress_sets_started_at_and_estimated_arrival():
    """Transitioning to in_progress should set started_at and estimated_arrival.

    estimated_arrival = scheduled_time + scheduling_default_eta_hours (4h).

    Validates: Requirement 4.4
    """
    es = _make_es_mock()
    scheduled_time = "2026-03-12T10:00:00+00:00"
    doc = _job_doc(
        status="assigned",
        asset_assigned="TRUCK_01",
        scheduled_time=scheduled_time,
    )
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.IN_PROGRESS)
    result = await svc.transition_status("JOB_1", transition, "t1")

    # Verify started_at is set
    assert result.started_at is not None

    # Verify estimated_arrival = scheduled_time + 4 hours
    expected_eta = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
    actual_eta = datetime.fromisoformat(result.estimated_arrival.replace("Z", "+00:00"))
    assert actual_eta == expected_eta

    # Verify update_document was called with started_at and estimated_arrival
    update_args = es.update_document.call_args[0][2]
    assert "started_at" in update_args
    assert "estimated_arrival" in update_args


@pytest.mark.asyncio
async def test_in_progress_without_asset_raises_400():
    """Transitioning to in_progress without an assigned asset should fail.

    Validates: Requirement 4.4
    """
    es = _make_es_mock()
    doc = _job_doc(status="assigned", asset_assigned=None)
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.IN_PROGRESS)
    with pytest.raises(AppException) as exc_info:
        await svc.transition_status("JOB_1", transition, "t1")

    assert exc_info.value.status_code == 400
    assert "no asset" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# Test: completed sets completed_at and records delay duration if delayed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_sets_completed_at():
    """Transitioning to completed should set completed_at.

    Validates: Requirement 4.5
    """
    es = _make_es_mock()
    doc = _job_doc(status="in_progress", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.COMPLETED)
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.completed_at is not None
    update_args = es.update_document.call_args[0][2]
    assert "completed_at" in update_args


@pytest.mark.asyncio
async def test_completed_records_delay_duration_if_delayed():
    """Completing a delayed job should record delay_duration_minutes.

    Validates: Requirement 4.5, 7.6
    """
    es = _make_es_mock()
    # Job is delayed with an estimated_arrival in the past
    past_eta = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    doc = _job_doc(
        status="in_progress",
        asset_assigned="TRUCK_01",
        delayed=True,
        estimated_arrival=past_eta,
    )
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.COMPLETED)
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.completed_at is not None
    # delay_duration_minutes should be recorded (approximately 120 minutes)
    update_args = es.update_document.call_args[0][2]
    assert "delay_duration_minutes" in update_args
    assert update_args["delay_duration_minutes"] >= 0


@pytest.mark.asyncio
async def test_completed_non_delayed_job_no_delay_duration():
    """Completing a non-delayed job should not record delay_duration_minutes.

    Validates: Requirement 4.5
    """
    es = _make_es_mock()
    doc = _job_doc(status="in_progress", asset_assigned="TRUCK_01", delayed=False)
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.COMPLETED)
    await svc.transition_status("JOB_1", transition, "t1")

    update_args = es.update_document.call_args[0][2]
    assert "delay_duration_minutes" not in update_args


# ---------------------------------------------------------------------------
# Test: failed requires failure_reason
# ---------------------------------------------------------------------------


def test_failed_without_failure_reason_raises_validation_error():
    """StatusTransition model should reject failed status without failure_reason.

    Validates: Requirement 4.6
    """
    with pytest.raises(ValidationError) as exc_info:
        StatusTransition(status=JobStatus.FAILED)

    assert "failure_reason" in str(exc_info.value)


@pytest.mark.asyncio
async def test_failed_with_failure_reason_succeeds():
    """Transitioning to failed with a failure_reason should succeed.

    Validates: Requirement 4.6
    """
    es = _make_es_mock()
    doc = _job_doc(status="in_progress", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.FAILED, failure_reason="Engine failure")
    result = await svc.transition_status("JOB_1", transition, "t1")

    assert result.status == JobStatus.FAILED
    update_args = es.update_document.call_args[0][2]
    assert update_args["failure_reason"] == "Engine failure"


# ---------------------------------------------------------------------------
# Test: Each transition appends status_changed event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_appends_status_changed_event():
    """Every status transition should append a status_changed event.

    Validates: Requirement 4.7
    """
    es = _make_es_mock()
    doc = _job_doc(status="assigned", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.IN_PROGRESS)
    await svc.transition_status("JOB_1", transition, "t1", actor_id="op_1")

    # index_document called once for the event
    assert es.index_document.await_count == 1
    event_call = es.index_document.await_args_list[0]
    assert event_call.args[0] == "job_events"
    event_doc = event_call.args[2]
    assert event_doc["event_type"] == "status_changed"
    assert event_doc["job_id"] == "JOB_1"
    assert event_doc["tenant_id"] == "t1"
    assert event_doc["actor_id"] == "op_1"
    assert event_doc["event_payload"]["old_status"] == "assigned"
    assert event_doc["event_payload"]["new_status"] == "in_progress"


@pytest.mark.asyncio
async def test_completed_transition_appends_event():
    """Completing a job should also append a status_changed event.

    Validates: Requirement 4.7
    """
    es = _make_es_mock()
    doc = _job_doc(status="in_progress", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.COMPLETED)
    await svc.transition_status("JOB_1", transition, "t1", actor_id="op_2")

    assert es.index_document.await_count == 1
    event_doc = es.index_document.await_args_list[0].args[2]
    assert event_doc["event_type"] == "status_changed"
    assert event_doc["event_payload"]["old_status"] == "in_progress"
    assert event_doc["event_payload"]["new_status"] == "completed"


@pytest.mark.asyncio
async def test_cancelled_transition_appends_event():
    """Cancelling a job should append a status_changed event.

    Validates: Requirement 4.7
    """
    es = _make_es_mock()
    doc = _job_doc(status="in_progress", asset_assigned="TRUCK_01")
    es.search_documents = AsyncMock(return_value=_job_hit(doc))
    svc = _make_service(es)

    transition = StatusTransition(status=JobStatus.CANCELLED)
    await svc.transition_status("JOB_1", transition, "t1", actor_id="op_3")

    assert es.index_document.await_count == 1
    event_doc = es.index_document.await_args_list[0].args[2]
    assert event_doc["event_type"] == "status_changed"
    assert event_doc["event_payload"]["old_status"] == "in_progress"
    assert event_doc["event_payload"]["new_status"] == "cancelled"
