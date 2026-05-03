"""
Unit tests for JobService - assignment and conflicts.

Tests asset assignment, incompatible asset type rejection, busy asset
conflict detection, reassignment with old/new asset logging, and
event appending for assignment operations.

Requirements: 3.1-3.6
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scheduling.models import JobStatus, JobType
from scheduling.services.job_service import JobService
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


def _scheduled_job_doc(
    job_id="JOB_1",
    job_type="cargo_transport",
    status="scheduled",
    tenant_id="t1",
    asset_assigned=None,
) -> dict:
    """Return a minimal job document in 'scheduled' status."""
    return {
        "job_id": job_id,
        "job_type": job_type,
        "status": status,
        "tenant_id": tenant_id,
        "asset_assigned": asset_assigned,
        "origin": "A",
        "destination": "B",
        "scheduled_time": "2026-03-12T10:00:00Z",
        "estimated_arrival": None,
        "started_at": None,
        "completed_at": None,
        "created_at": "2026-03-12T00:00:00Z",
        "updated_at": "2026-03-12T00:00:00Z",
        "created_by": None,
        "priority": "normal",
        "delayed": False,
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


def _vehicle_asset_hit(asset_id="TRUCK_01", asset_type="vehicle") -> dict:
    """Return an ES search response containing one asset."""
    return {
        "hits": {
            "hits": [{"_source": {"truck_id": asset_id, "asset_type": asset_type}}],
            "total": {"value": 1},
        }
    }


def _no_hits() -> dict:
    """Return an ES search response with zero hits."""
    return {"hits": {"hits": [], "total": {"value": 0}}}


def _job_hit(doc: dict) -> dict:
    """Wrap a job document in an ES search response."""
    return {
        "hits": {
            "hits": [{"_source": doc}],
            "total": {"value": 1},
        }
    }


# ---------------------------------------------------------------------------
# Test: assign_asset updates status to assigned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_asset_updates_status_to_assigned():
    """Assigning an asset to a scheduled job should set status to 'assigned'.

    Validates: Requirement 3.2
    """
    es = _make_es_mock()
    job_doc = _scheduled_job_doc()

    # Call 1: _get_job_doc → returns the scheduled job
    # Call 2: _verify_asset_compatible → returns a vehicle asset
    # Call 3: _check_asset_availability → no conflicts
    es.search_documents = AsyncMock(
        side_effect=[
            _job_hit(job_doc),
            _vehicle_asset_hit("TRUCK_01"),
            _no_hits(),
        ]
    )
    svc = _make_service(es)

    result = await svc.assign_asset("JOB_1", "TRUCK_01", "t1", actor_id="op_1")

    assert result.status == JobStatus.ASSIGNED
    assert result.asset_assigned == "TRUCK_01"

    # Verify update_document was called with status=assigned
    es.update_document.assert_awaited_once()
    update_args = es.update_document.call_args
    assert update_args[0][0] == "jobs_current"
    assert update_args[0][1] == "JOB_1"
    partial = update_args[0][2]
    assert partial["status"] == "assigned"
    assert partial["asset_assigned"] == "TRUCK_01"


# ---------------------------------------------------------------------------
# Test: assign with incompatible asset_type returns 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_incompatible_asset_type_returns_400():
    """Assigning a vessel asset to a cargo_transport job should fail with 400.

    Validates: Requirement 3.3
    """
    es = _make_es_mock()
    job_doc = _scheduled_job_doc(job_type="cargo_transport")

    # Call 1: _get_job_doc → returns the job
    # Call 2: _verify_asset_compatible → returns a vessel asset (incompatible)
    es.search_documents = AsyncMock(
        side_effect=[
            _job_hit(job_doc),
            _vehicle_asset_hit("VESSEL_01", asset_type="vessel"),
        ]
    )
    svc = _make_service(es)

    with pytest.raises(AppException) as exc_info:
        await svc.assign_asset("JOB_1", "VESSEL_01", "t1")

    assert exc_info.value.status_code == 400
    assert "not compatible" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test: assign with busy asset (overlapping active job) returns 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_busy_asset_returns_409():
    """Assigning an asset already on an active job should fail with 409.

    Validates: Requirement 3.4
    """
    es = _make_es_mock()
    job_doc = _scheduled_job_doc()

    conflicting_job = {
        "job_id": "JOB_99",
        "status": "in_progress",
        "asset_assigned": "TRUCK_01",
    }

    # Call 1: _get_job_doc → returns the scheduled job
    # Call 2: _verify_asset_compatible → returns a vehicle asset
    # Call 3: _check_asset_availability → finds a conflicting active job
    es.search_documents = AsyncMock(
        side_effect=[
            _job_hit(job_doc),
            _vehicle_asset_hit("TRUCK_01"),
            {
                "hits": {
                    "hits": [{"_source": conflicting_job}],
                    "total": {"value": 1},
                }
            },
        ]
    )
    svc = _make_service(es)

    with pytest.raises(AppException) as exc_info:
        await svc.assign_asset("JOB_1", "TRUCK_01", "t1")

    assert exc_info.value.status_code == 409
    assert "already assigned" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test: reassign_asset logs old and new asset_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassign_asset_logs_old_and_new_asset_ids():
    """Reassigning an asset should append an event with both old and new ids.

    Validates: Requirement 3.6
    """
    es = _make_es_mock()
    job_doc = _scheduled_job_doc(status="assigned", asset_assigned="TRUCK_01")

    # Call 1: _get_job_doc → returns the assigned job
    # Call 2: _verify_asset_compatible → returns the new vehicle asset
    # Call 3: _check_asset_availability → no conflicts
    es.search_documents = AsyncMock(
        side_effect=[
            _job_hit(job_doc),
            _vehicle_asset_hit("TRUCK_02"),
            _no_hits(),
        ]
    )
    svc = _make_service(es)

    result = await svc.reassign_asset("JOB_1", "TRUCK_02", "t1", actor_id="op_1")

    assert result.asset_assigned == "TRUCK_02"

    # The events should be appended via index_document on job_events:
    # 1. asset_reassigned event
    # 2. assignment_revoked event (Req 11.3)
    assert es.index_document.await_count == 2
    event_call = es.index_document.await_args_list[0]
    assert event_call.args[0] == "job_events"
    event_doc = event_call.args[2]
    assert event_doc["event_type"] == "asset_reassigned"
    assert event_doc["event_payload"]["old_asset_id"] == "TRUCK_01"
    assert event_doc["event_payload"]["new_asset_id"] == "TRUCK_02"

    # Verify the assignment_revoked event
    revoked_call = es.index_document.await_args_list[1]
    assert revoked_call.args[0] == "job_events"
    revoked_doc = revoked_call.args[2]
    assert revoked_doc["event_type"] == "assignment_revoked"
    assert revoked_doc["event_payload"]["previous_driver_id"] == "TRUCK_01"
    assert revoked_doc["event_payload"]["new_driver_id"] == "TRUCK_02"


# ---------------------------------------------------------------------------
# Test: assign appends asset_assigned event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_appends_asset_assigned_event():
    """After assigning an asset, an asset_assigned event must be appended.

    Validates: Requirement 3.5
    """
    es = _make_es_mock()
    job_doc = _scheduled_job_doc()

    es.search_documents = AsyncMock(
        side_effect=[
            _job_hit(job_doc),
            _vehicle_asset_hit("TRUCK_01"),
            _no_hits(),
        ]
    )
    svc = _make_service(es)

    await svc.assign_asset("JOB_1", "TRUCK_01", "t1", actor_id="op_1")

    # index_document called once for the event
    assert es.index_document.await_count == 1
    event_call = es.index_document.await_args_list[0]
    assert event_call.args[0] == "job_events"
    event_doc = event_call.args[2]
    assert event_doc["event_type"] == "asset_assigned"
    assert event_doc["job_id"] == "JOB_1"
    assert event_doc["tenant_id"] == "t1"
    assert event_doc["actor_id"] == "op_1"
    assert event_doc["event_payload"]["asset_id"] == "TRUCK_01"
