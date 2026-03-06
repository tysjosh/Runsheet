"""
Unit tests for JobService - creation and validation.

Tests job creation with valid payloads, type-specific validation rules
(cargo manifest, asset compatibility), asset existence verification,
and event appending.

Requirements: 2.1-2.8
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from pydantic import ValidationError

from scheduling.models import (
    CargoItem,
    CreateJob,
    JobType,
    JobStatus,
    JOB_ASSET_COMPATIBILITY,
)
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
    return es


def _make_service(es_mock: MagicMock) -> JobService:
    """Create a JobService with mocked dependencies."""
    with patch("scheduling.services.job_service.get_settings") as mock_settings:
        settings_obj = MagicMock()
        settings_obj.scheduling_default_eta_hours = 4
        mock_settings.return_value = settings_obj
        svc = JobService(es_service=es_mock, redis_url=None)
    # Replace the id generator with a predictable mock
    svc._id_gen = MagicMock()
    svc._id_gen.next_id = AsyncMock(return_value="JOB_1")
    return svc


def _valid_cargo_payload() -> CreateJob:
    """Return a valid cargo_transport CreateJob with a manifest."""
    return CreateJob(
        job_type=JobType.CARGO_TRANSPORT,
        origin="Port Harcourt",
        destination="Lagos",
        scheduled_time="2026-03-12T10:00:00Z",
        cargo_manifest=[
            CargoItem(description="Steel pipes", weight_kg=500.0),
        ],
    )


def _valid_passenger_payload() -> CreateJob:
    """Return a valid passenger_transport CreateJob (no manifest needed)."""
    return CreateJob(
        job_type=JobType.PASSENGER_TRANSPORT,
        origin="Airport",
        destination="Hotel",
        scheduled_time="2026-03-12T08:00:00Z",
    )


# ---------------------------------------------------------------------------
# Test: job creation with valid payload returns JOB_{number} id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_returns_job_id_format():
    """A valid creation should return a Job with JOB_{number} id.

    Validates: Requirement 2.2
    """
    es = _make_es_mock()
    svc = _make_service(es)
    svc._id_gen.next_id = AsyncMock(return_value="JOB_42")

    job = await svc.create_job(_valid_cargo_payload(), tenant_id="tenant_a")

    assert job.job_id == "JOB_42"
    assert job.status == JobStatus.SCHEDULED
    assert job.tenant_id == "tenant_a"


@pytest.mark.asyncio
async def test_create_job_indexes_document():
    """create_job should index the document into jobs_current.

    Validates: Requirement 2.1
    """
    es = _make_es_mock()
    svc = _make_service(es)

    await svc.create_job(_valid_passenger_payload(), tenant_id="t1")

    # index_document called at least once for the job itself
    calls = es.index_document.await_args_list
    job_index_call = calls[0]
    assert job_index_call.args[0] == "jobs_current"
    assert job_index_call.args[1] == "JOB_1"
    doc = job_index_call.args[2]
    assert doc["job_type"] == "passenger_transport"
    assert doc["status"] == "scheduled"
    assert doc["tenant_id"] == "t1"


# ---------------------------------------------------------------------------
# Test: cargo_transport without cargo_manifest returns 400
# ---------------------------------------------------------------------------


def test_cargo_transport_without_manifest_raises_validation_error():
    """CreateJob model validator should reject cargo_transport with no manifest.

    Validates: Requirement 2.3
    """
    with pytest.raises(ValidationError) as exc_info:
        CreateJob(
            job_type=JobType.CARGO_TRANSPORT,
            origin="A",
            destination="B",
            scheduled_time="2026-01-01T00:00:00Z",
            cargo_manifest=None,
        )
    assert "cargo_transport jobs require at least one cargo manifest item" in str(
        exc_info.value
    )


def test_cargo_transport_with_empty_manifest_raises_validation_error():
    """CreateJob model validator should reject cargo_transport with empty list.

    Validates: Requirement 2.3
    """
    with pytest.raises(ValidationError) as exc_info:
        CreateJob(
            job_type=JobType.CARGO_TRANSPORT,
            origin="A",
            destination="B",
            scheduled_time="2026-01-01T00:00:00Z",
            cargo_manifest=[],
        )
    assert "cargo_transport jobs require at least one cargo manifest item" in str(
        exc_info.value
    )


# ---------------------------------------------------------------------------
# Test: vessel_movement with vehicle asset returns 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vessel_movement_with_vehicle_asset_raises_400():
    """Assigning a vehicle-type asset to a vessel_movement job should fail.

    Validates: Requirement 2.4
    """
    es = _make_es_mock()
    # Return a vehicle asset from the assets search
    es.search_documents = AsyncMock(
        return_value={
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "truck_id": "TRUCK_01",
                            "asset_type": "vehicle",
                        }
                    }
                ],
                "total": {"value": 1},
            }
        }
    )
    svc = _make_service(es)

    payload = CreateJob(
        job_type=JobType.VESSEL_MOVEMENT,
        origin="Dock A",
        destination="Dock B",
        scheduled_time="2026-03-12T10:00:00Z",
        asset_assigned="TRUCK_01",
    )

    with pytest.raises(AppException) as exc_info:
        await svc.create_job(payload, tenant_id="t1")

    assert exc_info.value.status_code == 400
    assert "not compatible" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test: crane_booking with non-crane equipment returns 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crane_booking_with_vehicle_asset_raises_400():
    """Assigning a vehicle-type asset to a crane_booking job should fail.

    Validates: Requirement 2.5
    """
    es = _make_es_mock()
    es.search_documents = AsyncMock(
        return_value={
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "truck_id": "VEH_01",
                            "asset_type": "vehicle",
                        }
                    }
                ],
                "total": {"value": 1},
            }
        }
    )
    svc = _make_service(es)

    payload = CreateJob(
        job_type=JobType.CRANE_BOOKING,
        origin="Yard A",
        destination="Yard B",
        scheduled_time="2026-03-12T10:00:00Z",
        asset_assigned="VEH_01",
    )

    with pytest.raises(AppException) as exc_info:
        await svc.create_job(payload, tenant_id="t1")

    assert exc_info.value.status_code == 400
    assert "not compatible" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test: creation with asset_assigned verifies asset exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_with_asset_verifies_asset_exists():
    """When asset_assigned is provided, the service must look it up.

    If the asset is not found, a 404 AppException should be raised.

    Validates: Requirement 2.6
    """
    es = _make_es_mock()
    # Return no hits — asset does not exist
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    svc = _make_service(es)

    payload = CreateJob(
        job_type=JobType.PASSENGER_TRANSPORT,
        origin="A",
        destination="B",
        scheduled_time="2026-03-12T10:00:00Z",
        asset_assigned="NONEXISTENT_ASSET",
    )

    with pytest.raises(AppException) as exc_info:
        await svc.create_job(payload, tenant_id="t1")

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_create_with_compatible_asset_succeeds():
    """When asset_assigned is a compatible asset, creation should succeed.

    Validates: Requirements 2.4, 2.6
    """
    es = _make_es_mock()

    # First search call: _verify_asset_compatible → return a vehicle asset
    # Second search call: _check_asset_availability → no conflicts
    es.search_documents = AsyncMock(
        side_effect=[
            {
                "hits": {
                    "hits": [
                        {"_source": {"truck_id": "TRUCK_01", "asset_type": "vehicle"}}
                    ],
                    "total": {"value": 1},
                }
            },
            {"hits": {"hits": [], "total": {"value": 0}}},
        ]
    )
    svc = _make_service(es)

    payload = CreateJob(
        job_type=JobType.PASSENGER_TRANSPORT,
        origin="A",
        destination="B",
        scheduled_time="2026-03-12T10:00:00Z",
        asset_assigned="TRUCK_01",
    )

    job = await svc.create_job(payload, tenant_id="t1")
    assert job.job_id == "JOB_1"
    assert job.asset_assigned == "TRUCK_01"


# ---------------------------------------------------------------------------
# Test: creation appends job_created event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_appends_job_created_event():
    """After indexing the job, a job_created event must be appended.

    Validates: Requirement 2.7
    """
    es = _make_es_mock()
    svc = _make_service(es)

    await svc.create_job(_valid_passenger_payload(), tenant_id="t1", actor_id="user_1")

    # index_document is called twice: once for the job, once for the event
    assert es.index_document.await_count == 2

    event_call = es.index_document.await_args_list[1]
    assert event_call.args[0] == "job_events"
    event_doc = event_call.args[2]
    assert event_doc["event_type"] == "job_created"
    assert event_doc["job_id"] == "JOB_1"
    assert event_doc["tenant_id"] == "t1"
    assert event_doc["actor_id"] == "user_1"
    assert "job" in event_doc["event_payload"]
