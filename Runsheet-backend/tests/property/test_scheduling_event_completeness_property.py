"""
Property-based tests for Event Append Completeness.

# Feature: logistics-scheduling, Property 4: Event Append Completeness

**Validates: Requirements 2.7, 3.5, 4.7, 6.4, 15.3**

For any sequence of mutations on a job (creation, assignment, status change,
cargo update), the services SHALL append exactly one event to the job_events
index per mutation. The total event count SHALL equal the number of mutations
performed.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis.strategies import lists, sampled_from

from scheduling.models import (
    CargoItem,
    CargoItemStatus,
    CreateJob,
    JobType,
    JobStatus,
    StatusTransition,
)
from scheduling.services.job_service import JobService
from scheduling.services.cargo_service import CargoService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JOB_EVENTS_INDEX = "job_events"
JOBS_CURRENT_INDEX = "jobs_current"
TENANT_ID = "tenant_test"
ACTOR_ID = "operator_1"

# Mutation types that the property test exercises
MUTATION_TYPES = ["create", "assign", "status_change", "cargo_update"]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_mutation_types = sampled_from(MUTATION_TYPES)
_mutation_sequences = lists(_mutation_types, min_size=1, max_size=5)


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
    # For CargoService painless script updates
    es.client = MagicMock()
    es.client.update = MagicMock(return_value={"result": "updated"})
    return es


def _make_job_service(es_mock: MagicMock) -> JobService:
    """Create a JobService with mocked dependencies."""
    with patch("scheduling.services.job_service.get_settings") as mock_settings:
        settings_obj = MagicMock()
        settings_obj.scheduling_default_eta_hours = 4
        mock_settings.return_value = settings_obj
        svc = JobService(es_service=es_mock, redis_url=None)
    svc._id_gen = MagicMock()
    svc._id_gen.next_id = AsyncMock(return_value="JOB_1")
    svc._ws_manager = None
    return svc


def _make_cargo_service(es_mock: MagicMock) -> CargoService:
    """Create a CargoService with mocked dependencies."""
    svc = CargoService(es_service=es_mock)
    svc._ws_manager = None
    return svc


def _count_job_events_calls(es_mock: MagicMock) -> int:
    """Count calls to index_document where the index is 'job_events'."""
    count = 0
    for call in es_mock.index_document.call_args_list:
        args, kwargs = call
        # index_document(index, doc_id, document)
        if args and args[0] == JOB_EVENTS_INDEX:
            count += 1
        elif kwargs.get("index") == JOB_EVENTS_INDEX:
            count += 1
    return count


def _build_scheduled_job_doc(job_id: str = "JOB_1") -> dict:
    """Build a minimal job document in 'scheduled' status."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "job_id": job_id,
        "job_type": "cargo_transport",
        "status": "scheduled",
        "tenant_id": TENANT_ID,
        "asset_assigned": None,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": "2026-03-12T10:00:00Z",
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
        "cargo_manifest": [
            {
                "item_id": "ITEM_001",
                "description": "Steel pipes",
                "weight_kg": 500.0,
                "container_number": None,
                "seal_number": None,
                "item_status": "pending",
            }
        ],
    }


def _build_assigned_job_doc(job_id: str = "JOB_1") -> dict:
    """Build a minimal job document in 'assigned' status with an asset."""
    doc = _build_scheduled_job_doc(job_id)
    doc["status"] = "assigned"
    doc["asset_assigned"] = "TRUCK_001"
    return doc


def _build_in_progress_job_doc(job_id: str = "JOB_1") -> dict:
    """Build a minimal job document in 'in_progress' status."""
    doc = _build_assigned_job_doc(job_id)
    doc["status"] = "in_progress"
    doc["started_at"] = datetime.now(timezone.utc).isoformat()
    doc["estimated_arrival"] = "2026-03-12T14:00:00Z"
    return doc


# ---------------------------------------------------------------------------
# Mutation executors
# ---------------------------------------------------------------------------

async def _execute_create(job_svc: JobService, es_mock: MagicMock) -> None:
    """Execute a create_job mutation."""
    payload = CreateJob(
        job_type=JobType.CARGO_TRANSPORT,
        origin="Port A",
        destination="Port B",
        scheduled_time="2026-03-12T10:00:00Z",
        cargo_manifest=[
            CargoItem(description="Steel pipes", weight_kg=500.0),
        ],
    )
    await job_svc.create_job(payload, tenant_id=TENANT_ID, actor_id=ACTOR_ID)


async def _execute_assign(job_svc: JobService, es_mock: MagicMock) -> None:
    """Execute an assign_asset mutation.

    Mocks the job as 'scheduled' and the asset as a compatible vehicle.
    """
    job_doc = _build_scheduled_job_doc()
    es_mock.search_documents = AsyncMock(side_effect=[
        # First call: _get_job_doc lookup
        {"hits": {"hits": [{"_source": job_doc}], "total": {"value": 1}}},
        # Second call: _verify_asset_compatible (asset lookup)
        {"hits": {"hits": [{"_source": {"asset_id": "TRUCK_001", "asset_type": "vehicle"}}], "total": {"value": 1}}},
        # Third call: _check_asset_availability (no conflicts)
        {"hits": {"hits": [], "total": {"value": 0}}},
    ])
    await job_svc.assign_asset(
        job_id="JOB_1", asset_id="TRUCK_001",
        tenant_id=TENANT_ID, actor_id=ACTOR_ID,
    )


async def _execute_status_change(job_svc: JobService, es_mock: MagicMock) -> None:
    """Execute a status transition mutation (assigned → in_progress).

    Mocks the job as 'assigned' with an asset.
    """
    job_doc = _build_assigned_job_doc()
    es_mock.search_documents = AsyncMock(return_value={
        "hits": {"hits": [{"_source": job_doc}], "total": {"value": 1}},
    })
    transition = StatusTransition(status=JobStatus.IN_PROGRESS)
    await job_svc.transition_status(
        job_id="JOB_1", transition=transition,
        tenant_id=TENANT_ID, actor_id=ACTOR_ID,
    )


async def _execute_cargo_update(cargo_svc: CargoService, es_mock: MagicMock) -> None:
    """Execute a cargo item status update mutation.

    Mocks the job as having a cargo manifest with a pending item.
    """
    job_doc = _build_in_progress_job_doc()
    es_mock.search_documents = AsyncMock(return_value={
        "hits": {"hits": [{"_source": job_doc}], "total": {"value": 1}},
    })
    await cargo_svc.update_cargo_item_status(
        job_id="JOB_1", item_id="ITEM_001",
        new_status=CargoItemStatus.LOADED,
        tenant_id=TENANT_ID, actor_id=ACTOR_ID,
    )


# ---------------------------------------------------------------------------
# Property 4 – Event Append Completeness
# ---------------------------------------------------------------------------
class TestEventAppendCompleteness:
    """**Validates: Requirements 2.7, 3.5, 4.7, 6.4, 15.3**"""

    @given(mutations=_mutation_sequences)
    @settings(max_examples=150)
    @pytest.mark.asyncio
    async def test_event_count_equals_mutation_count(self, mutations: list[str]):
        """
        For any sequence of mutations, the number of events appended to
        job_events (via index_document calls) SHALL equal the number of
        mutations performed.

        **Validates: Requirements 2.7, 3.5, 4.7, 6.4, 15.3**
        """
        es_mock = _make_es_mock()
        job_svc = _make_job_service(es_mock)
        cargo_svc = _make_cargo_service(es_mock)

        for mutation in mutations:
            # Reset search_documents before each mutation so side_effect
            # sequences are fresh
            if mutation == "create":
                es_mock.search_documents = AsyncMock(
                    return_value={
                        "hits": {"hits": [], "total": {"value": 0}},
                    }
                )
                await _execute_create(job_svc, es_mock)

            elif mutation == "assign":
                await _execute_assign(job_svc, es_mock)

            elif mutation == "status_change":
                await _execute_status_change(job_svc, es_mock)

            elif mutation == "cargo_update":
                await _execute_cargo_update(cargo_svc, es_mock)

        event_count = _count_job_events_calls(es_mock)
        assert event_count == len(mutations), (
            f"Expected {len(mutations)} events for mutations {mutations}, "
            f"but found {event_count} index_document calls to '{JOB_EVENTS_INDEX}'"
        )


# ---------------------------------------------------------------------------
# Property 4b – Each individual mutation appends exactly one event
# ---------------------------------------------------------------------------
class TestEachMutationAppendsOneEvent:
    """**Validates: Requirements 2.7, 3.5, 4.7, 6.4, 15.3**"""

    @given(mutation=_mutation_types)
    @settings(max_examples=150)
    @pytest.mark.asyncio
    async def test_single_mutation_appends_exactly_one_event(self, mutation: str):
        """
        For any single mutation type, exactly one event SHALL be appended
        to the job_events index.

        **Validates: Requirements 2.7, 3.5, 4.7, 6.4, 15.3**
        """
        es_mock = _make_es_mock()
        job_svc = _make_job_service(es_mock)
        cargo_svc = _make_cargo_service(es_mock)

        if mutation == "create":
            await _execute_create(job_svc, es_mock)
        elif mutation == "assign":
            await _execute_assign(job_svc, es_mock)
        elif mutation == "status_change":
            await _execute_status_change(job_svc, es_mock)
        elif mutation == "cargo_update":
            await _execute_cargo_update(cargo_svc, es_mock)

        event_count = _count_job_events_calls(es_mock)
        assert event_count == 1, (
            f"Mutation '{mutation}' should append exactly 1 event, "
            f"but {event_count} index_document calls to '{JOB_EVENTS_INDEX}' found"
        )
