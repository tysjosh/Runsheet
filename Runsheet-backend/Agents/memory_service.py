"""
Memory Service for long-term agent memory storage and retrieval.

Stores operational patterns and user preferences in the ``agent_memory``
Elasticsearch index, enabling agents to learn from past observations
and personalise responses across sessions.

Index mapping fields:
    memory_id, memory_type, agent_id, tenant_id, content,
    confidence_score, created_at, last_accessed, access_count, tags

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7
"""
import uuid
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class MemoryService:
    """Long-term memory store for operational patterns and user preferences.

    Memories are stored in the ``agent_memory`` Elasticsearch index.
    Each memory has a confidence score that decays over time when the
    memory is not accessed, and memories with very low confidence are
    automatically purged.

    Attributes:
        INDEX: The Elasticsearch index name for memory entries.
        DECAY_DAYS: Number of days after which stale memories decay.
        DECAY_FACTOR: Multiplier applied to confidence of stale memories.
        PURGE_THRESHOLD: Confidence score below which memories are purged.
    """

    INDEX = "agent_memory"
    DECAY_DAYS = 90
    DECAY_FACTOR = 0.5
    PURGE_THRESHOLD = 0.1

    def __init__(self, es_service):
        """Initialise the service with its dependencies.

        Args:
            es_service: ElasticsearchService for persistence.
        """
        self._es = es_service

    # ------------------------------------------------------------------
    # Store operations
    # ------------------------------------------------------------------

    async def store_pattern(
        self,
        agent_id: str,
        tenant_id: str,
        content: str,
        confidence: float,
        tags: list,
    ) -> str:
        """Store a discovered operational pattern.

        Args:
            agent_id: The agent that discovered the pattern.
            tenant_id: Tenant scope for the memory.
            content: Description of the pattern discovered.
            confidence: Initial confidence score (0.0–1.0).
            tags: List of keyword tags for retrieval.

        Returns:
            The generated memory_id (UUID string).
        """
        return await self._store(
            memory_type="pattern",
            agent_id=agent_id,
            tenant_id=tenant_id,
            content=content,
            confidence=confidence,
            tags=tags,
        )

    async def store_preference(
        self,
        agent_id: str,
        tenant_id: str,
        content: str,
        tags: list,
    ) -> str:
        """Store a user preference from conversation.

        Preferences are stored with a default confidence of 1.0.

        Args:
            agent_id: The agent that captured the preference.
            tenant_id: Tenant scope for the memory.
            content: Description of the user preference.
            tags: List of keyword tags for retrieval.

        Returns:
            The generated memory_id (UUID string).
        """
        return await self._store(
            memory_type="preference",
            agent_id=agent_id,
            tenant_id=tenant_id,
            content=content,
            confidence=1.0,
            tags=tags,
        )

    async def _store(
        self,
        memory_type: str,
        agent_id: str,
        tenant_id: str,
        content: str,
        confidence: float,
        tags: list,
    ) -> str:
        """Internal helper to persist a memory entry.

        Args:
            memory_type: ``"pattern"`` or ``"preference"``.
            agent_id: The agent that created the memory.
            tenant_id: Tenant scope.
            content: Memory content text.
            confidence: Confidence score (0.0–1.0).
            tags: Keyword tags.

        Returns:
            The generated memory_id.
        """
        memory_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        doc = {
            "memory_id": memory_id,
            "memory_type": memory_type,
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "content": content,
            "confidence_score": confidence,
            "created_at": now,
            "last_accessed": now,
            "access_count": 0,
            "tags": tags,
        }

        await self._es.index_document(self.INDEX, memory_id, doc)
        return memory_id

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    async def query_relevant(
        self,
        tenant_id: str,
        context: str,
        limit: int = 5,
    ) -> list:
        """Query memories relevant to the current context using text matching.

        Retrieves the most relevant memories for a tenant based on a
        full-text match against the ``content`` field. Each returned
        memory has its ``last_accessed`` timestamp and ``access_count``
        updated.

        Args:
            tenant_id: Tenant scope for the query.
            context: Free-text context string to match against.
            limit: Maximum number of memories to return.

        Returns:
            List of memory entry dicts, ordered by relevance.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"match": {"content": context}},
                    ]
                }
            },
            "size": limit,
        }

        result = await self._es.search_documents(self.INDEX, query)
        hits = result.get("hits", {}).get("hits", [])
        memories = [hit["_source"] for hit in hits]

        # Update access metadata for returned memories
        now = datetime.now(timezone.utc).isoformat()
        for memory in memories:
            try:
                await self._es.update_document(
                    self.INDEX,
                    memory["memory_id"],
                    {
                        "doc": {
                            "last_accessed": now,
                            "access_count": memory.get("access_count", 0) + 1,
                        }
                    },
                )
            except Exception as e:
                logger.warning(
                    f"Failed to update access metadata for memory "
                    f"{memory['memory_id']}: {e}"
                )

        return memories

    # ------------------------------------------------------------------
    # Decay and purge
    # ------------------------------------------------------------------

    async def decay_stale(self) -> int:
        """Reduce confidence of memories not accessed in 90 days. Purge below 0.1.

        Scans all memories whose ``last_accessed`` is older than
        ``DECAY_DAYS`` days ago. For each stale memory the confidence
        score is multiplied by ``DECAY_FACTOR`` (0.5). If the resulting
        confidence falls below ``PURGE_THRESHOLD`` (0.1) the memory is
        deleted.

        Returns:
            The number of memories affected (decayed + purged).
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.DECAY_DAYS)
        ).isoformat()

        query = {
            "query": {
                "range": {
                    "last_accessed": {"lte": cutoff},
                }
            },
            "size": 1000,
        }

        result = await self._es.search_documents(self.INDEX, query)
        hits = result.get("hits", {}).get("hits", [])
        affected = 0

        for hit in hits:
            source = hit["_source"]
            memory_id = source["memory_id"]
            old_confidence = source.get("confidence_score", 0.0)
            new_confidence = old_confidence * self.DECAY_FACTOR

            if new_confidence < self.PURGE_THRESHOLD:
                # Purge the memory
                try:
                    await self._es.delete_document(self.INDEX, memory_id)
                    affected += 1
                except Exception as e:
                    logger.warning(f"Failed to purge memory {memory_id}: {e}")
            else:
                # Decay the confidence
                try:
                    await self._es.update_document(
                        self.INDEX,
                        memory_id,
                        {"doc": {"confidence_score": new_confidence}},
                    )
                    affected += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to decay memory {memory_id}: {e}"
                    )

        return affected

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, memory_id: str, tenant_id: str) -> bool:
        """Delete a specific memory.

        Verifies the memory belongs to the given tenant before deleting.

        Args:
            memory_id: The memory to delete.
            tenant_id: Tenant scope — must match the memory's tenant_id.

        Returns:
            True if the memory was deleted, False if not found or
            tenant mismatch.
        """
        try:
            doc = await self._es.get_document(self.INDEX, memory_id)
            if doc is None:
                return False

            source = doc.get("_source", doc)
            if source.get("tenant_id") != tenant_id:
                return False

            await self._es.delete_document(self.INDEX, memory_id)
            return True
        except Exception as e:
            logger.warning(f"Failed to delete memory {memory_id}: {e}")
            return False

    # ------------------------------------------------------------------
    # List (for REST endpoint)
    # ------------------------------------------------------------------

    async def list_memories(
        self,
        tenant_id: str,
        memory_type: str = None,
        tags: list = None,
        page: int = 1,
        size: int = 20,
    ) -> dict:
        """List memories for a tenant with optional filtering.

        Args:
            tenant_id: Tenant scope.
            memory_type: Optional filter by memory type
                (``"pattern"`` or ``"preference"``).
            tags: Optional list of tags to filter by.
            page: Page number (1-based).
            size: Number of entries per page.

        Returns:
            Dict with ``items``, ``total``, ``page``, and ``size``.
        """
        must_clauses = [{"term": {"tenant_id": tenant_id}}]

        if memory_type:
            must_clauses.append({"term": {"memory_type": memory_type}})

        if tags:
            for tag in tags:
                must_clauses.append({"term": {"tags": tag}})

        from_offset = (page - 1) * size
        query = {
            "query": {"bool": {"must": must_clauses}},
            "sort": [{"created_at": {"order": "desc"}}],
            "from": from_offset,
            "size": size,
        }

        result = await self._es.search_documents(self.INDEX, query)
        hits = result.get("hits", {}).get("hits", [])
        total_value = result.get("hits", {}).get("total", {})
        if isinstance(total_value, dict):
            total = total_value.get("value", 0)
        else:
            total = total_value

        items = [hit["_source"] for hit in hits]

        return {
            "items": items,
            "total": total,
            "page": page,
            "size": size,
        }
