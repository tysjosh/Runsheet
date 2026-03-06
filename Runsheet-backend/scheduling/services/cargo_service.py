"""
Cargo manifest service for the Logistics Scheduling module.

Manages cargo manifest items within cargo_transport jobs, including
manifest retrieval, updates, individual item status changes, and
cross-job cargo search.

Requirements covered:
- 6.1: Get cargo manifest
- 6.2: Update cargo manifest
- 6.3: Cargo item fields and status
- 6.4: Cargo status changed event
- 6.5: Search cargo across jobs
- 6.6: All-delivered WebSocket notification
"""

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from errors.exceptions import resource_not_found, validation_error
from scheduling.models import CargoItem, CargoItemStatus
from scheduling.services.scheduling_es_mappings import (
    JOBS_CURRENT_INDEX,
    JOB_EVENTS_INDEX,
)
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class CargoService:
    """Manages cargo manifest items within cargo_transport jobs.

    Validates: Requirements 6.1-6.6
    """

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service
        self._ws_manager = None  # Wired in task 8.3

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_cargo_manifest(
        self, job_id: str, tenant_id: str
    ) -> list[dict]:
        """Return the cargo manifest for a job.

        Validates: Requirement 6.1

        Args:
            job_id: The job identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            List of cargo item dicts from the job's cargo_manifest.

        Raises:
            AppException: 404 if job not found for this tenant.
        """
        job_doc = await self._get_job_doc(job_id, tenant_id)
        return job_doc.get("cargo_manifest") or []

    async def update_cargo_manifest(
        self,
        job_id: str,
        items: list[CargoItem],
        tenant_id: str,
        actor_id: Optional[str] = None,
    ) -> list[dict]:
        """Replace the cargo manifest for a job.

        Validates: Requirement 6.2
        - Auto-generates item_id for items that don't have one.
        - Replaces the entire cargo_manifest nested array.
        - Appends a ``cargo_updated`` event.

        Args:
            job_id: The job to update.
            items: New list of cargo items.
            tenant_id: Tenant scope from JWT.
            actor_id: The operator performing the update.

        Returns:
            The updated list of cargo item dicts.

        Raises:
            AppException: 404 if job not found.
        """
        job_doc = await self._get_job_doc(job_id, tenant_id)

        # Build the new manifest, auto-generating item_ids where missing
        manifest: list[dict] = []
        for item in items:
            item_dict = item.model_dump()
            if not item_dict.get("item_id"):
                item_dict["item_id"] = f"CARGO_{uuid.uuid4().hex[:8]}"
            manifest.append(item_dict)

        now = datetime.now(timezone.utc).isoformat()

        # Update the job document with the new manifest
        await self._es.update_document(
            JOBS_CURRENT_INDEX,
            job_id,
            {"cargo_manifest": manifest, "updated_at": now},
        )

        # Append cargo_updated event
        old_manifest = job_doc.get("cargo_manifest") or []
        await self._append_event(
            job_id=job_id,
            event_type="cargo_updated",
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload={
                "old_item_count": len(old_manifest),
                "new_item_count": len(manifest),
                "items": manifest,
            },
        )

        logger.info("Updated cargo manifest for job %s (%d items)", job_id, len(manifest))
        return manifest

    async def update_cargo_item_status(
        self,
        job_id: str,
        item_id: str,
        new_status: CargoItemStatus,
        tenant_id: str,
        actor_id: Optional[str] = None,
    ) -> dict:
        """Update a single cargo item's status within the nested array.

        Validates: Requirements 6.3, 6.4, 6.6
        - Uses a painless script to update the specific item in-place.
        - Appends a ``cargo_status_changed`` event.
        - Checks if all items are delivered and broadcasts cargo_complete.

        Args:
            job_id: The job containing the cargo item.
            item_id: The cargo item to update.
            new_status: The new status for the item.
            tenant_id: Tenant scope from JWT.
            actor_id: The operator performing the update.

        Returns:
            The updated cargo item dict.

        Raises:
            AppException: 404 if job or item not found; 400 if validation fails.
        """
        job_doc = await self._get_job_doc(job_id, tenant_id)
        manifest = job_doc.get("cargo_manifest") or []

        # Find the item and its old status
        target_item = None
        old_status = None
        for item in manifest:
            if item.get("item_id") == item_id:
                old_status = item.get("item_status")
                target_item = item
                break

        if target_item is None:
            raise resource_not_found(
                f"Cargo item '{item_id}' not found in job '{job_id}'",
                details={"job_id": job_id, "item_id": item_id},
            )

        # Use painless script to update the specific item in the nested array
        painless_script = """
            for (int i = 0; i < ctx._source.cargo_manifest.size(); i++) {
                if (ctx._source.cargo_manifest[i].item_id == params.item_id) {
                    ctx._source.cargo_manifest[i].item_status = params.new_status;
                    break;
                }
            }
            ctx._source.updated_at = params.now;
        """

        now = datetime.now(timezone.utc).isoformat()

        es_client = self._es.client
        es_client.update(
            index=JOBS_CURRENT_INDEX,
            id=job_id,
            body={
                "script": {
                    "source": painless_script,
                    "lang": "painless",
                    "params": {
                        "item_id": item_id,
                        "new_status": new_status.value,
                        "now": now,
                    },
                }
            },
            refresh=True,
        )

        # Append cargo_status_changed event
        await self._append_event(
            job_id=job_id,
            event_type="cargo_status_changed",
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload={
                "item_id": item_id,
                "old_status": old_status,
                "new_status": new_status.value,
            },
        )

        # Build the updated item to return
        updated_item = {**target_item, "item_status": new_status.value}

        logger.info(
            "Updated cargo item %s in job %s: %s → %s",
            item_id, job_id, old_status, new_status.value,
        )

        # Broadcast cargo_update for the individual item change
        # Validates: Requirement 9.3
        if self._ws_manager is not None:
            try:
                await self._ws_manager.broadcast(
                    "cargo_update",
                    {
                        "job_id": job_id,
                        "item_id": item_id,
                        "old_status": old_status,
                        "new_status": new_status.value,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "WebSocket broadcast failed for cargo_update on job %s item %s: %s",
                    job_id, item_id, exc,
                )

        # Check if all items are now delivered
        all_delivered = await self._check_all_delivered(job_id, tenant_id)
        if all_delivered:
            await self._broadcast_cargo_complete(job_id, job_doc)

        return updated_item

    async def search_cargo(
        self,
        tenant_id: str,
        container_number: Optional[str] = None,
        description: Optional[str] = None,
        item_status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> dict:
        """Search cargo items across all jobs using nested queries.

        Validates: Requirement 6.5

        Args:
            tenant_id: Tenant scope from JWT.
            container_number: Filter by container number (exact match).
            description: Filter by description (text search).
            item_status: Filter by item status (exact match).
            page: Page number (1-based).
            size: Items per page.

        Returns:
            Paginated response with matching cargo items and their job context.
        """
        # Build nested query filters for cargo_manifest
        nested_filters: list[dict] = []

        if container_number:
            nested_filters.append(
                {"term": {"cargo_manifest.container_number": container_number}}
            )
        if description:
            nested_filters.append(
                {"match": {"cargo_manifest.description": description}}
            )
        if item_status:
            nested_filters.append(
                {"term": {"cargo_manifest.item_status": item_status}}
            )

        if not nested_filters:
            raise validation_error(
                "At least one search filter is required: container_number, description, or item_status",
            )

        from_offset = (page - 1) * size

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {
                            "nested": {
                                "path": "cargo_manifest",
                                "query": {
                                    "bool": {
                                        "must": nested_filters,
                                    }
                                },
                                "inner_hits": {
                                    "size": 100,
                                },
                            }
                        },
                    ]
                }
            },
            "from": from_offset,
            "size": size,
        }

        response = await self._es.search_documents(
            JOBS_CURRENT_INDEX, query, size=size
        )

        hits = response["hits"]["hits"]
        total = response["hits"]["total"]["value"]

        # Flatten results: each matching cargo item with its job context
        results: list[dict] = []
        for hit in hits:
            source = hit["_source"]
            inner_hits = hit.get("inner_hits", {}).get("cargo_manifest", {}).get("hits", {}).get("hits", [])
            for inner_hit in inner_hits:
                cargo_item = inner_hit["_source"]
                results.append({
                    "job_id": source.get("job_id"),
                    "job_type": source.get("job_type"),
                    "job_status": source.get("status"),
                    "origin": source.get("origin"),
                    "destination": source.get("destination"),
                    **cargo_item,
                })

        total_pages = max(1, math.ceil(total / size))

        return {
            "data": results,
            "pagination": {
                "page": page,
                "size": size,
                "total": total,
                "total_pages": total_pages,
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_job_doc(self, job_id: str, tenant_id: str) -> dict:
        """Fetch a raw job document from jobs_current with tenant filter.

        Args:
            job_id: The job identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            The raw Elasticsearch document _source dict.

        Raises:
            AppException: 404 if job not found for this tenant.
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

        response = await self._es.search_documents(
            JOBS_CURRENT_INDEX, query, size=1
        )
        hits = response["hits"]["hits"]

        if not hits:
            raise resource_not_found(
                f"Job '{job_id}' not found",
                details={"job_id": job_id},
            )

        return hits[0]["_source"]

    async def _check_all_delivered(
        self, job_id: str, tenant_id: str
    ) -> bool:
        """Check if every item in the manifest has item_status=delivered.

        Re-fetches the job to get the latest state after the painless update.

        Args:
            job_id: The job to check.
            tenant_id: Tenant scope.

        Returns:
            True if all items are delivered, False otherwise.
        """
        job_doc = await self._get_job_doc(job_id, tenant_id)
        manifest = job_doc.get("cargo_manifest") or []

        if not manifest:
            return False

        return all(
            item.get("item_status") == CargoItemStatus.DELIVERED.value
            for item in manifest
        )

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
            event_type: One of cargo_updated, cargo_status_changed.
            tenant_id: Tenant scope for the event.
            actor_id: The user/operator who triggered the mutation.
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

    async def _broadcast_cargo_complete(
        self, job_id: str, job_doc: dict
    ) -> None:
        """Broadcast cargo_complete via WebSocket when all items are delivered.

        Validates: Requirement 6.6

        Args:
            job_id: The job whose cargo is fully delivered.
            job_doc: The job document for broadcast payload.
        """
        logger.info("All cargo items delivered for job %s — broadcasting cargo_complete", job_id)

        if self._ws_manager is not None:
            try:
                await self._ws_manager.broadcast(
                    "cargo_complete",
                    {
                        "job_id": job_id,
                        "job_type": job_doc.get("job_type"),
                        "origin": job_doc.get("origin"),
                        "destination": job_doc.get("destination"),
                        "asset_assigned": job_doc.get("asset_assigned"),
                    },
                )
            except Exception as exc:
                logger.warning(
                    "WebSocket broadcast failed for cargo_complete on job %s: %s",
                    job_id, exc,
                )
        else:
            logger.debug(
                "WebSocket manager not wired; skipping cargo_complete broadcast for job %s",
                job_id,
            )
