"""
Immutable audit timeline service for per-job event tracking.

Provides append-only event storage and query capabilities for the
job_audit_timeline Elasticsearch index. All job-related actions
(status changes, assignments, driver actions, notifications, agent
proposals) are recorded as immutable timeline events.

Validates: Requirements 12.1, 12.2, 12.3, 12.4
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from notifications.services.audit_es_mappings import JOB_AUDIT_TIMELINE_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class AuditTimelineService:
    """Append-only audit timeline for all job-related actions.

    Follows the same ES-backed async pattern as NotificationService and
    JobService. Events are written to the ``job_audit_timeline`` index
    and are never updated or deleted, enforcing immutability.

    Validates: Requirements 12.1, 12.2, 12.3, 12.4
    """

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

    async def append_event(
        self,
        job_id: str,
        event_type: str,
        actor_type: str,
        actor_id: str,
        payload: dict,
        tenant_id: str,
    ) -> str:
        """Append an immutable event to the audit timeline.

        Generates a UUID for the timeline_event_id and uses the current
        UTC timestamp. The event is indexed into the job_audit_timeline
        ES index and is never updated or deleted.

        Validates: Requirements 12.1, 12.2, 12.4

        Args:
            job_id: The job this event belongs to.
            event_type: The type of event (e.g. status_changed,
                assignment, ack, message, exception_reported,
                pod_submitted, notification_sent, proposal_executed).
            actor_type: The type of actor (driver, dispatcher, agent,
                system).
            actor_id: The identifier of the actor who triggered the event.
            payload: Arbitrary dict with event-specific context. Stored
                as an opaque object (enabled=False in mapping).
            tenant_id: Tenant scope for the event.

        Returns:
            The generated timeline_event_id (UUID string).
        """
        timeline_event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        event_doc = {
            "timeline_event_id": timeline_event_id,
            "job_id": job_id,
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "timestamp": now,
            "payload": payload or {},
            "tenant_id": tenant_id,
        }

        try:
            await self._es.index_document(
                JOB_AUDIT_TIMELINE_INDEX, timeline_event_id, event_doc
            )
            logger.info(
                "Appended audit event %s (type=%s) for job %s",
                timeline_event_id,
                event_type,
                job_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to append audit event for job %s: %s",
                job_id,
                exc,
            )
            raise

        return timeline_event_id

    async def query_timeline(
        self,
        job_id: str,
        tenant_id: str,
        event_type: Optional[str] = None,
        actor_type: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> list[dict]:
        """Query timeline events sorted by timestamp ascending.

        Supports optional filters on event_type, actor_type, and time
        range. Results are always sorted chronologically (oldest first).

        Validates: Requirements 12.3

        Args:
            job_id: The job to query events for.
            tenant_id: Tenant scope for the query.
            event_type: Optional filter on event_type (exact match).
            actor_type: Optional filter on actor_type (exact match).
            start_time: Optional ISO 8601 timestamp for range start (gte).
            end_time: Optional ISO 8601 timestamp for range end (lte).

        Returns:
            List of event dicts sorted by timestamp ascending.
        """
        must_clauses: list[dict] = [
            {"term": {"job_id": job_id}},
            {"term": {"tenant_id": tenant_id}},
        ]

        if event_type is not None:
            must_clauses.append({"term": {"event_type": event_type}})

        if actor_type is not None:
            must_clauses.append({"term": {"actor_type": actor_type}})

        if start_time is not None or end_time is not None:
            time_range: dict = {}
            if start_time is not None:
                time_range["gte"] = start_time
            if end_time is not None:
                time_range["lte"] = end_time
            must_clauses.append({"range": {"timestamp": time_range}})

        query = {
            "query": {"bool": {"must": must_clauses}},
            "sort": [{"timestamp": {"order": "asc"}}],
            "size": 10000,
        }

        response = await self._es.search_documents(
            JOB_AUDIT_TIMELINE_INDEX, query, size=10000
        )

        hits = response.get("hits", {}).get("hits", [])
        return [hit["_source"] for hit in hits]
