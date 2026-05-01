"""
Risk Classification Registry for mutation tools.

Classifies mutation tool invocations by risk level (low, medium, high)
with support for Redis-backed overrides and configurable defaults.
Unknown tool names default to HIGH risk for safety.

Requirements: 1.4, 1.5
"""
from enum import Enum
from typing import Dict, Optional
import json
import logging

logger = logging.getLogger(__name__)


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Default risk classification for mutation tools
DEFAULT_RISK_REGISTRY: Dict[str, RiskLevel] = {
    # Low risk - execute immediately
    "update_fuel_threshold": RiskLevel.LOW,
    # Medium risk - brief confirmation window
    "assign_asset_to_job": RiskLevel.MEDIUM,
    "update_job_status": RiskLevel.MEDIUM,
    "create_job": RiskLevel.MEDIUM,
    "escalate_shipment": RiskLevel.MEDIUM,
    "request_fuel_refill": RiskLevel.MEDIUM,
    # High risk - explicit approval required
    "cancel_job": RiskLevel.HIGH,
    "reassign_rider": RiskLevel.HIGH,
}


class RiskRegistry:
    """Classifies mutation tool invocations by risk level.

    Uses a default in-memory registry with optional Redis-backed overrides.
    When a Redis client is provided, overrides are checked first before
    falling back to the default registry. Unknown tools default to HIGH risk.
    """

    def __init__(self, redis_client=None):
        self._defaults = dict(DEFAULT_RISK_REGISTRY)
        self._redis = redis_client

    async def classify(self, tool_name: str, tenant_id: str = "") -> RiskLevel:
        """Return the risk level for a tool, checking tenant-scoped Redis overrides first.

        Checks in order:
        1. Tenant-specific override: risk_override:{tenant_id}:{tool_name}
        2. Global override: risk_override:{tool_name}
        3. Default registry

        Args:
            tool_name: The name of the mutation tool to classify.
            tenant_id: Optional tenant for tenant-scoped overrides.

        Returns:
            The RiskLevel for the tool. Defaults to HIGH for unknown tools.
        """
        if self._redis:
            # Check tenant-specific override first
            if tenant_id:
                try:
                    override = await self._redis.get(
                        f"risk_override:{tenant_id}:{tool_name}"
                    )
                    if override:
                        value = override.decode() if isinstance(override, bytes) else override
                        return RiskLevel(value)
                except Exception as e:
                    logger.warning(
                        "Redis lookup failed for risk_override:%s:%s: %s",
                        tenant_id, tool_name, e,
                    )

            # Check global override
            try:
                override = await self._redis.get(f"risk_override:{tool_name}")
                if override:
                    value = override.decode() if isinstance(override, bytes) else override
                    return RiskLevel(value)
            except Exception as e:
                logger.warning(
                    "Redis lookup failed for risk_override:%s: %s",
                    tool_name, e,
                )

        return self._defaults.get(tool_name, RiskLevel.HIGH)

    async def set_override(
        self, tool_name: str, level: RiskLevel, tenant_id: str = ""
    ) -> None:
        """Set a Redis-backed risk level override.

        Args:
            tool_name: The name of the mutation tool to override.
            level: The new RiskLevel to assign.
            tenant_id: Optional tenant for tenant-scoped override.
                If empty, sets a global override.

        Raises:
            RuntimeError: If no Redis client is configured.
        """
        if self._redis:
            key = (
                f"risk_override:{tenant_id}:{tool_name}"
                if tenant_id
                else f"risk_override:{tool_name}"
            )
            await self._redis.set(key, level.value)
        else:
            logger.warning(
                "Cannot set risk override for %s: no Redis client configured",
                tool_name,
            )
