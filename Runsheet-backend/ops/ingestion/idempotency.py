"""
Redis-backed idempotency service for webhook event deduplication.

Ensures at-least-once delivery semantics by tracking processed event_ids
in Redis with a configurable TTL. Reuses the same Redis connection pattern
as RedisSessionStore.

Requirements: 1.4, 1.5, 1.7
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

    def _get_key(self, event_id: str) -> str:
        return f"{self.PREFIX}{event_id}"

    async def is_duplicate(self, event_id: str) -> bool:
        """Check if event_id was already processed."""
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        key = self._get_key(event_id)
        return await self.client.exists(key) > 0

    async def mark_processed(self, event_id: str) -> None:
        """Store event_id with TTL."""
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        key = self._get_key(event_id)
        ttl_seconds = int(self.ttl.total_seconds())
        await self.client.setex(key, ttl_seconds, "1")
        logger.debug("Marked event %s as processed (TTL: %s hours)", event_id, self.ttl.total_seconds() / 3600)

    async def health_check(self) -> bool:
        """Check Redis connectivity."""
        if not self.client:
            return False
        try:
            return await self.client.ping() is True
        except Exception:
            return False
