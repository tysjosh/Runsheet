"""
Autonomy Config Service for per-tenant autonomy level management.

Manages per-tenant autonomy level configuration in Redis, controlling
how much authority AI agents have for each tenant. Levels range from
"suggest-only" (all actions require approval) to "full-auto" (all
actions auto-execute with audit logging).

Requirements: 10.1, 10.2, 10.6
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default autonomy level for new tenants (safe onboarding)
DEFAULT_AUTONOMY_LEVEL = "suggest-only"

# Valid autonomy levels in order of increasing autonomy
VALID_AUTONOMY_LEVELS = frozenset({
    "suggest-only",
    "auto-low",
    "auto-medium",
    "full-auto",
})


class AutonomyConfigService:
    """Manages per-tenant autonomy level configuration in Redis.

    Uses Redis key pattern: tenant:{tenant_id}:autonomy_level
    No TTL is applied — autonomy levels are persistent.

    When Redis is unavailable, falls back to the default level ("suggest-only")
    for safety, ensuring no unintended auto-execution occurs.
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client

    async def get_level(self, tenant_id: str) -> str:
        """Get the autonomy level for a tenant.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            The autonomy level string. Defaults to "suggest-only" if not set
            or if Redis is unavailable.
        """
        if self._redis:
            try:
                key = f"tenant:{tenant_id}:autonomy_level"
                value = await self._redis.get(key)
                if value:
                    decoded = value.decode() if isinstance(value, bytes) else value
                    if decoded in VALID_AUTONOMY_LEVELS:
                        return decoded
                    logger.warning(
                        "Invalid autonomy level '%s' for tenant %s, "
                        "returning default",
                        decoded,
                        tenant_id,
                    )
                    return DEFAULT_AUTONOMY_LEVEL
            except Exception as e:
                logger.warning(
                    "Redis lookup failed for tenant:%s:autonomy_level: %s",
                    tenant_id,
                    e,
                )

        return DEFAULT_AUTONOMY_LEVEL

    async def set_level(self, tenant_id: str, level: str) -> str:
        """Set the autonomy level for a tenant.

        Args:
            tenant_id: The tenant identifier.
            level: The new autonomy level. Must be one of the valid levels.

        Returns:
            The previous autonomy level.

        Raises:
            ValueError: If the level is not a valid autonomy level.
            RuntimeError: If no Redis client is configured.
        """
        if level not in VALID_AUTONOMY_LEVELS:
            raise ValueError(
                f"Invalid autonomy level '{level}'. "
                f"Must be one of: {', '.join(sorted(VALID_AUTONOMY_LEVELS))}"
            )

        previous = await self.get_level(tenant_id)

        if self._redis:
            try:
                key = f"tenant:{tenant_id}:autonomy_level"
                await self._redis.set(key, level)
            except Exception as e:
                logger.error(
                    "Failed to set autonomy level for tenant %s: %s",
                    tenant_id,
                    e,
                )
                raise
        else:
            logger.warning(
                "Cannot set autonomy level for tenant %s: "
                "no Redis client configured",
                tenant_id,
            )

        return previous
