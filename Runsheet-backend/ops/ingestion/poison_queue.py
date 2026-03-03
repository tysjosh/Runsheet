"""
Poison Queue Service for failed webhook event storage and retry.

Stores failed events in the ops_poison_queue Elasticsearch index for
durability across restarts. Supports listing, retry, and purge operations.

Requirements: 4.1-4.7
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_RETRIES = 5


class PoisonQueueEntry(BaseModel):
    event_id: str
    original_payload: dict
    error_reason: str
    error_type: str
    tenant_id: str = ""
    trace_id: str = ""
    created_at: str = ""
    retry_count: int = 0
    max_retries: int = MAX_RETRIES
    status: str = "pending"  # pending | retrying | permanently_failed


class PoisonQueueService:
    """Manages the ops_poison_queue Elasticsearch index."""

    INDEX_NAME = "ops_poison_queue"

    def __init__(self, ops_es_service):
        self.ops_es = ops_es_service

    async def store_failed_event(
        self,
        payload: dict,
        error: str,
        error_type: str,
        tenant_id: str = "",
        trace_id: str = "",
    ) -> None:
        """Store a failed event in the poison queue. Req 4.1, 20.6"""
        event_id = payload.get("event_id", "unknown")
        doc = {
            "event_id": event_id,
            "original_payload": payload,
            "error_reason": error,
            "error_type": error_type,
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "retry_count": 0,
            "max_retries": MAX_RETRIES,
            "status": "pending",
        }
        es = self.ops_es.es_service
        await es.client.index(index=self.INDEX_NAME, id=event_id, document=doc)
        logger.warning(
            "Event %s stored in poison queue: %s (%s), trace_id=%s",
            event_id, error, error_type, trace_id,
        )

    async def list_failed_events(
        self,
        error_type: Optional[str] = None,
        time_range: Optional[tuple] = None,
        retry_count: Optional[int] = None,
        page: int = 1,
        size: int = 20,
    ) -> dict:
        """List failed events with filtering. Req 4.3"""
        filters = []
        if error_type:
            filters.append({"term": {"error_type": error_type}})
        if retry_count is not None:
            filters.append({"term": {"retry_count": retry_count}})
        if time_range:
            start, end = time_range
            filters.append({"range": {"created_at": {"gte": start, "lte": end}}})

        query = {"bool": {"must": filters}} if filters else {"match_all": {}}
        body = {
            "query": query,
            "from": (page - 1) * size,
            "size": size,
            "sort": [{"created_at": "desc"}],
        }
        es = self.ops_es.es_service
        result = await es.client.search(index=self.INDEX_NAME, body=body)
        hits = result.get("hits", {})
        total = hits.get("total", {}).get("value", 0)
        docs = [h["_source"] for h in hits.get("hits", [])]
        return {"data": docs, "total": total, "page": page, "size": size}

    async def retry_event(self, event_id: str) -> dict:
        """Retry a poison queue event through the standard pipeline. Req 4.4"""
        es = self.ops_es.es_service
        try:
            result = await es.client.get(index=self.INDEX_NAME, id=event_id)
        except Exception:
            return {"status": "not_found", "event_id": event_id}

        source = result["_source"]
        current_retries = source.get("retry_count", 0)

        if current_retries >= MAX_RETRIES:
            logger.error(
                "Event %s exceeded max retries (%d). Marking permanently failed.",
                event_id, MAX_RETRIES,
            )
            await es.client.update(
                index=self.INDEX_NAME,
                id=event_id,
                doc={"status": "permanently_failed"},
            )
            return {"status": "permanently_failed", "event_id": event_id}

        # Increment retry count and mark as retrying
        await es.client.update(
            index=self.INDEX_NAME,
            id=event_id,
            doc={"retry_count": current_retries + 1, "status": "retrying"},
        )

        return {
            "status": "retrying",
            "event_id": event_id,
            "original_payload": source.get("original_payload", {}),
            "retry_count": current_retries + 1,
        }

    async def purge_event(self, event_id: str) -> None:
        """Permanently remove a failed event from the queue. Req 4.7"""
        es = self.ops_es.es_service
        await es.client.delete(index=self.INDEX_NAME, id=event_id, ignore=[404])
        logger.info("Purged event %s from poison queue", event_id)

    async def get_queue_depth(self, tenant_id: Optional[str] = None) -> int:
        """Get current poison queue depth."""
        query: dict = {"match_all": {}}
        if tenant_id:
            query = {"term": {"tenant_id": tenant_id}}
        es = self.ops_es.es_service
        result = await es.client.count(index=self.INDEX_NAME, query=query)
        return result.get("count", 0)
