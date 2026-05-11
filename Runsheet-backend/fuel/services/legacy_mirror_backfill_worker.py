"""
Legacy Mirror Backfill Worker — drains ``pending_legacy_mirrors`` at 60s cadence.

For each entry in the retry queue, re-runs ``mirror_order`` or
``mirror_driver`` via :class:`LegacyDualWriter`. On success the entry is
removed. On failure the retry count is incremented with exponential
backoff (doubling each time) up to a 24-hour ceiling from creation time.
When retries are exhausted (next_retry_at would exceed 24 hours from
``created_at``), the entry is moved to ``ops_poison_queue`` with reason
``legacy_mirror_exhausted`` and the
``orders_legacy_mirror_exhausted_total{tenant_id}`` Prometheus counter
fires.

The worker itself MUST NOT crash on individual entry failures — each
entry is processed independently and errors are logged.

Validates: Requirements 1.3.2, 9.2.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from prometheus_client import Counter

from services.metrics import FUELOPS_REGISTRY
from services.time_utils import utcnow

logger = logging.getLogger(__name__)
ES_SEARCH_TIMEOUT_SECONDS = 10

__all__ = ["LegacyMirrorBackfillWorker", "run_backfill_cycle"]

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

orders_legacy_mirror_exhausted_total = Counter(
    "orders_legacy_mirror_exhausted_total",
    "Total legacy mirror entries that exhausted retries and were moved "
    "to the ops_poison_queue. Fires when exponential backoff exceeds "
    "the 24-hour ceiling from entry creation.",
    ["tenant_id"],
    registry=FUELOPS_REGISTRY,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PENDING_LEGACY_MIRRORS_INDEX = "pending_legacy_mirrors"

#: Maximum wall-clock time from entry creation before the entry is
#: considered exhausted and moved to the poison queue.
MAX_RETRY_WINDOW_HOURS = 24

#: Base backoff interval in seconds (doubles each retry).
BASE_BACKOFF_SECONDS = 60

#: Worker polling cadence in seconds.
WORKER_CADENCE_SECONDS = 60


# ---------------------------------------------------------------------------
# Worker implementation
# ---------------------------------------------------------------------------


class LegacyMirrorBackfillWorker:
    """Scheduled worker that drains the ``pending_legacy_mirrors`` queue.

    Parameters
    ----------
    es_service:
        The base ``ElasticsearchService`` for querying/deleting entries
        in ``pending_legacy_mirrors``.
    legacy_dual_writer:
        The :class:`LegacyDualWriter` instance used to re-run
        ``mirror_order`` / ``mirror_driver``.
    order_repository:
        Repository to fetch the current order document for re-mirroring.
    driver_repository:
        Repository to fetch the current driver document for re-mirroring.
    poison_queue_service:
        The :class:`PoisonQueueService` for moving exhausted entries.
    clock:
        Optional clock override for testing. Defaults to
        ``services.time_utils.utcnow``.
    """

    def __init__(
        self,
        es_service: Any,
        legacy_dual_writer: Any,
        order_repository: Any,
        driver_repository: Any,
        poison_queue_service: Any,
        clock: Optional[Callable] = None,
    ) -> None:
        self._es = es_service
        self._dual_writer = legacy_dual_writer
        self._order_repo = order_repository
        self._driver_repo = driver_repository
        self._poison_queue = poison_queue_service
        self._clock = clock or utcnow

    async def run_cycle(self) -> int:
        """Execute one drain cycle.

        Returns the number of entries successfully processed (removed).
        """
        entries = await self._fetch_due_entries()
        processed = 0

        for entry in entries:
            try:
                success = await self._process_entry(entry)
                if success:
                    processed += 1
            except Exception as exc:
                # Individual entry failures MUST NOT crash the worker.
                logger.error(
                    "Backfill worker failed on entry=%s: %s",
                    entry.get("entry_id", "unknown"),
                    exc,
                )

        return processed

    async def _fetch_due_entries(self) -> list[dict]:
        """Query ``pending_legacy_mirrors`` for entries whose
        ``next_retry_at`` is at or before now."""
        now = self._clock()
        query = {
            "bool": {
                "filter": [
                    {"range": {"next_retry_at": {"lte": now.isoformat()}}},
                ]
            }
        }
        body = {
            "query": query,
            "size": 100,
            "sort": [{"next_retry_at": "asc"}],
        }
        try:
            result = await self._es.client.search(
                index=PENDING_LEGACY_MIRRORS_INDEX,
                body=body,
                request_timeout=ES_SEARCH_TIMEOUT_SECONDS,
            )
            hits = result.get("hits", {}).get("hits", [])
            return [h["_source"] for h in hits]
        except Exception as exc:
            logger.error(
                "Failed to query pending_legacy_mirrors: %s", exc
            )
            return []

    async def _process_entry(self, entry: dict) -> bool:
        """Process a single pending mirror entry.

        Returns True if the entry was successfully mirrored and removed.
        Returns False if the entry was backed off or moved to poison queue.
        """
        entity_type = entry.get("entity_type", "order")
        entity_id = entry.get("entity_id", "unknown")
        tenant_id = entry.get("tenant_id", "unknown")
        entry_id = entry.get("entry_id", "unknown")
        created_at_str = entry.get("created_at", "")
        retry_count = entry.get("retry_count", 0)

        # Attempt the mirror operation
        mirror_success = await self._attempt_mirror(
            entity_type, entity_id, tenant_id
        )

        if mirror_success:
            # Success — remove the entry from pending_legacy_mirrors
            await self._remove_entry(entry_id)
            logger.info(
                "Backfill mirror succeeded for %s=%s, tenant=%s — "
                "entry removed",
                entity_type,
                entity_id,
                tenant_id,
            )
            return True

        # Mirror failed — check if retries are exhausted
        now = self._clock()
        created_at = self._parse_datetime(created_at_str) or now

        # Calculate next backoff: base * 2^retry_count
        next_backoff_seconds = BASE_BACKOFF_SECONDS * (2 ** retry_count)
        next_retry_at = now + timedelta(seconds=next_backoff_seconds)

        # Check if next_retry_at would exceed 24 hours from creation
        max_deadline = created_at + timedelta(hours=MAX_RETRY_WINDOW_HOURS)

        if next_retry_at > max_deadline:
            # Retries exhausted — move to poison queue
            await self._move_to_poison_queue(entry, tenant_id)
            await self._remove_entry(entry_id)

            # Fire the exhausted alert counter
            orders_legacy_mirror_exhausted_total.labels(
                tenant_id=tenant_id,
            ).inc()

            logger.warning(
                "Legacy mirror retries exhausted for %s=%s, tenant=%s "
                "— moved to ops_poison_queue",
                entity_type,
                entity_id,
                tenant_id,
            )
            return False

        # Back off — update retry_count and next_retry_at
        await self._update_entry_backoff(
            entry_id, retry_count + 1, next_retry_at
        )
        logger.info(
            "Backfill mirror failed for %s=%s, tenant=%s — "
            "backing off to %s (retry #%d)",
            entity_type,
            entity_id,
            tenant_id,
            next_retry_at.isoformat(),
            retry_count + 1,
        )
        return False

    async def _attempt_mirror(
        self, entity_type: str, entity_id: str, tenant_id: str
    ) -> bool:
        """Re-run the mirror operation for the given entity.

        Returns True on success, False on failure.
        The dual writer's mirror methods never raise, so we check
        success by attempting a direct mirror call and verifying no
        error was enqueued. For the backfill worker, we fetch the
        current entity and call the mirror directly, catching any
        exception as a failure signal.
        """
        try:
            if entity_type == "order":
                order_doc = await self._fetch_order(entity_id, tenant_id)
                if order_doc is None:
                    logger.warning(
                        "Order %s not found for tenant %s during "
                        "backfill — treating as success (order may "
                        "have been deleted)",
                        entity_id,
                        tenant_id,
                    )
                    return True
                # Call the projection + upsert directly to detect failures
                legacy_doc = self._dual_writer._project_order_to_shipment(
                    order_doc
                )
                await self._dual_writer._ops_es.upsert_shipment_current(
                    legacy_doc
                )
                return True

            elif entity_type == "driver":
                driver_doc = await self._fetch_driver(entity_id, tenant_id)
                if driver_doc is None:
                    logger.warning(
                        "Driver %s not found for tenant %s during "
                        "backfill — treating as success (driver may "
                        "have been deleted)",
                        entity_id,
                        tenant_id,
                    )
                    return True
                legacy_doc = self._dual_writer._project_driver_to_rider(
                    driver_doc
                )
                await self._dual_writer._ops_es.upsert_rider_current(
                    legacy_doc
                )
                return True

            else:
                logger.error(
                    "Unknown entity_type=%s for backfill entry", entity_type
                )
                return False

        except Exception as exc:
            logger.warning(
                "Mirror attempt failed for %s=%s, tenant=%s: %s",
                entity_type,
                entity_id,
                tenant_id,
                exc,
            )
            return False

    async def _fetch_order(
        self, order_id: str, tenant_id: str
    ) -> Optional[dict]:
        """Fetch the current order document from fuel_orders_current."""
        try:
            from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

            result = await self._es.client.get(
                index=FUEL_ORDERS_CURRENT_INDEX, id=order_id
            )
            source = result.get("_source", {})
            # Verify tenant isolation
            if source.get("tenant_id") != tenant_id:
                return None
            return source
        except Exception:
            return None

    async def _fetch_driver(
        self, driver_id: str, tenant_id: str
    ) -> Optional[dict]:
        """Fetch the current driver document from drivers_current."""
        try:
            from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX

            result = await self._es.client.get(
                index=DRIVERS_CURRENT_INDEX, id=driver_id
            )
            source = result.get("_source", {})
            # Verify tenant isolation
            if source.get("tenant_id") != tenant_id:
                return None
            return source
        except Exception:
            return None

    async def _remove_entry(self, entry_id: str) -> None:
        """Remove an entry from pending_legacy_mirrors."""
        try:
            await self._es.client.delete(
                index=PENDING_LEGACY_MIRRORS_INDEX,
                id=entry_id,
                ignore=[404],
            )
        except Exception as exc:
            logger.error(
                "Failed to remove entry %s from pending_legacy_mirrors: %s",
                entry_id,
                exc,
            )

    async def _update_entry_backoff(
        self, entry_id: str, new_retry_count: int, next_retry_at: datetime
    ) -> None:
        """Update an entry's retry_count and next_retry_at for backoff."""
        now = self._clock()
        try:
            await self._es.client.update(
                index=PENDING_LEGACY_MIRRORS_INDEX,
                id=entry_id,
                doc={
                    "retry_count": new_retry_count,
                    "next_retry_at": next_retry_at.isoformat(),
                    "updated_at": now.isoformat(),
                },
            )
        except Exception as exc:
            logger.error(
                "Failed to update backoff for entry %s: %s", entry_id, exc
            )

    async def _move_to_poison_queue(
        self, entry: dict, tenant_id: str
    ) -> None:
        """Move an exhausted entry to the ops_poison_queue."""
        try:
            await self._poison_queue.store_failed_event(
                payload={
                    "event_id": entry.get("entry_id", "unknown"),
                    "entity_type": entry.get("entity_type"),
                    "entity_id": entry.get("entity_id"),
                    "tenant_id": tenant_id,
                    "original_failure_reason": entry.get("failure_reason"),
                    "retry_count": entry.get("retry_count", 0),
                    "created_at": entry.get("created_at"),
                },
                error="Legacy mirror retries exhausted after 24-hour window",
                error_type="legacy_mirror_exhausted",
                tenant_id=tenant_id,
                trace_id=entry.get("entry_id", ""),
            )
        except Exception as exc:
            logger.error(
                "Failed to move entry %s to poison queue: %s",
                entry.get("entry_id"),
                exc,
            )

    @staticmethod
    def _parse_datetime(dt_str: str) -> Optional[datetime]:
        """Parse an ISO-8601 datetime string to a timezone-aware datetime."""
        if not dt_str:
            return None
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Standalone cycle runner (for use in bootstrap background task)
# ---------------------------------------------------------------------------


async def run_backfill_cycle(worker: LegacyMirrorBackfillWorker) -> None:
    """Run a single backfill cycle. Used by the periodic background task."""
    try:
        processed = await worker.run_cycle()
        if processed > 0:
            logger.info(
                "Legacy mirror backfill cycle completed: %d entries processed",
                processed,
            )
    except Exception as exc:
        logger.error("Legacy mirror backfill cycle failed: %s", exc)
