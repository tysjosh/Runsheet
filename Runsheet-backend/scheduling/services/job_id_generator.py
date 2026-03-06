"""
Job ID generator using Redis atomic counter.

Generates sequential job IDs in the format JOB_{number} using Redis INCR
for atomicity across multiple backend instances. Falls back to UUID-based
IDs if Redis is unavailable.

Requirement 2.2: WHEN a job is created, THE Scheduling_API SHALL generate
a unique job_id with the format JOB_{sequential_number}.
"""

import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


class JobIdGenerator:
    """Generates JOB_{sequential_number} IDs using Redis atomic counter."""

    KEY = "scheduling:job_id_counter"

    def __init__(self, redis_url: Optional[str] = None):
        self._redis_url = redis_url
        self._client = None

    async def _get_client(self):
        """Lazily connect to Redis, reusing the existing connection pattern."""
        if self._client is None and self._redis_url:
            try:
                import redis.asyncio as redis
                self._client = redis.from_url(
                    self._redis_url, decode_responses=True
                )
            except Exception as e:
                logger.warning("Failed to connect to Redis for job ID generation: %s", e)
                self._client = None
        return self._client

    async def next_id(self) -> str:
        """
        Generate the next job ID.

        Uses Redis INCR on scheduling:job_id_counter for atomic sequential IDs.
        Falls back to UUID-based ID (JOB_{uuid[:8]}) if Redis is unavailable.

        Returns:
            A unique job ID string in JOB_{number} or JOB_{uuid_prefix} format.
        """
        client = await self._get_client()
        if client:
            try:
                counter = await client.incr(self.KEY)
                return f"JOB_{counter}"
            except Exception as e:
                logger.warning(
                    "Redis INCR failed for job ID generation, falling back to UUID: %s", e
                )

        # Fallback: UUID-based ID
        return f"JOB_{uuid.uuid4().hex[:8]}"

    async def close(self):
        """Close the Redis connection if open."""
        if self._client:
            await self._client.close()
            self._client = None
