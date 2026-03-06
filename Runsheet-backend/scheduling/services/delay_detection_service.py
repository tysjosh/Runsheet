"""
Delay detection service for the Logistics Scheduling module.

Handles ETA tracking, automatic delay detection for overdue jobs, and
delay metrics aggregation. Runs as a periodic check (background task)
or is queried on-demand via API endpoints.

Requirements covered:
- 7.1: ETA calculation stored on in_progress transition
- 7.2: GET /scheduling/jobs/{job_id}/eta endpoint support
- 7.3: Automatic delay detection for overdue in_progress jobs
- 7.4: WebSocket delay_alert broadcast when jobs become delayed
- 7.5: Delay metrics (count, avg duration, grouped by job_type)
- 7.6: Delay duration recorded on completion
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from scheduling.models import JobStatus
from scheduling.services.scheduling_es_mappings import JOBS_CURRENT_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class DelayDetectionService:
    """Detects delayed jobs and broadcasts alerts.

    Validates: Requirements 7.1-7.6
    """

    def __init__(self, es_service: ElasticsearchService, ws_manager=None):
        self._es = es_service
        self._ws = ws_manager

    # ------------------------------------------------------------------
    # Delay Detection  (Requirements 7.3, 7.4)
    # ------------------------------------------------------------------

    async def check_delays(self, tenant_id: Optional[str] = None) -> list[dict]:
        """Check for in_progress jobs that have exceeded their estimated_arrival.

        Queries jobs_current for jobs where:
        - status = in_progress
        - delayed = false
        - estimated_arrival < now

        For each matching job:
        - Sets delayed = true
        - Calculates delay_duration_minutes
        - Broadcasts delay_alert via WebSocket

        If tenant_id is None, checks across all tenants (for the periodic
        background task).

        Args:
            tenant_id: Optional tenant scope. None checks all tenants.

        Returns:
            List of job dicts that were newly marked as delayed.

        Validates: Requirements 7.3, 7.4
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        must_clauses: list[dict] = [
            {"term": {"status": JobStatus.IN_PROGRESS.value}},
            {"term": {"delayed": False}},
            {"range": {"estimated_arrival": {"lt": now_iso}}},
        ]

        if tenant_id is not None:
            must_clauses.append({"term": {"tenant_id": tenant_id}})

        query: dict = {
            "query": {"bool": {"must": must_clauses}},
            "size": 1000,
        }

        response = await self._es.search_documents(
            JOBS_CURRENT_INDEX, query, size=1000
        )

        hits = response["hits"]["hits"]
        newly_delayed: list[dict] = []

        for hit in hits:
            job_doc = hit["_source"]
            job_id = job_doc["job_id"]

            # Calculate delay duration in minutes
            delay_minutes = 0
            estimated_arrival_str = job_doc.get("estimated_arrival")
            if estimated_arrival_str:
                try:
                    eta_dt = datetime.fromisoformat(
                        estimated_arrival_str.replace("Z", "+00:00")
                    )
                    delay_minutes = max(
                        int((now - eta_dt).total_seconds() / 60), 0
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        "Could not parse estimated_arrival for job %s: %s",
                        job_id,
                        estimated_arrival_str,
                    )

            # Update job as delayed
            update_fields = {
                "delayed": True,
                "delay_duration_minutes": delay_minutes,
                "updated_at": now_iso,
            }

            try:
                await self._es.update_document(
                    JOBS_CURRENT_INDEX, job_id, update_fields
                )
            except Exception as exc:
                logger.error(
                    "Failed to mark job %s as delayed: %s", job_id, exc
                )
                continue

            # Merge updates into doc for broadcast
            job_doc.update(update_fields)
            newly_delayed.append(job_doc)

            # Broadcast delay_alert via WebSocket
            await self._broadcast_delay_alert(job_doc, delay_minutes)

            logger.info(
                "Marked job %s as delayed (delay: %d minutes)",
                job_id,
                delay_minutes,
            )

        if newly_delayed:
            logger.info(
                "Delay check complete: %d job(s) newly marked as delayed",
                len(newly_delayed),
            )
        else:
            logger.debug("Delay check complete: no new delays detected")

        return newly_delayed

    # ------------------------------------------------------------------
    # ETA Query  (Requirement 7.2)
    # ------------------------------------------------------------------

    async def get_eta(self, job_id: str, tenant_id: str) -> dict:
        """Return the current estimated_arrival for a job.

        Args:
            job_id: The job identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            Dict with job_id, estimated_arrival, delayed, and
            delay_duration_minutes.

        Raises:
            AppException: 404 if job not found for this tenant.

        Validates: Requirement 7.2
        """
        from errors.exceptions import resource_not_found

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
            "_source": [
                "job_id",
                "estimated_arrival",
                "delayed",
                "delay_duration_minutes",
                "status",
                "scheduled_time",
            ],
        }

        response = await self._es.search_documents(
            JOBS_CURRENT_INDEX, query, size=1
        )
        hits = response["hits"]["hits"]

        if not hits:
            raise resource_not_found(
                f"Job '{job_id}' not found",
                details={"job_id": job_id},
            )

        source = hits[0]["_source"]
        return {
            "job_id": source["job_id"],
            "estimated_arrival": source.get("estimated_arrival"),
            "delayed": source.get("delayed", False),
            "delay_duration_minutes": source.get("delay_duration_minutes"),
            "status": source.get("status"),
            "scheduled_time": source.get("scheduled_time"),
        }

    # ------------------------------------------------------------------
    # Delay Metrics  (Requirement 7.5)
    # ------------------------------------------------------------------

    async def get_delay_metrics(
        self,
        tenant_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict:
        """Return delay statistics using ES aggregations.

        Computes:
        - Total count of delayed jobs
        - Average delay_duration_minutes
        - Delayed jobs grouped by job_type

        Supports optional date range filtering on scheduled_time.

        Args:
            tenant_id: Tenant scope from JWT.
            start_date: Optional start of date range (ISO 8601).
            end_date: Optional end of date range (ISO 8601).

        Returns:
            Dict with total_delayed, avg_delay_minutes, and
            delays_by_job_type.

        Validates: Requirement 7.5
        """
        must_clauses: list[dict] = [
            {"term": {"tenant_id": tenant_id}},
            {"term": {"delayed": True}},
        ]

        # Optional date range filter
        if start_date is not None or end_date is not None:
            date_range: dict = {}
            if start_date is not None:
                date_range["gte"] = start_date
            if end_date is not None:
                date_range["lte"] = end_date
            must_clauses.append({"range": {"scheduled_time": date_range}})

        query: dict = {
            "query": {"bool": {"must": must_clauses}},
            "size": 0,
            "aggs": {
                "avg_delay": {
                    "avg": {"field": "delay_duration_minutes"}
                },
                "delays_by_job_type": {
                    "terms": {"field": "job_type", "size": 20},
                    "aggs": {
                        "avg_delay": {
                            "avg": {"field": "delay_duration_minutes"}
                        }
                    },
                },
            },
        }

        response = await self._es.search_documents(
            JOBS_CURRENT_INDEX, query, size=0
        )

        total_delayed = response["hits"]["total"]["value"]
        aggs = response.get("aggregations", {})

        avg_delay_minutes = 0.0
        avg_delay_agg = aggs.get("avg_delay", {})
        if avg_delay_agg.get("value") is not None:
            avg_delay_minutes = round(avg_delay_agg["value"], 2)

        delays_by_job_type: list[dict] = []
        job_type_buckets = aggs.get("delays_by_job_type", {}).get("buckets", [])
        for bucket in job_type_buckets:
            bucket_avg = bucket.get("avg_delay", {}).get("value")
            delays_by_job_type.append({
                "job_type": bucket["key"],
                "count": bucket["doc_count"],
                "avg_delay_minutes": round(bucket_avg, 2) if bucket_avg is not None else 0.0,
            })

        return {
            "total_delayed": total_delayed,
            "avg_delay_minutes": avg_delay_minutes,
            "delays_by_job_type": delays_by_job_type,
        }

    # ------------------------------------------------------------------
    # Internal: WebSocket broadcast
    # ------------------------------------------------------------------

    async def _broadcast_delay_alert(
        self, job_data: dict, delay_minutes: int
    ) -> None:
        """Broadcast a delay_alert via WebSocket for a newly delayed job.

        Validates: Requirement 7.4

        Args:
            job_data: The full job document.
            delay_minutes: The calculated delay duration in minutes.
        """
        if self._ws is not None:
            try:
                await self._ws.broadcast(
                    "delay_alert",
                    {
                        "job_id": job_data.get("job_id"),
                        "job_type": job_data.get("job_type"),
                        "asset_assigned": job_data.get("asset_assigned"),
                        "origin": job_data.get("origin"),
                        "destination": job_data.get("destination"),
                        "estimated_arrival": job_data.get("estimated_arrival"),
                        "delay_duration_minutes": delay_minutes,
                        "tenant_id": job_data.get("tenant_id"),
                    },
                )
            except Exception as exc:
                logger.warning(
                    "WebSocket broadcast failed for delay_alert on job %s: %s",
                    job_data.get("job_id"),
                    exc,
                )
        else:
            logger.debug(
                "WebSocket manager not wired; skipping delay_alert broadcast "
                "for job %s",
                job_data.get("job_id"),
            )
