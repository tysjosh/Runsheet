"""
Property-based tests for Asset Scheduling Conflict Detection.

# Feature: logistics-scheduling, Property 3: Asset Scheduling Conflict Detection

**Validates: Requirements 2.6, 3.4**

For any asset assignment, the JobService SHALL reject the assignment with a
409 error if the asset is already assigned to another active job (status:
assigned or in_progress) with an overlapping time window. No asset SHALL be
double-booked.

This test generates job sequences where two jobs target the same asset with
overlapping time windows and verifies that the second assignment is rejected
with a 409 status code.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from hypothesis import given, settings, assume
from hypothesis.strategies import (
    sampled_from,
    text,
    integers,
    composite,
)

from errors.exceptions import AppException
from scheduling.models import (
    CreateJob,
    CargoItem,
    JobType,
    JobStatus,
    JOB_ASSET_COMPATIBILITY,
)
from scheduling.services.job_service import JobService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JOBS_CURRENT_INDEX = "jobs_current"
JOB_EVENTS_INDEX = "job_events"
TENANT_ID = "tenant_conflict_test"
ACTOR_ID = "operator_1"

# Active statuses that block asset assignment
ACTIVE_STATUSES = [JobStatus.ASSIGNED.value, JobStatus.IN_PROGRESS.value]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_all_job_types = sampled_from(list(JobType))
_active_statuses = sampled_from(ACTIVE_STATUSES)
_asset_ids = text(
    min_size=3, max_size=20,
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
)
# Hour offsets for scheduled_time to create overlapping windows
_hour_offsets = integers(min_value=0, max_value=72)


@composite
def job_type_with_compatible_asset(draw):
    """Draw a (job_type, asset_type) pair that is compatible."""
    job_type = draw(_all_job_types)
    compatible_types = JOB_ASSET_COMPATIBILITY[job_type]
    asset_type = draw(sampled_from(compatible_types))
    return job_type, asset_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es.get_document = AsyncMock(return_value=None)
    return es


def _make_job_service(es_mock: MagicMock) -> JobService:
    """Create a JobService with mocked dependencies."""
    with patch("scheduling.services.job_service.get_settings") as mock_settings:
        settings_obj = MagicMock()
        settings_obj.scheduling_default_eta_hours = 4
        mock_settings.return_value = settings_obj
        svc = JobService(es_service=es_mock, redis_url=None)
    svc._id_gen = MagicMock()
    svc._id_gen.next_id = AsyncMock(return_value="JOB_2")
    svc._ws_manager = None
    return svc


def _build_existing_active_job(
    job_id: str,
    asset_id: str,
    job_type: str,
    status: str,
    scheduled_time: str,
) -> dict:
    """Build a job document representing an existing active job."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "job_id": job_id,
        "job_type": job_type,
        "status": status,
        "tenant_id": TENANT_ID,
        "asset_assigned": asset_id,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": scheduled_time,
        "estimated_arrival": None,
        "started_at": now if status == "in_progress" else None,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
        "created_by": ACTOR_ID,
        "priority": "normal",
        "delayed": False,
        "delay_duration_minutes": None,
        "failure_reason": None,
        "notes": None,
        "cargo_manifest": None,
    }


def _build_scheduled_job(
    job_id: str,
    job_type: str,
    scheduled_time: str,
) -> dict:
    """Build a job document in 'scheduled' status (no asset yet)."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "job_id": job_id,
        "job_type": job_type,
        "status": "scheduled",
        "tenant_id": TENANT_ID,
        "asset_assigned": None,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": scheduled_time,
        "estimated_arrival": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
        "created_by": ACTOR_ID,
        "priority": "normal",
        "delayed": False,
        "delay_duration_minutes": None,
        "failure_reason": None,
        "notes": None,
        "cargo_manifest": None,
    }


# ---------------------------------------------------------------------------
# Property 3 – Asset Scheduling Conflict Detection via assign_asset
# ---------------------------------------------------------------------------
class TestAssetConflictOnAssign:
    """**Validates: Requirements 2.6, 3.4**"""

    @given(
        job_type_asset=job_type_with_compatible_asset(),
        existing_status=_active_statuses,
        hour_offset=_hour_offsets,
    )
    @settings(max_examples=100)
    @pytest.mark.asyncio
    async def test_second_assignment_rejected_with_409(
        self,
        job_type_asset: tuple,
        existing_status: str,
        hour_offset: int,
    ):
        """
        When an asset is already assigned to an active job (assigned or
        in_progress), assigning the same asset to a second job SHALL be
        rejected with a 409 status code.

        **Validates: Requirements 2.6, 3.4**
        """
        job_type, asset_type = job_type_asset
        asset_id = f"ASSET_{asset_type.upper()}_001"
        scheduled_time = f"2026-03-{12 + (hour_offset % 28):02d}T{hour_offset % 24:02d}:00:00Z"

        # Build the existing active job that already has this asset
        existing_job = _build_existing_active_job(
            job_id="JOB_1",
            asset_id=asset_id,
            job_type=job_type.value,
            status=existing_status,
            scheduled_time=scheduled_time,
        )

        # Build the new scheduled job that wants the same asset
        new_job = _build_scheduled_job(
            job_id="JOB_2",
            job_type=job_type.value,
            scheduled_time=scheduled_time,
        )

        es_mock = _make_es_mock()
        job_svc = _make_job_service(es_mock)

        # Mock the sequence of ES calls for assign_asset:
        # 1. _get_job_doc: returns the new scheduled job
        # 2. _verify_asset_compatible: returns the asset with compatible type
        # 3. _check_asset_availability: returns the conflicting active job
        es_mock.search_documents = AsyncMock(side_effect=[
            # _get_job_doc lookup for JOB_2
            {"hits": {"hits": [{"_source": new_job}], "total": {"value": 1}}},
            # _verify_asset_compatible: asset exists with compatible type
            {"hits": {"hits": [{"_source": {"asset_id": asset_id, "asset_type": asset_type}}], "total": {"value": 1}}},
            # _check_asset_availability: conflicting active job found
            {"hits": {"hits": [{"_source": existing_job}], "total": {"value": 1}}},
        ])

        with pytest.raises(AppException) as exc_info:
            await job_svc.assign_asset(
                job_id="JOB_2",
                asset_id=asset_id,
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
            )

        assert exc_info.value.status_code == 409, (
            f"Expected 409 conflict when asset '{asset_id}' is already "
            f"{existing_status} on JOB_1, but got status "
            f"{exc_info.value.status_code}"
        )


# ---------------------------------------------------------------------------
# Property 3b – Asset Scheduling Conflict Detection via create_job
# ---------------------------------------------------------------------------
class TestAssetConflictOnCreate:
    """**Validates: Requirements 2.6, 3.4**"""

    @given(
        job_type_asset=job_type_with_compatible_asset(),
        existing_status=_active_statuses,
        hour_offset=_hour_offsets,
    )
    @settings(max_examples=100)
    @pytest.mark.asyncio
    async def test_create_with_busy_asset_rejected_with_409(
        self,
        job_type_asset: tuple,
        existing_status: str,
        hour_offset: int,
    ):
        """
        When creating a job with an asset_assigned that is already busy
        on another active job, the creation SHALL be rejected with a 409
        status code.

        **Validates: Requirements 2.6, 3.4**
        """
        job_type, asset_type = job_type_asset
        asset_id = f"ASSET_{asset_type.upper()}_001"
        scheduled_time = f"2026-03-{12 + (hour_offset % 28):02d}T{hour_offset % 24:02d}:00:00Z"

        # Build the existing active job that already has this asset
        existing_job = _build_existing_active_job(
            job_id="JOB_1",
            asset_id=asset_id,
            job_type=job_type.value,
            status=existing_status,
            scheduled_time=scheduled_time,
        )

        es_mock = _make_es_mock()
        job_svc = _make_job_service(es_mock)

        # Mock the sequence of ES calls for create_job with asset_assigned:
        # 1. _verify_asset_compatible: returns the asset with compatible type
        # 2. _check_asset_availability: returns the conflicting active job
        es_mock.search_documents = AsyncMock(side_effect=[
            # _verify_asset_compatible: asset exists with compatible type
            {"hits": {"hits": [{"_source": {"asset_id": asset_id, "asset_type": asset_type}}], "total": {"value": 1}}},
            # _check_asset_availability: conflicting active job found
            {"hits": {"hits": [{"_source": existing_job}], "total": {"value": 1}}},
        ])

        # Build a CreateJob payload with the busy asset
        cargo_manifest = None
        if job_type == JobType.CARGO_TRANSPORT:
            cargo_manifest = [
                CargoItem(description="Test cargo", weight_kg=100.0),
            ]

        payload = CreateJob(
            job_type=job_type,
            origin="Port A",
            destination="Port B",
            scheduled_time=scheduled_time,
            asset_assigned=asset_id,
            cargo_manifest=cargo_manifest,
        )

        with pytest.raises(AppException) as exc_info:
            await job_svc.create_job(
                data=payload,
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
            )

        assert exc_info.value.status_code == 409, (
            f"Expected 409 conflict when creating job with asset '{asset_id}' "
            f"already {existing_status} on JOB_1, but got status "
            f"{exc_info.value.status_code}"
        )


# ---------------------------------------------------------------------------
# Property 3c – No conflict when asset has no active jobs
# ---------------------------------------------------------------------------
class TestNoConflictWhenAssetFree:
    """**Validates: Requirements 2.6, 3.4**"""

    @given(
        job_type_asset=job_type_with_compatible_asset(),
        hour_offset=_hour_offsets,
    )
    @settings(max_examples=100)
    @pytest.mark.asyncio
    async def test_assignment_succeeds_when_asset_is_free(
        self,
        job_type_asset: tuple,
        hour_offset: int,
    ):
        """
        When an asset has no overlapping active jobs, assigning it to a
        scheduled job SHALL succeed (no 409 raised).

        **Validates: Requirements 2.6, 3.4**
        """
        job_type, asset_type = job_type_asset
        asset_id = f"ASSET_{asset_type.upper()}_002"
        scheduled_time = f"2026-03-{12 + (hour_offset % 28):02d}T{hour_offset % 24:02d}:00:00Z"

        new_job = _build_scheduled_job(
            job_id="JOB_2",
            job_type=job_type.value,
            scheduled_time=scheduled_time,
        )

        es_mock = _make_es_mock()
        job_svc = _make_job_service(es_mock)

        # Mock the sequence of ES calls for assign_asset:
        # 1. _get_job_doc: returns the scheduled job
        # 2. _verify_asset_compatible: returns the asset with compatible type
        # 3. _check_asset_availability: no conflicting jobs
        es_mock.search_documents = AsyncMock(side_effect=[
            # _get_job_doc lookup
            {"hits": {"hits": [{"_source": new_job}], "total": {"value": 1}}},
            # _verify_asset_compatible: asset exists
            {"hits": {"hits": [{"_source": {"asset_id": asset_id, "asset_type": asset_type}}], "total": {"value": 1}}},
            # _check_asset_availability: no conflicts
            {"hits": {"hits": [], "total": {"value": 0}}},
        ])

        # Should NOT raise — asset is free
        result = await job_svc.assign_asset(
            job_id="JOB_2",
            asset_id=asset_id,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

        assert result.status == JobStatus.ASSIGNED, (
            f"Expected job status to be 'assigned' after successful "
            f"assignment, got '{result.status}'"
        )
        assert result.asset_assigned == asset_id, (
            f"Expected asset_assigned to be '{asset_id}', "
            f"got '{result.asset_assigned}'"
        )
