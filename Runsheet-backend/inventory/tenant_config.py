"""
Tenant Inventory Configuration Service.

Manages per-tenant inventory settings in Redis, controlling how inventory
checks influence operational decisions (job assignment blocking, readiness
checks, auto-consumption on job completion).

Follows the same Redis-backed per-tenant pattern as AutonomyConfigService.

Requirements: 1.4, 5.1
"""
import json
import logging
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)

# Redis key prefix for inventory settings
INVENTORY_CONFIG_PREFIX = "tenant:{tenant_id}:inventory_settings"


@dataclass
class InventorySettings:
    """Per-tenant inventory configuration settings.

    Attributes:
        block_on_critical_shortage: When True, the AssetReadinessChecker will
            reject job assignments if critical parts are out of stock.
            Default False (fail-open — assignments proceed with warnings).
        readiness_check_enabled: When True, the AssetReadinessChecker will
            query inventory during job assignment. When False, readiness
            checks are skipped entirely. Default True.
        auto_consume_on_completion: When True, parts listed in a maintenance
            job's cargo manifest are automatically deducted from inventory
            when the job transitions to completed. Default True.
    """

    block_on_critical_shortage: bool = False
    readiness_check_enabled: bool = True
    auto_consume_on_completion: bool = True


# Default settings used when Redis is unavailable or tenant has no config
DEFAULT_INVENTORY_SETTINGS = InventorySettings()


class TenantInventoryConfigService:
    """Manages per-tenant inventory settings in Redis.

    Uses Redis key pattern: ``tenant:{tenant_id}:inventory_settings``
    Values are stored as JSON strings.

    When Redis is unavailable, returns default settings (fail-open):
    - block_on_critical_shortage: False (don't block assignments)
    - readiness_check_enabled: True (check readiness)
    - auto_consume_on_completion: True (auto-consume parts)

    This ensures inventory checks never block operations due to
    infrastructure failures.

    Requirements: 1.4, 5.1
    """

    def __init__(self, redis_client=None):
        """
        Args:
            redis_client: An async Redis client instance. If None, all
                operations return defaults.
        """
        self._redis = redis_client

    def _get_key(self, tenant_id: str) -> str:
        """Build the Redis key for a tenant's inventory settings."""
        return f"tenant:{tenant_id}:inventory_settings"

    async def get_settings(self, tenant_id: str) -> InventorySettings:
        """Get inventory settings for a tenant.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            InventorySettings with the tenant's configuration. Returns
            defaults if not set or if Redis is unavailable.
        """
        if not self._redis:
            logger.debug(
                "TenantInventoryConfigService: no Redis client, "
                "returning defaults for tenant %s",
                tenant_id,
            )
            return InventorySettings()

        try:
            key = self._get_key(tenant_id)
            value = await self._redis.get(key)
            if value is None:
                return InventorySettings()

            raw = value.decode() if isinstance(value, bytes) else value
            data = json.loads(raw)
            return InventorySettings(
                block_on_critical_shortage=data.get(
                    "block_on_critical_shortage", False
                ),
                readiness_check_enabled=data.get(
                    "readiness_check_enabled", True
                ),
                auto_consume_on_completion=data.get(
                    "auto_consume_on_completion", True
                ),
            )
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "TenantInventoryConfigService: invalid JSON for tenant %s: %s "
                "— returning defaults",
                tenant_id,
                e,
            )
            return InventorySettings()
        except Exception as e:
            logger.warning(
                "TenantInventoryConfigService: Redis error for tenant %s: %s "
                "— returning defaults",
                tenant_id,
                e,
            )
            return InventorySettings()

    async def set_settings(
        self, tenant_id: str, settings: InventorySettings
    ) -> None:
        """Set inventory settings for a tenant.

        Args:
            tenant_id: The tenant identifier.
            settings: The InventorySettings to persist.

        Raises:
            RuntimeError: If no Redis client is configured.
        """
        if not self._redis:
            raise RuntimeError(
                "Cannot set inventory settings: no Redis client configured"
            )

        key = self._get_key(tenant_id)
        value = json.dumps(asdict(settings))
        await self._redis.set(key, value)
        logger.info(
            "Inventory settings updated for tenant %s: %s",
            tenant_id,
            value,
        )

    async def get_block_on_critical_shortage(self, tenant_id: str) -> bool:
        """Check if tenant has block_on_critical_shortage enabled.

        Convenience method for the AssetReadinessChecker.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            True if the tenant has blocking enabled, False otherwise.
        """
        settings = await self.get_settings(tenant_id)
        return settings.block_on_critical_shortage

    async def get_readiness_check_enabled(self, tenant_id: str) -> bool:
        """Check if tenant has readiness checks enabled.

        Convenience method for the JobService.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            True if readiness checks are enabled, False otherwise.
        """
        settings = await self.get_settings(tenant_id)
        return settings.readiness_check_enabled

    async def get_auto_consume_on_completion(self, tenant_id: str) -> bool:
        """Check if tenant has auto-consume on completion enabled.

        Convenience method for the JobService.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            True if auto-consumption is enabled, False otherwise.
        """
        settings = await self.get_settings(tenant_id)
        return settings.auto_consume_on_completion
