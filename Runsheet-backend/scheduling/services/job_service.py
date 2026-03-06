"""
Core job service handling job creation, assignment, status transitions, and queries.

Manages the full job lifecycle for the Logistics Scheduling module, including
asset compatibility verification, availability checks, event logging, and
WebSocket broadcast integration.

Requirements covered:
- 2.1-2.8: Job creation and validation
- 3.1-3.6: Job assignment and dispatch
- 4.1-4.8: Job status progression and lifecycle
- 5.1-5.7: Job query and filtering
- 8.5: Tenant-scoped job access
- 15.1, 15.3, 15.4: Event append and audit trail
"""

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.settings import get_settings
from errors.exceptions import (
    AppException,
    resource_not_found,
    validation_error,
)
from errors.codes import ErrorCode
from scheduling.models import (
    CargoItem,
    CreateJob,
    Job,
    JobEvent,
    JobStatus,
    JobType,
    StatusTransition,
    JOB_ASSET_COMPATIBILITY,
    VALID_TRANSITIONS,
)
from scheduling.services.job_id_generator import JobIdGenerator
from scheduling.services.scheduling_es_mappings import (
    JOBS_CURRENT_INDEX,
    JOB_EVENTS_INDEX,
)
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class JobService:
    """Manages job lifecycle: creation, assignment, status transitions, and queries.

    Validates: Requirements 2.1-2.8, 3.1-3.6, 4.1-4.8, 5.1-5.7, 8.5, 15.1-15.4
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        redis_url: Optional[str] = None,
    ):
        self._es = es_service
        self._id_gen = JobIdGenerator(redis_url)
        self._settings = get_settings()
        self._ws_manager = None  # Wired in task 8.3

    # ------------------------------------------------------------------
    # Job Creation  (Requirements 2.1-2.8, 8.5)
    # ------------------------------------------------------------------

    async def create_job(
        self,
        data: CreateJob,
        tenant_id: str,
        actor_id: Optional[str] = None,
    ) -> Job:
        """Create a new logistics job.

        - Generates a sequential JOB_{n} id via Redis INCR.
        - Validates asset compatibility and availability when asset_assigned
          is provided.
        - Auto-generates item_id for cargo manifest items missing one.
        - Indexes the document into jobs_current.
        - Appends a ``job_created`` event to job_events.

        Args:
            data: Validated CreateJob payload.
            tenant_id: Tenant extracted from the authenticated JWT context.
            actor_id: Optional user/operator performing the action.

        Returns:
            The created Job model.

        Raises:
            AppException: On validation failures (400) or conflicts (409).
        """
        # --- Asset verification (if provided) ---
        if data.asset_assigned:
            await self._verify_asset_compatible(data.asset_assigned, data.job_type)
            await self._check_asset_availability(
                data.asset_assigned, data.scheduled_time, tenant_id
            )

        # --- Generate ID ---
        job_id = await self._id_gen.next_id()

        now = datetime.now(timezone.utc).isoformat()

        # --- Prepare cargo manifest ---
        cargo_manifest = None
        if data.cargo_manifest:
            cargo_manifest = []
            for item in data.cargo_manifest:
                item_dict = item.model_dump()
                if not item_dict.get("item_id"):
                    item_dict["item_id"] = f"ITEM_{uuid.uuid4().hex[:8]}"
                cargo_manifest.append(item_dict)

        # --- Build document ---
        doc: dict = {
            "job_id": job_id,
            "job_type": data.job_type.value,
            "status": JobStatus.SCHEDULED.value,
            "tenant_id": tenant_id,
            "asset_assigned": data.asset_assigned,
            "origin": data.origin,
            "destination": data.destination,
            "scheduled_time": data.scheduled_time,
            "estimated_arrival": None,
            "started_at": None,
            "completed_at": None,
            "created_at": now,
            "updated_at": now,
            "created_by": data.created_by or actor_id,
            "priority": data.priority.value,
            "delayed": False,
            "delay_duration_minutes": None,
            "failure_reason": None,
            "notes": data.notes,
            "cargo_manifest": cargo_manifest,
        }

        # Optional geo-points
        if data.origin_location:
            doc["origin_location"] = data.origin_location.model_dump()
        if data.destination_location:
            doc["destination_location"] = data.destination_location.model_dump()

        # --- Index into jobs_current ---
        await self._es.index_document(JOBS_CURRENT_INDEX, job_id, doc)

        # --- Append event ---
        await self._append_event(
            job_id=job_id,
            event_type="job_created",
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload={"job": doc},
        )

        # --- Broadcast ---
        await self._broadcast_job_update("job_created", doc)

        return Job(**doc)

    # ------------------------------------------------------------------
    # Internal: fetch job document  (reused by assign, reassign, etc.)
    # ------------------------------------------------------------------

    async def _get_job_doc(self, job_id: str, tenant_id: str) -> dict:
        """Fetch a raw job document from jobs_current by job_id with tenant filter.

        Args:
            job_id: The job identifier.
            tenant_id: Tenant scope extracted from JWT.

        Returns:
            The raw Elasticsearch document ``_source`` dict.

        Raises:
            AppException: 404 if the job is not found for this tenant.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"job_id": job_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }

        response = await self._es.search_documents(JOBS_CURRENT_INDEX, query, size=1)
        hits = response["hits"]["hits"]

        if not hits:
            raise resource_not_found(
                f"Job '{job_id}' not found",
                details={"job_id": job_id},
            )

        return hits[0]["_source"]

    # ------------------------------------------------------------------
    # Assignment  (Requirements 3.1-3.6)
    # ------------------------------------------------------------------

    async def assign_asset(
        self,
        job_id: str,
        asset_id: str,
        tenant_id: str,
        actor_id: Optional[str] = None,
    ) -> Job:
        """Assign an asset to a scheduled job.

        Validates: Requirements 3.1-3.5
        - Fetches the job and verifies its status is ``scheduled``.
        - Verifies the asset exists and its type is compatible with the job type.
        - Checks the asset is not already assigned to another active job.
        - Updates the job status to ``assigned`` and sets ``asset_assigned``.
        - Appends an ``asset_assigned`` event.

        Args:
            job_id: The job to assign an asset to.
            asset_id: The asset to assign.
            tenant_id: Tenant scope from JWT.
            actor_id: The operator performing the assignment.

        Returns:
            The updated Job model.

        Raises:
            AppException: 400 if job status is not ``scheduled`` or asset is
                incompatible; 404 if job or asset not found; 409 if asset is
                already busy.
        """
        # Fetch job and verify status
        job_doc = await self._get_job_doc(job_id, tenant_id)

        if job_doc["status"] != JobStatus.SCHEDULED.value:
            raise validation_error(
                f"Cannot assign asset to job '{job_id}': current status is "
                f"'{job_doc['status']}', expected 'scheduled'",
                details={
                    "job_id": job_id,
                    "current_status": job_doc["status"],
                    "expected_status": JobStatus.SCHEDULED.value,
                },
            )

        # Verify asset exists and is compatible
        job_type = JobType(job_doc["job_type"])
        await self._verify_asset_compatible(asset_id, job_type)

        # Check asset availability (no overlapping active jobs)
        await self._check_asset_availability(
            asset_id, job_doc["scheduled_time"], tenant_id
        )

        # Update job document
        now = datetime.now(timezone.utc).isoformat()
        update_fields = {
            "status": JobStatus.ASSIGNED.value,
            "asset_assigned": asset_id,
            "updated_at": now,
        }

        await self._es.update_document(JOBS_CURRENT_INDEX, job_id, update_fields)

        # Append event
        await self._append_event(
            job_id=job_id,
            event_type="asset_assigned",
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload={
                "asset_id": asset_id,
                "job_id": job_id,
            },
        )

        # Merge updates into doc for return / broadcast
        job_doc.update(update_fields)
        await self._broadcast_job_update("status_changed", job_doc)

        return Job(**job_doc)

    async def reassign_asset(
        self,
        job_id: str,
        new_asset_id: str,
        tenant_id: str,
        actor_id: Optional[str] = None,
    ) -> Job:
        """Change the assigned asset on an active job.

        Validates: Requirement 3.6
        - Verifies the job status is ``assigned`` or ``in_progress``.
        - Verifies the new asset is compatible and available.
        - Updates ``asset_assigned`` and appends an ``asset_reassigned`` event
          recording both old and new asset ids.

        Args:
            job_id: The job to reassign.
            new_asset_id: The replacement asset.
            tenant_id: Tenant scope from JWT.
            actor_id: The operator performing the reassignment.

        Returns:
            The updated Job model.

        Raises:
            AppException: 400 if job status is invalid or asset incompatible;
                404 if job or asset not found; 409 if new asset is busy.
        """
        # Fetch job and verify status
        job_doc = await self._get_job_doc(job_id, tenant_id)

        allowed_statuses = {JobStatus.ASSIGNED.value, JobStatus.IN_PROGRESS.value}
        if job_doc["status"] not in allowed_statuses:
            raise validation_error(
                f"Cannot reassign asset on job '{job_id}': current status is "
                f"'{job_doc['status']}', expected one of {sorted(allowed_statuses)}",
                details={
                    "job_id": job_id,
                    "current_status": job_doc["status"],
                    "allowed_statuses": sorted(allowed_statuses),
                },
            )

        # Verify new asset is compatible
        job_type = JobType(job_doc["job_type"])
        await self._verify_asset_compatible(new_asset_id, job_type)

        # Check new asset availability (exclude current job from conflict check)
        await self._check_asset_availability(
            new_asset_id, job_doc["scheduled_time"], tenant_id, exclude_job_id=job_id
        )

        old_asset_id = job_doc.get("asset_assigned")

        # Update job document
        now = datetime.now(timezone.utc).isoformat()
        update_fields = {
            "asset_assigned": new_asset_id,
            "updated_at": now,
        }

        await self._es.update_document(JOBS_CURRENT_INDEX, job_id, update_fields)

        # Append event with old and new asset ids
        await self._append_event(
            job_id=job_id,
            event_type="asset_reassigned",
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload={
                "job_id": job_id,
                "old_asset_id": old_asset_id,
                "new_asset_id": new_asset_id,
            },
        )

        # Merge updates into doc for return / broadcast
        job_doc.update(update_fields)
        await self._broadcast_job_update("status_changed", job_doc)

        return Job(**job_doc)

    # ------------------------------------------------------------------
    # Status Transitions  (Requirements 4.1-4.8)
    # ------------------------------------------------------------------

    async def transition_status(
        self,
        job_id: str,
        transition: StatusTransition,
        tenant_id: str,
        actor_id: Optional[str] = None,
    ) -> Job:
        """Transition a job to a new status.

        Validates: Requirements 4.1-4.8
        - Validates the transition against VALID_TRANSITIONS.
        - For ``in_progress``: verifies an asset is assigned, sets
          ``started_at`` and calculates ``estimated_arrival``.
        - For ``completed``: sets ``completed_at`` and records
          ``delay_duration_minutes`` if the job was delayed.
        - For ``failed``: requires ``failure_reason`` (enforced by model
          validator, but double-checked here).
        - For ``cancelled`` or ``failed``: asset release is a no-op for MVP
          since availability is query-based.
        - Appends a ``status_changed`` event with old/new status and actor.

        Args:
            job_id: The job to transition.
            transition: StatusTransition payload with target status and
                optional failure_reason.
            tenant_id: Tenant scope from JWT.
            actor_id: The operator performing the transition.

        Returns:
            The updated Job model.

        Raises:
            AppException: 400 if the transition is invalid, asset is not
                assigned for in_progress, or failure_reason is missing for
                failed; 404 if job not found.
        """
        # Fetch job
        job_doc = await self._get_job_doc(job_id, tenant_id)

        current_status = JobStatus(job_doc["status"])
        target_status = transition.status

        # Validate transition
        allowed = VALID_TRANSITIONS.get(current_status, [])
        if target_status not in allowed:
            raise validation_error(
                f"Cannot transition job '{job_id}' from '{current_status.value}' "
                f"to '{target_status.value}'",
                details={
                    "job_id": job_id,
                    "current_status": current_status.value,
                    "target_status": target_status.value,
                    "allowed_transitions": [s.value for s in allowed],
                },
            )

        now = datetime.now(timezone.utc).isoformat()
        update_fields: dict = {
            "status": target_status.value,
            "updated_at": now,
        }

        # --- in_progress: verify asset assigned, set started_at, calculate ETA ---
        if target_status == JobStatus.IN_PROGRESS:
            if not job_doc.get("asset_assigned"):
                raise validation_error(
                    f"Cannot start job '{job_id}': no asset is assigned",
                    details={
                        "job_id": job_id,
                        "target_status": target_status.value,
                    },
                )
            update_fields["started_at"] = now

            # Calculate estimated_arrival = scheduled_time + default ETA hours
            try:
                scheduled_dt = datetime.fromisoformat(
                    job_doc["scheduled_time"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                scheduled_dt = datetime.now(timezone.utc)

            eta_hours = self._settings.scheduling_default_eta_hours
            estimated_arrival = (scheduled_dt + timedelta(hours=eta_hours)).isoformat()
            update_fields["estimated_arrival"] = estimated_arrival

        # --- completed: set completed_at, record delay if applicable ---
        if target_status == JobStatus.COMPLETED:
            update_fields["completed_at"] = now

            if job_doc.get("delayed"):
                # Calculate delay_duration_minutes from estimated_arrival to now
                estimated_arrival_str = job_doc.get("estimated_arrival")
                if estimated_arrival_str:
                    try:
                        eta_dt = datetime.fromisoformat(
                            estimated_arrival_str.replace("Z", "+00:00")
                        )
                        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
                        delay_minutes = int(
                            (now_dt - eta_dt).total_seconds() / 60
                        )
                        update_fields["delay_duration_minutes"] = max(delay_minutes, 0)
                    except (ValueError, TypeError):
                        logger.warning(
                            "Could not calculate delay duration for job %s",
                            job_id,
                        )

        # --- failed: require failure_reason ---
        if target_status == JobStatus.FAILED:
            if not transition.failure_reason:
                raise validation_error(
                    f"failure_reason is required when transitioning job "
                    f"'{job_id}' to 'failed'",
                    details={
                        "job_id": job_id,
                        "target_status": target_status.value,
                    },
                )
            update_fields["failure_reason"] = transition.failure_reason

        # --- cancelled or failed: release asset (no-op for MVP) ---
        # Asset availability is determined by querying active jobs, so
        # changing status away from assigned/in_progress is sufficient.

        # Update job document
        await self._es.update_document(JOBS_CURRENT_INDEX, job_id, update_fields)

        # Append status_changed event
        await self._append_event(
            job_id=job_id,
            event_type="status_changed",
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload={
                "old_status": current_status.value,
                "new_status": target_status.value,
                "actor_id": actor_id,
            },
        )

        # Merge updates and broadcast
        job_doc.update(update_fields)
        await self._broadcast_job_update("status_changed", job_doc)

        return Job(**job_doc)

    # ------------------------------------------------------------------
    # Query Methods  (Requirements 5.1-5.7, 15.2)
    # ------------------------------------------------------------------

    async def get_job(self, job_id: str, tenant_id: str) -> dict:
        """Fetch a single job with its full event history.

        Validates: Requirement 5.3
        - Fetches the job document from jobs_current with tenant filter.
        - Queries job_events for the complete event timeline.

        Args:
            job_id: The job identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            Dict with ``job`` (Job model) and ``events`` (list of JobEvent).

        Raises:
            AppException: 404 if job not found for this tenant.
        """
        job_doc = await self._get_job_doc(job_id, tenant_id)
        events = await self.get_job_events(job_id, tenant_id)

        return {
            "job": Job(**job_doc),
            "events": events,
        }

    async def list_jobs(
        self,
        tenant_id: str,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
        asset_assigned: Optional[str] = None,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "scheduled_time",
        sort_order: str = "asc",
    ) -> dict:
        """Paginated job listing with filters.

        Validates: Requirements 5.1, 5.2, 5.6, 5.7

        Args:
            tenant_id: Tenant scope from JWT.
            job_type: Filter by job type enum value.
            status: Filter by job status enum value.
            asset_assigned: Filter by assigned asset id.
            origin: Filter by origin (keyword match).
            destination: Filter by destination (keyword match).
            start_date: Filter scheduled_time >= start_date (ISO 8601).
            end_date: Filter scheduled_time <= end_date (ISO 8601).
            page: Page number (1-based).
            size: Page size.
            sort_by: Field to sort by.
            sort_order: ``asc`` or ``desc``.

        Returns:
            Dict with ``data`` (list of Job dicts) and ``pagination`` envelope.

        Raises:
            AppException: 400 for invalid filter values.
        """
        # Validate filter values
        if job_type is not None:
            valid_types = [jt.value for jt in JobType]
            if job_type not in valid_types:
                raise validation_error(
                    f"Invalid job_type filter: '{job_type}'",
                    details={"job_type": job_type, "valid_values": valid_types},
                )

        if status is not None:
            valid_statuses = [js.value for js in JobStatus]
            if status not in valid_statuses:
                raise validation_error(
                    f"Invalid status filter: '{status}'",
                    details={"status": status, "valid_values": valid_statuses},
                )

        if sort_order not in ("asc", "desc"):
            raise validation_error(
                f"Invalid sort_order: '{sort_order}'",
                details={"sort_order": sort_order, "valid_values": ["asc", "desc"]},
            )

        # Build query
        must_clauses: list[dict] = [
            {"term": {"tenant_id": tenant_id}},
        ]

        if job_type is not None:
            must_clauses.append({"term": {"job_type": job_type}})
        if status is not None:
            must_clauses.append({"term": {"status": status}})
        if asset_assigned is not None:
            must_clauses.append({"term": {"asset_assigned": asset_assigned}})
        if origin is not None:
            must_clauses.append({"term": {"origin.keyword": origin}})
        if destination is not None:
            must_clauses.append({"term": {"destination.keyword": destination}})

        # Date range filter on scheduled_time
        if start_date is not None or end_date is not None:
            date_range: dict = {}
            if start_date is not None:
                date_range["gte"] = start_date
            if end_date is not None:
                date_range["lte"] = end_date
            must_clauses.append({"range": {"scheduled_time": date_range}})

        from_offset = (page - 1) * size

        query: dict = {
            "query": {"bool": {"must": must_clauses}},
            "sort": [{sort_by: {"order": sort_order}}],
            "from": from_offset,
            "size": size,
            "track_total_hits": True,
        }

        response = await self._es.search_documents(
            JOBS_CURRENT_INDEX, query, size=size
        )

        hits = response["hits"]["hits"]
        total = response["hits"]["total"]["value"]
        total_pages = math.ceil(total / size) if size > 0 else 0

        data = [hit["_source"] for hit in hits]

        return {
            "data": data,
            "pagination": {
                "page": page,
                "size": size,
                "total": total,
                "total_pages": total_pages,
            },
        }

    async def get_active_jobs(self, tenant_id: str) -> list[dict]:
        """Return jobs with status in (scheduled, assigned, in_progress).

        Validates: Requirement 5.4
        Sorted by scheduled_time ascending.

        Args:
            tenant_id: Tenant scope from JWT.

        Returns:
            List of job source dicts.
        """
        active_statuses = [
            JobStatus.SCHEDULED.value,
            JobStatus.ASSIGNED.value,
            JobStatus.IN_PROGRESS.value,
        ]

        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": active_statuses}},
                    ]
                }
            },
            "sort": [{"scheduled_time": {"order": "asc"}}],
            "size": 1000,
        }

        response = await self._es.search_documents(
            JOBS_CURRENT_INDEX, query, size=1000
        )

        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def get_delayed_jobs(self, tenant_id: str) -> list[dict]:
        """Return in-progress jobs that are delayed.

        Validates: Requirement 5.5
        Queries status=in_progress AND delayed=true.

        Args:
            tenant_id: Tenant scope from JWT.

        Returns:
            List of delayed job source dicts.
        """
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"status": JobStatus.IN_PROGRESS.value}},
                        {"term": {"delayed": True}},
                    ]
                }
            },
            "sort": [{"scheduled_time": {"order": "asc"}}],
            "size": 1000,
        }

        response = await self._es.search_documents(
            JOBS_CURRENT_INDEX, query, size=1000
        )

        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def get_job_events(
        self, job_id: str, tenant_id: str
    ) -> list[JobEvent]:
        """Return the full event timeline for a job.

        Validates: Requirement 15.2
        Sorted by event_timestamp ascending.

        Args:
            job_id: The job identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            List of JobEvent models sorted chronologically.
        """
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"job_id": job_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "sort": [{"event_timestamp": {"order": "asc"}}],
            "size": 1000,
        }

        response = await self._es.search_documents(
            JOB_EVENTS_INDEX, query, size=1000
        )

        return [
            JobEvent(**hit["_source"])
            for hit in response["hits"]["hits"]
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _verify_asset_compatible(
        self, asset_id: str, job_type: JobType
    ) -> dict:
        """Check that the asset exists and its asset_type is compatible.

        Queries the ``assets`` alias (backed by the ``trucks`` index) for the
        given asset_id and verifies that the asset's ``asset_type`` is listed
        in ``JOB_ASSET_COMPATIBILITY[job_type]``.

        Args:
            asset_id: The asset identifier to look up.
            job_type: The job type requiring a compatible asset.

        Returns:
            The asset document dict from Elasticsearch.

        Raises:
            AppException: 404 if asset not found, 400 if incompatible type.
        """
        query = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"truck_id": asset_id}},
                        {"term": {"asset_id": asset_id}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "size": 1,
        }

        response = await self._es.search_documents("trucks", query, size=1)
        hits = response["hits"]["hits"]

        if not hits:
            raise resource_not_found(
                f"Asset '{asset_id}' not found",
                details={"asset_id": asset_id},
            )

        asset = hits[0]["_source"]
        asset_type = asset.get("asset_type", "vehicle")  # legacy default

        compatible_types = JOB_ASSET_COMPATIBILITY.get(job_type, [])
        if asset_type not in compatible_types:
            raise validation_error(
                f"Asset type '{asset_type}' is not compatible with job type "
                f"'{job_type.value}'. Compatible types: {compatible_types}",
                details={
                    "asset_id": asset_id,
                    "asset_type": asset_type,
                    "job_type": job_type.value,
                    "compatible_types": compatible_types,
                },
            )

        return asset

    async def _check_asset_availability(
        self,
        asset_id: str,
        scheduled_time: str,
        tenant_id: str,
        exclude_job_id: Optional[str] = None,
    ) -> None:
        """Verify the asset has no overlapping active jobs.

        Queries ``jobs_current`` for jobs with the same ``asset_assigned``
        that are in an active status (assigned or in_progress).  If any
        overlapping job is found the request is rejected with a 409.

        Args:
            asset_id: The asset to check.
            scheduled_time: The proposed job's scheduled time (ISO 8601).
            tenant_id: Tenant scope.
            exclude_job_id: Optionally exclude a specific job (for reassign).

        Raises:
            AppException: 409 if the asset is already busy.
        """
        must_clauses: list[dict] = [
            {"term": {"asset_assigned": asset_id}},
            {"term": {"tenant_id": tenant_id}},
            {"terms": {"status": [JobStatus.ASSIGNED.value, JobStatus.IN_PROGRESS.value]}},
        ]

        must_not_clauses: list[dict] = []
        if exclude_job_id:
            must_not_clauses.append({"term": {"job_id": exclude_job_id}})

        query: dict = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    **({"must_not": must_not_clauses} if must_not_clauses else {}),
                }
            },
            "size": 1,
        }

        response = await self._es.search_documents(JOBS_CURRENT_INDEX, query, size=1)
        total = response["hits"]["total"]["value"]

        if total > 0:
            conflicting = response["hits"]["hits"][0]["_source"]
            raise AppException(
                error_code=ErrorCode.DRIFT_THRESHOLD_EXCEEDED,
                message=(
                    f"Asset '{asset_id}' is already assigned to active job "
                    f"'{conflicting['job_id']}' (status: {conflicting['status']})"
                ),
                status_code=409,
                details={
                    "asset_id": asset_id,
                    "conflicting_job_id": conflicting["job_id"],
                    "conflicting_status": conflicting["status"],
                },
            )

    # ------------------------------------------------------------------
    # Event append helper  (Requirements 15.1, 15.3, 15.4)
    # ------------------------------------------------------------------

    async def _append_event(
        self,
        job_id: str,
        event_type: str,
        tenant_id: str,
        actor_id: Optional[str],
        payload: dict,
    ) -> str:
        """Append an event to the job_events index.

        Every mutation (create, assign, reassign, status change, cargo update)
        MUST call this method exactly once before returning.

        Args:
            job_id: The job this event belongs to.
            event_type: One of job_created, asset_assigned, asset_reassigned,
                        status_changed, cargo_updated, cargo_status_changed.
            tenant_id: Tenant scope for the event.
            actor_id: The user/operator who triggered the mutation.
            payload: Arbitrary dict stored as event_payload (not indexed).

        Returns:
            The generated event_id (UUID).
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        event_doc: dict = {
            "event_id": event_id,
            "job_id": job_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "event_timestamp": now,
            "event_payload": payload,
        }

        await self._es.index_document(JOB_EVENTS_INDEX, event_id, event_doc)
        logger.info(
            "Appended %s event %s for job %s", event_type, event_id, job_id
        )
        return event_id

    # ------------------------------------------------------------------
    # WebSocket broadcast stub  (wired in task 8.3)
    # ------------------------------------------------------------------

    async def _broadcast_job_update(
        self, event_type: str, job_data: dict
    ) -> None:
        """Broadcast a job change via the SchedulingWebSocketManager.

        Validates: Requirements 9.2, 9.4

        Args:
            event_type: The broadcast event type (e.g. job_created,
                        status_changed, delay_alert).
            job_data: The full job document to include in the broadcast.
        """
        if self._ws_manager is not None:
            try:
                await self._ws_manager.broadcast(event_type, job_data)
            except Exception as exc:
                logger.warning(
                    "WebSocket broadcast failed for %s on job %s: %s",
                    event_type,
                    job_data.get("job_id"),
                    exc,
                )
        else:
            logger.debug(
                "WebSocket manager not wired; skipping broadcast for %s on job %s",
                event_type,
                job_data.get("job_id"),
            )
