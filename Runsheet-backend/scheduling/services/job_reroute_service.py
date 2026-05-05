"""
Job reroute service for ad-hoc destination changes.

Allows dispatchers to reroute any cargo truck to an alternate destination
(e.g., a breakdown site or alternate fuel station) through the
ConfirmationProtocol.

Requirements covered:
- 2.1: Verify job exists and has valid status for rerouting
- 2.2: Create MutationRequest and submit to ConfirmationProtocol
- 2.3: Update job document on approval
- 2.4: Append job_rerouted event to job_events
- 2.5: Broadcast job_rerouted WebSocket event
- 2.6: Return 404 for missing job or wrong tenant
- 2.7: Return 400 for invalid job status
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from Agents.confirmation_protocol import MutationRequest
from errors.exceptions import (
    resource_not_found,
    validation_error,
)
from scheduling.services.scheduling_es_mappings import (
    JOBS_CURRENT_INDEX,
    JOB_EVENTS_INDEX,
)

logger = logging.getLogger(__name__)

# Statuses that allow rerouting
_REROUTABLE_STATUSES = {"assigned", "in_progress"}


class JobRerouteService:
    """Handles ad-hoc job rerouting through the ConfirmationProtocol.

    Follows the same patterns as JobService for ES queries, event appending,
    and WebSocket broadcasting.

    Validates: Requirements 2.1–2.7
    """

    def __init__(self, es_service, confirmation_protocol):
        self._es = es_service
        self._confirmation_protocol = confirmation_protocol
        self._ws_manager = None  # Wired by bootstrap

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def reroute_job(
        self,
        job_id: str,
        new_destination: str,
        tenant_id: str,
        reason: str,
        new_destination_location: Optional[dict] = None,
        actor_id: Optional[str] = None,
    ) -> dict:
        """Reroute a job to a new destination.

        Flow:
        1. Fetch job from jobs_current with tenant filter
        2. Validate status is assigned or in_progress
        3. Submit MutationRequest to ConfirmationProtocol
        4. On approval: update job document
        5. Append job_rerouted event
        6. Broadcast job_rerouted WebSocket event

        Args:
            job_id: The job to reroute.
            new_destination: The new destination address/name.
            tenant_id: Tenant scope from JWT.
            reason: Reason for the reroute.
            new_destination_location: Optional geo-point dict with lat/lng.
            actor_id: Optional user/operator performing the reroute.

        Returns:
            Dict with the updated job document and mutation result.

        Raises:
            AppException: 404 if job not found or wrong tenant.
            AppException: 400 if job status is not reroutable.
        """
        # 1. Fetch job with tenant filter (same pattern as JobService._get_job_doc)
        job_doc = await self._get_job_doc(job_id, tenant_id)

        # 2. Validate status
        if job_doc["status"] not in _REROUTABLE_STATUSES:
            raise validation_error(
                f"Cannot reroute job '{job_id}': current status is "
                f"'{job_doc['status']}', expected 'assigned' or 'in_progress'",
                details={
                    "job_id": job_id,
                    "current_status": job_doc["status"],
                    "allowed_statuses": sorted(_REROUTABLE_STATUSES),
                },
            )

        old_destination = job_doc.get("destination")

        # 3. Create MutationRequest and submit to ConfirmationProtocol
        mutation_params = {
            "job_id": job_id,
            "new_destination": new_destination,
            "reason": reason,
            "tenant_id": tenant_id,
        }
        if new_destination_location:
            mutation_params["new_destination_location"] = new_destination_location
        if actor_id:
            mutation_params["actor_id"] = actor_id

        mutation = MutationRequest(
            tool_name="reroute_job",
            parameters=mutation_params,
            tenant_id=tenant_id,
            agent_id="job_reroute_service",
            user_id=actor_id,
        )

        result = await self._confirmation_protocol.process_mutation(mutation)

        if not result.executed:
            return {
                "job": job_doc,
                "mutation_result": {
                    "executed": False,
                    "approval_id": result.approval_id,
                    "confirmation_method": result.confirmation_method,
                },
            }

        # 4. On approval: update job document with new destination and timestamps
        now = datetime.now(timezone.utc).isoformat()
        update_fields = {
            "destination": new_destination,
            "updated_at": now,
        }
        if new_destination_location:
            update_fields["destination_location"] = new_destination_location

        await self._es.update_document(JOBS_CURRENT_INDEX, job_id, update_fields)

        # Merge updates into doc for event and broadcast
        job_doc.update(update_fields)

        # 5. Append job_rerouted event
        await self._append_event(
            job_id=job_id,
            event_type="job_rerouted",
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload={
                "old_destination": old_destination,
                "new_destination": new_destination,
                "reason": reason,
                "actor_id": actor_id,
            },
        )

        # 6. Broadcast job_rerouted WebSocket event
        await self._broadcast_job_update("job_rerouted", job_doc)

        return {
            "job": job_doc,
            "mutation_result": {
                "executed": True,
                "confirmation_method": result.confirmation_method,
            },
        }

    # ------------------------------------------------------------------
    # Internal: fetch job document (same pattern as JobService._get_job_doc)
    # ------------------------------------------------------------------

    async def _get_job_doc(self, job_id: str, tenant_id: str) -> dict:
        """Fetch a raw job document from jobs_current by job_id with tenant filter.

        Args:
            job_id: The job identifier.
            tenant_id: Tenant scope extracted from JWT.

        Returns:
            The raw Elasticsearch document _source dict.

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
    # Event append helper (same pattern as JobService._append_event)
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

        Args:
            job_id: The job this event belongs to.
            event_type: The event type string.
            tenant_id: Tenant scope for the event.
            actor_id: The user/operator who triggered the action.
            payload: Arbitrary dict stored as event_payload.

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
    # WebSocket broadcast helper (same pattern as JobService._broadcast_job_update)
    # ------------------------------------------------------------------

    async def _broadcast_job_update(
        self, event_type: str, job_data: dict
    ) -> None:
        """Broadcast a job change via the SchedulingWebSocketManager.

        Args:
            event_type: The broadcast event type (e.g. job_rerouted).
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
