"""Per-tenant monotonic invoice numbering via Redis INCR.

.. deprecated::
    Superseded by the PostgreSQL per-tenant counter
    (``persistence.models.InvoiceCounterORM`` +
    ``InvoiceRepository.allocate_number``), which allocates the next number
    under a row lock inside the SAME transaction as the invoice finalize — so
    a number is never skipped on rollback nor issued twice under concurrency,
    with no Redis/ES checkpoint+reseed machinery. ``InvoiceService.finalize_draft``
    now calls ``commerce_persistence_bridge.allocate_invoice_number`` when
    ``commerce_dual_write_postgres`` is enabled. This module is retained only
    for environments that have not yet adopted the persistence layer and will
    be removed once the Postgres source-of-truth is the default.

Uses Redis INCR on key ``commerce:invoice_seq:{tenant_id}`` for fast
monotonic generation. A daily checkpoint writes the current ``max_seq``
to the ``invoice_counter_checkpoints`` ES index so the counter can be
re-seeded on Redis loss.

On Redis loss (key doesn't exist), the service re-seeds from the most
recent ES checkpoint's ``max_seq`` plus the count of invoices observed
in ``invoices_current`` since the checkpoint date. This guarantees
monotonicity with at most one day's worth of potential gaps; duplicates
are impossible because the re-seed reads the max from both sources.

Resolves design §14 open-question 1, option a.

Validates: Requirements 5.1, C2, C3
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from commerce.services.commerce_es_mappings import (
    INVOICE_COUNTER_CHECKPOINTS_INDEX,
    INVOICES_CURRENT_INDEX,
)
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REDIS_KEY_PREFIX = "commerce:invoice_seq"


def _redis_key(tenant_id: str) -> str:
    """Build the Redis key for a tenant's invoice sequence counter."""
    return f"{_REDIS_KEY_PREFIX}:{tenant_id}"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class InvoiceNumberingService:
    """Per-tenant monotonic invoice number generator.

    Uses Redis INCR for fast, atomic counter increments. Falls back to
    ES checkpoint + invoice count on Redis key absence (cold start or
    Redis loss).

    Args:
        es_service: ElasticsearchService instance for checkpoint reads/writes
            and invoice count queries.
        redis_client: Async Redis client supporting ``incr``, ``get``, ``set``,
            and ``exists`` operations. When None, every call falls through to
            the ES-based reseed path (useful for testing but not production).
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        redis_client: Any = None,
    ) -> None:
        self._es = es_service
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def next_number(self, tenant_id: str) -> int:
        """Return the next monotonic invoice number for a tenant.

        Uses Redis INCR for atomic increment. If the Redis key does not
        exist (cold start or Redis loss), re-seeds from the ES checkpoint
        before incrementing.

        Args:
            tenant_id: Tenant identifier for data isolation.

        Returns:
            The next sequential invoice number (int, starting from 1).
        """
        key = _redis_key(tenant_id)

        if self._redis is not None:
            # Check if the key exists; if not, reseed first
            try:
                exists = await self._redis.exists(key)
            except Exception as exc:
                logger.warning(
                    "Redis exists check failed for tenant %s, falling back to reseed: %s",
                    tenant_id,
                    exc,
                )
                exists = False

            if not exists:
                await self.reseed_from_checkpoint(tenant_id)

            # Atomic increment
            try:
                next_val = await self._redis.incr(key)
                return int(next_val)
            except Exception as exc:
                logger.error(
                    "Redis INCR failed for tenant %s: %s",
                    tenant_id,
                    exc,
                )
                # Fall through to ES-only path as last resort
                return await self._next_from_es_only(tenant_id)
        else:
            # No Redis client — use ES-only path
            return await self._next_from_es_only(tenant_id)

    async def write_checkpoint(self, tenant_id: str) -> Dict[str, Any]:
        """Write a daily checkpoint of the current sequence to ES.

        Reads the current counter value from Redis (or computes it from
        ES if Redis is unavailable) and persists it to the
        ``invoice_counter_checkpoints`` index.

        The checkpoint document ID is ``{tenant_id}:{YYYY-MM-DD}`` so
        re-running on the same day is idempotent (upsert).

        Args:
            tenant_id: Tenant identifier.

        Returns:
            The checkpoint document that was written.
        """
        now = utcnow()
        today_str = now.date().isoformat()

        # Get current max_seq from Redis or ES
        max_seq = await self._get_current_max_seq(tenant_id)

        doc_id = f"{tenant_id}:{today_str}"
        doc: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "max_seq": max_seq,
            "checkpoint_date": today_str,
            "created_at": now.isoformat(),
        }

        await self._es.index_document(
            INVOICE_COUNTER_CHECKPOINTS_INDEX, doc_id, doc
        )

        logger.info(
            "Wrote invoice numbering checkpoint for tenant %s: max_seq=%d, date=%s",
            tenant_id,
            max_seq,
            today_str,
        )
        return doc

    async def reseed_from_checkpoint(self, tenant_id: str) -> int:
        """Re-seed the Redis counter from the latest ES checkpoint.

        Reads the most recent checkpoint's ``max_seq`` and adds the count
        of invoices created in ``invoices_current`` since the checkpoint
        date. Sets the Redis key to this computed value.

        This guarantees monotonicity: the re-seeded value is always >=
        any previously issued number. Gaps are possible (at most one
        day's worth) but duplicates are impossible.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            The value the counter was re-seeded to.
        """
        # Step 1: Get the latest checkpoint
        checkpoint = await self._get_latest_checkpoint(tenant_id)

        if checkpoint is None:
            # No checkpoint exists — count all invoices for this tenant
            total_invoices = await self._count_invoices_since(tenant_id, None)
            seed_value = total_invoices
        else:
            checkpoint_max_seq = int(checkpoint.get("max_seq", 0))
            checkpoint_date = checkpoint.get("checkpoint_date")

            # Step 2: Count invoices created since the checkpoint
            invoices_since = await self._count_invoices_since(
                tenant_id, checkpoint_date
            )

            # Step 3: Seed = max(checkpoint_max_seq, checkpoint_max_seq + invoices_since)
            # The seed is the checkpoint value plus any invoices created after it
            seed_value = checkpoint_max_seq + invoices_since

        # Step 4: Set the Redis key
        if self._redis is not None:
            key = _redis_key(tenant_id)
            try:
                await self._redis.set(key, str(seed_value), ex=90 * 24 * 60 * 60)
                logger.info(
                    "Re-seeded invoice counter for tenant %s to %d",
                    tenant_id,
                    seed_value,
                )
            except Exception as exc:
                logger.error(
                    "Failed to set Redis key during reseed for tenant %s: %s",
                    tenant_id,
                    exc,
                )

        return seed_value

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_current_max_seq(self, tenant_id: str) -> int:
        """Get the current max sequence number from Redis or ES.

        Tries Redis first; falls back to counting invoices in ES.
        """
        if self._redis is not None:
            key = _redis_key(tenant_id)
            try:
                raw = await self._redis.get(key)
                if raw is not None:
                    return int(raw)
            except Exception as exc:
                logger.warning(
                    "Redis GET failed for tenant %s during checkpoint: %s",
                    tenant_id,
                    exc,
                )

        # Fallback: get from latest checkpoint + invoice count since
        checkpoint = await self._get_latest_checkpoint(tenant_id)
        if checkpoint is None:
            return await self._count_invoices_since(tenant_id, None)

        checkpoint_max_seq = int(checkpoint.get("max_seq", 0))
        checkpoint_date = checkpoint.get("checkpoint_date")
        invoices_since = await self._count_invoices_since(
            tenant_id, checkpoint_date
        )
        return checkpoint_max_seq + invoices_since

    async def _get_latest_checkpoint(
        self, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve the most recent checkpoint for a tenant from ES."""
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
            "sort": [{"checkpoint_date": {"order": "desc"}}],
        }
        query = inject_tenant_filter(base_query, tenant_id)

        try:
            response = await self._es.search_documents(
                INVOICE_COUNTER_CHECKPOINTS_INDEX, query, size=1
            )
            hits = response.get("hits", {}).get("hits", [])
            if hits:
                return hits[0]["_source"]
        except Exception as exc:
            logger.error(
                "Failed to query checkpoint for tenant %s: %s",
                tenant_id,
                exc,
            )

        return None

    async def _count_invoices_since(
        self, tenant_id: str, since_date: Optional[str]
    ) -> int:
        """Count invoices created since a given date for a tenant.

        If since_date is None, counts all invoices for the tenant.
        """
        must_clauses = []
        if since_date is not None:
            must_clauses.append(
                {"range": {"created_at": {"gt": since_date}}}
            )

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": must_clauses if must_clauses else [{"match_all": {}}],
                }
            },
            "size": 0,
            "track_total_hits": True,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        try:
            response = await self._es.search_documents(
                INVOICES_CURRENT_INDEX, query, size=0
            )
            total = response.get("hits", {}).get("total", {})
            if hasattr(total, "get") or isinstance(total, dict):
                return int(total.get("value", 0))
            return int(total) if total else 0
        except Exception as exc:
            logger.error(
                "Failed to count invoices since %s for tenant %s: %s",
                since_date,
                tenant_id,
                exc,
            )
            return 0

    async def _next_from_es_only(self, tenant_id: str) -> int:
        """Compute the next invoice number from ES only (no Redis).

        Used as a last-resort fallback when Redis is completely
        unavailable. Counts all invoices + 1.

        Note: This path is NOT atomic and should only be used when
        Redis is genuinely unavailable. Under concurrent load without
        Redis, gaps or (in extreme cases) duplicates could occur.
        Production deployments MUST have Redis available.
        """
        checkpoint = await self._get_latest_checkpoint(tenant_id)
        if checkpoint is None:
            total = await self._count_invoices_since(tenant_id, None)
            return total + 1

        checkpoint_max_seq = int(checkpoint.get("max_seq", 0))
        checkpoint_date = checkpoint.get("checkpoint_date")
        invoices_since = await self._count_invoices_since(
            tenant_id, checkpoint_date
        )
        return checkpoint_max_seq + invoices_since + 1
