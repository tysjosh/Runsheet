"""
Replay Service for historical data backfill from Dinee REST APIs.

Pulls paginated historical data from Dinee APIs, processes through the same
AdapterTransformer and idempotency pipeline as live webhook events, and
upserts into Elasticsearch. Older replay snapshots arriving after newer
live updates are automatically discarded by the scripted upsert's
last_event_timestamp comparison.

Requirements: 3.1-3.7
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from config.settings import Settings
from ops.ingestion.adapter import AdapterTransformer, WebhookPayload
from ops.ingestion.idempotency import IdempotencyService
from ops.services.ops_es_service import OpsElasticsearchService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job status model
# ---------------------------------------------------------------------------


class ReplayJobStatus(BaseModel):
    """Progress and status of a replay/backfill job."""

    job_id: str = Field(..., description="Unique job identifier")
    tenant_id: str = Field(..., description="Tenant being backfilled")
    status: str = Field(
        default="running",
        description="Job status: pending | running | completed | failed",
    )
    total_records: int = Field(default=0, description="Total records to process")
    processed_count: int = Field(default=0, description="Successfully processed")
    failed_count: int = Field(default=0, description="Failed to process")
    skipped_count: int = Field(default=0, description="Skipped (duplicates)")
    estimated_remaining: Optional[str] = Field(
        default=None, description="Estimated time remaining"
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Job start timestamp",
    )
    completed_at: Optional[datetime] = Field(
        default=None, description="Job completion timestamp"
    )


# ---------------------------------------------------------------------------
# Replay Service
# ---------------------------------------------------------------------------

# Default page size for Dinee API pagination
DEFAULT_PAGE_SIZE = 100

# Max retry attempts for transient Dinee API errors (Req 3.6)
MAX_RETRIES = 5

# Base delay in seconds for exponential backoff
BASE_BACKOFF_SECONDS = 1.0


class ReplayService:
    """
    Background job service that pulls historical data from Dinee REST APIs
    to rebuild or backfill Elasticsearch state.

    Uses the same AdapterTransformer, IdempotencyService, and scripted
    upsert pipeline as live webhook processing. Older replay snapshots
    are automatically discarded by the out-of-order reconciliation logic.

    Requirements: 3.1-3.7
    """

    def __init__(
        self,
        adapter: AdapterTransformer,
        idempotency: IdempotencyService,
        ops_es: OpsElasticsearchService,
        settings: Settings,
    ):
        self._adapter = adapter
        self._idempotency = idempotency
        self._ops_es = ops_es
        self._settings = settings

        # In-memory job tracking (job_id -> ReplayJobStatus)
        self._jobs: dict[str, ReplayJobStatus] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def trigger_backfill(
        self,
        tenant_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> ReplayJobStatus:
        """
        Trigger a backfill job for a tenant and time range.

        Creates a background task that pulls paginated data from the
        Dinee REST API and processes each record through the standard
        ingestion pipeline.

        Validates: Req 3.1, 3.2

        Returns:
            ReplayJobStatus with the initial job state.
        """
        job_id = str(uuid.uuid4())
        job = ReplayJobStatus(
            job_id=job_id,
            tenant_id=tenant_id,
            status="running",
        )
        self._jobs[job_id] = job

        # Launch the backfill in the background
        asyncio.create_task(
            self._run_backfill(job, tenant_id, start_time, end_time)
        )

        logger.info(
            "Replay backfill triggered: job_id=%s tenant_id=%s range=[%s, %s]",
            job_id,
            tenant_id,
            start_time.isoformat(),
            end_time.isoformat(),
        )
        return job

    async def get_job_status(self, job_id: str) -> Optional[ReplayJobStatus]:
        """
        Return the current status of a backfill job.

        Validates: Req 3.5

        Returns:
            ReplayJobStatus or None if the job_id is unknown.
        """
        return self._jobs.get(job_id)

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    async def _run_backfill(
        self,
        job: ReplayJobStatus,
        tenant_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """
        Execute the backfill: paginate through Dinee API, transform,
        deduplicate, and upsert each record.
        """
        base_url = self._settings.dinee_api_base_url
        api_key = self._settings.dinee_api_key

        if not base_url:
            job.status = "failed"
            job.completed_at = datetime.now(timezone.utc)
            logger.error(
                "Replay backfill failed: dinee_api_base_url not configured "
                "(job_id=%s)",
                job.job_id,
            )
            return

        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        page = 1
        processing_start = datetime.now(timezone.utc)

        try:
            async with httpx.AsyncClient(
                base_url=base_url, headers=headers, timeout=30.0
            ) as client:
                while True:
                    # --- Fetch a page from Dinee API with retries ---
                    records, total = await self._fetch_page_with_retry(
                        client, tenant_id, start_time, end_time, page
                    )

                    if records is None:
                        # All retries exhausted for this page
                        job.status = "failed"
                        job.completed_at = datetime.now(timezone.utc)
                        logger.error(
                            "Replay backfill failed: could not fetch page %d "
                            "(job_id=%s)",
                            page,
                            job.job_id,
                        )
                        break

                    # Update total on first page
                    if page == 1 and total is not None:
                        job.total_records = total

                    if not records:
                        # No more records
                        break

                    # --- Process each record ---
                    for record in records:
                        await self._process_record(job, record, tenant_id)

                    # Update estimated remaining
                    self._update_estimated_remaining(job, processing_start)

                    # Move to next page
                    page += 1

                    # If we've processed all known records, stop
                    if (
                        job.total_records > 0
                        and (job.processed_count + job.failed_count + job.skipped_count)
                        >= job.total_records
                    ):
                        break

        except Exception as exc:
            logger.error(
                "Replay backfill unexpected error: job_id=%s error=%s",
                job.job_id,
                exc,
            )
            job.status = "failed"
            job.completed_at = datetime.now(timezone.utc)
            return

        # Mark completed if not already failed
        if job.status == "running":
            job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        job.estimated_remaining = None

        # Req 3.7 — Log summary on completion
        logger.info(
            "Replay backfill %s: job_id=%s tenant_id=%s "
            "total=%d processed=%d failed=%d skipped=%d",
            job.status,
            job.job_id,
            job.tenant_id,
            job.total_records,
            job.processed_count,
            job.failed_count,
            job.skipped_count,
        )

    # ------------------------------------------------------------------
    # Dinee API pagination with retry
    # ------------------------------------------------------------------

    async def _fetch_page_with_retry(
        self,
        client: httpx.AsyncClient,
        tenant_id: str,
        start_time: datetime,
        end_time: datetime,
        page: int,
    ) -> tuple[Optional[list[dict]], Optional[int]]:
        """
        Fetch a single page from the Dinee backfill API with exponential
        backoff retry on transient errors (up to MAX_RETRIES attempts).

        Validates: Req 3.2, 3.6

        Returns:
            (records, total) on success, (None, None) if all retries fail.
        """
        params = {
            "tenant_id": tenant_id,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "page": page,
            "page_size": DEFAULT_PAGE_SIZE,
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get("/events", params=params)
                response.raise_for_status()
                data = response.json()

                records = data.get("records", data.get("data", []))
                total = data.get("total", data.get("total_records"))
                return records, total

            except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
                if attempt < MAX_RETRIES:
                    delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Dinee API request failed (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        attempt,
                        MAX_RETRIES,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Dinee API request failed after %d attempts: %s",
                        MAX_RETRIES,
                        exc,
                    )
                    return None, None

        return None, None

    # ------------------------------------------------------------------
    # Record processing (mirrors webhook receiver pipeline)
    # ------------------------------------------------------------------

    async def _process_record(
        self,
        job: ReplayJobStatus,
        record: dict,
        tenant_id: str,
    ) -> None:
        """
        Process a single backfill record through the same pipeline as
        live webhook events: idempotency check → transform → upsert.

        Validates: Req 3.3, 3.4
        """
        request_id = f"replay-{job.job_id}-{uuid.uuid4().hex[:8]}"
        event_id = record.get("event_id", "")

        # --- Idempotency check (same as webhook receiver, Req 3.4) ---
        try:
            if event_id and await self._idempotency.is_duplicate(event_id):
                job.skipped_count += 1
                return
        except Exception as exc:
            logger.warning(
                "Idempotency check failed for event_id=%s: %s",
                event_id,
                exc,
            )
            # Continue processing — better to risk a duplicate than skip

        # --- Build WebhookPayload for the adapter ---
        try:
            payload = WebhookPayload(
                event_id=event_id or str(uuid.uuid4()),
                event_type=record.get("event_type", "unknown"),
                schema_version=record.get("schema_version", "1.0"),
                tenant_id=record.get("tenant_id", tenant_id),
                timestamp=record.get("timestamp", datetime.now(timezone.utc).isoformat()),
                data=record.get("data", record),
            )
        except Exception as exc:
            logger.warning(
                "Failed to parse replay record as WebhookPayload: %s", exc
            )
            job.failed_count += 1
            return

        # --- Check schema version support ---
        if not self._adapter.is_version_supported(payload.schema_version):
            logger.warning(
                "Replay record has unsupported schema_version '%s' "
                "(event_id=%s) — skipping",
                payload.schema_version,
                event_id,
            )
            job.failed_count += 1
            return

        # --- Transform via AdapterTransformer (Req 3.3) ---
        try:
            result = self._adapter.transform(payload, request_id)
        except Exception as exc:
            logger.warning(
                "Adapter transform failed for replay event_id=%s: %s",
                event_id,
                exc,
            )
            job.failed_count += 1
            return

        # --- Upsert into Elasticsearch (same pipeline as live events) ---
        try:
            if result.event_doc:
                await self._ops_es.append_shipment_event(result.event_doc)

            if result.shipment_current_doc:
                await self._ops_es.upsert_shipment_current(
                    result.shipment_current_doc
                )

            if result.rider_current_doc:
                await self._ops_es.upsert_rider_current(
                    result.rider_current_doc
                )
        except Exception as exc:
            logger.warning(
                "ES indexing failed for replay event_id=%s: %s",
                event_id,
                exc,
            )
            job.failed_count += 1
            return

        # --- Mark as processed in idempotency store ---
        try:
            if event_id:
                await self._idempotency.mark_processed(event_id)
        except Exception as exc:
            logger.warning(
                "Failed to mark replay event_id=%s as processed: %s",
                event_id,
                exc,
            )

        job.processed_count += 1

    # ------------------------------------------------------------------
    # Progress estimation
    # ------------------------------------------------------------------

    @staticmethod
    def _update_estimated_remaining(
        job: ReplayJobStatus,
        processing_start: datetime,
    ) -> None:
        """
        Update the estimated_remaining field based on processing rate.

        Validates: Req 3.5
        """
        done = job.processed_count + job.failed_count + job.skipped_count
        if done == 0 or job.total_records == 0:
            job.estimated_remaining = None
            return

        elapsed = (datetime.now(timezone.utc) - processing_start).total_seconds()
        if elapsed <= 0:
            job.estimated_remaining = None
            return

        rate = done / elapsed  # records per second
        remaining = max(0, job.total_records - done)

        if rate > 0:
            seconds_left = remaining / rate
            if seconds_left < 60:
                job.estimated_remaining = f"{int(seconds_left)}s"
            elif seconds_left < 3600:
                job.estimated_remaining = f"{int(seconds_left / 60)}m"
            else:
                job.estimated_remaining = f"{seconds_left / 3600:.1f}h"
        else:
            job.estimated_remaining = None


# ---------------------------------------------------------------------------
# Module-level service reference (wired via configure_replay_service)
# ---------------------------------------------------------------------------

_replay_service: Optional[ReplayService] = None


def configure_replay_service(
    *,
    adapter: AdapterTransformer,
    idempotency: IdempotencyService,
    ops_es: OpsElasticsearchService,
    settings: Settings,
) -> ReplayService:
    """
    Create and wire the ReplayService singleton.

    Called once during application startup (from main.py).
    Returns the created service instance.
    """
    global _replay_service
    _replay_service = ReplayService(
        adapter=adapter,
        idempotency=idempotency,
        ops_es=ops_es,
        settings=settings,
    )
    return _replay_service


def get_replay_service() -> Optional[ReplayService]:
    """Return the configured ReplayService or None."""
    return _replay_service
