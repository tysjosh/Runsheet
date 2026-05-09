"""
Redis-backed idempotency service for webhook event deduplication.

Ensures at-least-once delivery semantics by tracking processed event_ids
in Redis with a configurable TTL. Reuses the same Redis connection pattern
as RedisSessionStore.

Keys are namespaced per-tenant (``idemp:{tenant_id}:{event_id}``) so two
tenants that happen to emit the same ``event_id`` cannot collide. Older
callers that do not yet know their tenant can pass ``tenant_id=None``
and fall back to the legacy ``idemp:{event_id}`` shape — new writes
should always supply a tenant.

Requirements: 1.4, 1.5, 1.7, 9.2 (tenant isolation)
"""

import logging
from datetime import timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class IdempotencyService:
    """
    Redis-backed idempotency store for webhook event deduplication.

    Uses a key prefix of "idemp:" for namespace isolation and a configurable
    TTL (default 72 hours) for automatic expiry of processed event markers.

    Keys are tenant-scoped to avoid cross-tenant collisions: a webhook
    received for tenant A with ``event_id="evt_123"`` must not be
    considered a duplicate of the same ``event_id`` arriving for tenant
    B (the same upstream provider can legitimately replay an event id
    under a different tenant's webhook secret).
    """

    PREFIX = "idemp:"

    def __init__(self, redis_url: str, ttl_hours: int = 72):
        self.redis_url = redis_url
        self.ttl = timedelta(hours=ttl_hours)
        self.client = None

    async def connect(self) -> None:
        """Establish connection to Redis."""
        import redis.asyncio as redis
        self.client = redis.from_url(self.redis_url, decode_responses=True)

    async def disconnect(self) -> None:
        """Close the Redis connection."""
        if self.client:
            await self.client.close()
            self.client = None

    def _get_key(self, event_id: str, tenant_id: Optional[str] = None) -> str:
        """Compose the Redis key used for idempotency tracking.

        Tenant-scoped by default (``idemp:{tenant_id}:{event_id}``) so
        two tenants with the same upstream event id cannot collide.
        Callers that pre-date tenant scoping (or internal healthchecks)
        can still pass ``tenant_id=None`` and get the legacy
        ``idemp:{event_id}`` shape.
        """
        if tenant_id:
            return f"{self.PREFIX}{tenant_id}:{event_id}"
        return f"{self.PREFIX}{event_id}"

    async def is_duplicate(self, event_id: str, tenant_id: Optional[str] = None) -> bool:
        """Check if event_id was already processed for the given tenant."""
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        key = self._get_key(event_id, tenant_id)
        return await self.client.exists(key) > 0

    async def mark_processed(self, event_id: str, tenant_id: Optional[str] = None) -> None:
        """Store event_id with TTL for the given tenant."""
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        key = self._get_key(event_id, tenant_id)
        ttl_seconds = int(self.ttl.total_seconds())
        await self.client.setex(key, ttl_seconds, "1")
        logger.debug(
            "Marked event %s as processed for tenant=%s (TTL: %s hours)",
            event_id,
            tenant_id or "<unscoped>",
            self.ttl.total_seconds() / 3600,
        )

    async def health_check(self) -> bool:
        """Check Redis connectivity."""
        if not self.client:
            return False
        try:
            return await self.client.ping() is True
        except Exception:
            return False
