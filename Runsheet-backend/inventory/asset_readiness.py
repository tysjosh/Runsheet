"""
Asset Readiness Checker.

Verifies critical maintenance parts availability before truck assignment.
Queries the inventory index for items in critical categories (tires,
brake_parts, engine_parts) that are compatible with the given asset type.
Returns a ReadinessResult indicating whether assignment should proceed,
warn, or block.

Follows fail-open design: on any ES error, returns READY with empty parts
list so that dispatching is never blocked by infrastructure failures.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from inventory.es_mappings import INVENTORY_INDEX

logger = logging.getLogger(__name__)


class ReadinessStatus(str, Enum):
    """Status classification for asset readiness checks."""

    READY = "ready"          # All critical parts in stock
    WARNING = "warning"      # Some parts low stock
    CRITICAL = "critical"    # Some parts out of stock
    BLOCKED = "blocked"      # Blocked by tenant policy


@dataclass
class PartAvailability:
    """Availability details for a single inventory part."""

    item_id: str
    name: str
    category: str
    status: str              # in_stock, low_stock, out_of_stock
    quantity: int
    min_threshold: int
    location: str


@dataclass
class ReadinessResult:
    """Result of an asset readiness check."""

    status: ReadinessStatus
    parts_checked: List[PartAvailability] = field(default_factory=list)
    missing_parts: List[PartAvailability] = field(default_factory=list)
    low_parts: List[PartAvailability] = field(default_factory=list)
    blocked: bool = False
    block_reason: Optional[str] = None


class AssetReadinessChecker:
    """Verifies critical maintenance parts availability before truck assignment.

    Queries the inventory index for items in critical categories (tires,
    brake_parts, engine_parts) that are compatible with the given asset type.
    Returns a ReadinessResult indicating whether assignment should proceed,
    warn, or block.

    Classification logic:
    - READY: all critical parts have status in_stock
    - WARNING: at least one critical part has status low_stock, none out_of_stock
    - CRITICAL: at least one critical part has status out_of_stock
    - BLOCKED: CRITICAL + tenant has block_on_critical_shortage enabled

    Fail-open: on any ES error, returns READY with empty parts list and logs
    a warning. Assignment always proceeds unless explicitly blocked by tenant
    policy.

    Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
    """

    CRITICAL_CATEGORIES = ["tires", "brake_parts", "engine_parts"]

    def __init__(self, es_service, tenant_config_service=None):
        """
        Args:
            es_service: An ElasticsearchService instance for querying inventory.
            tenant_config_service: A TenantInventoryConfigService instance for
                checking tenant blocking policy. If None, blocking is never
                applied.
        """
        self._es = es_service
        self._tenant_config = tenant_config_service

    async def check_readiness(
        self, asset_id: str, asset_type: str, tenant_id: str
    ) -> ReadinessResult:
        """Check critical parts availability for an asset type.

        Queries the inventory index for Critical_Parts (tires, brake_parts,
        engine_parts) compatible with the given asset type. Classifies the
        result based on stock statuses and tenant policy.

        Args:
            asset_id: The asset being assigned.
            asset_type: The asset's type (e.g., "vehicle", "vessel").
            tenant_id: Tenant scope.

        Returns:
            ReadinessResult with status and part details.
        """
        try:
            parts = await self._query_critical_parts(asset_type, tenant_id)
        except Exception as e:
            # Fail-open: return READY with empty parts on any ES error
            logger.warning(
                "AssetReadinessChecker: ES query failed for asset_id=%s, "
                "asset_type=%s, tenant_id=%s — failing open (READY). Error: %s",
                asset_id,
                asset_type,
                tenant_id,
                e,
            )
            return ReadinessResult(status=ReadinessStatus.READY)

        # Classify parts by status
        missing_parts = [p for p in parts if p.status == "out_of_stock"]
        low_parts = [p for p in parts if p.status == "low_stock"]

        # Determine base status
        if missing_parts:
            status = ReadinessStatus.CRITICAL
        elif low_parts:
            status = ReadinessStatus.WARNING
        else:
            status = ReadinessStatus.READY

        # Check tenant blocking policy
        blocked = False
        block_reason = None
        if status == ReadinessStatus.CRITICAL:
            blocking_enabled = await self._is_blocking_enabled(tenant_id)
            if blocking_enabled:
                status = ReadinessStatus.BLOCKED
                blocked = True
                missing_names = ", ".join(p.name for p in missing_parts)
                block_reason = (
                    f"Assignment blocked: critical parts out of stock "
                    f"({missing_names}). Tenant policy "
                    f"block_on_critical_shortage is enabled."
                )

        return ReadinessResult(
            status=status,
            parts_checked=parts,
            missing_parts=missing_parts,
            low_parts=low_parts,
            blocked=blocked,
            block_reason=block_reason,
        )

    async def _query_critical_parts(
        self, asset_type: str, tenant_id: str
    ) -> List[PartAvailability]:
        """Query inventory for critical parts compatible with asset_type.

        Searches the inventory index for items that:
        - Belong to the given tenant
        - Are in one of the CRITICAL_CATEGORIES
        - Have the asset_type in their compatible_assets list

        Args:
            asset_type: The asset type to check compatibility for.
            tenant_id: Tenant scope.

        Returns:
            List of PartAvailability objects for matching items.

        Raises:
            Exception: If the ES query fails (caller handles fail-open).
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"category": self.CRITICAL_CATEGORIES}},
                        {"term": {"compatible_assets": asset_type}},
                    ]
                }
            },
            "size": 50,
        }

        response = await self._es.search_documents(
            INVENTORY_INDEX, query, size=50
        )

        parts = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            parts.append(
                PartAvailability(
                    item_id=source.get("item_id", ""),
                    name=source.get("name", ""),
                    category=source.get("category", ""),
                    status=source.get("status", ""),
                    quantity=source.get("quantity", 0),
                    min_threshold=source.get("min_threshold", 0),
                    location=source.get("location", ""),
                )
            )

        return parts

    async def _is_blocking_enabled(self, tenant_id: str) -> bool:
        """Check if tenant has block_on_critical_shortage enabled.

        If no tenant config service is available, returns False (fail-open).

        Args:
            tenant_id: The tenant identifier.

        Returns:
            True if the tenant has blocking enabled, False otherwise.
        """
        if self._tenant_config is None:
            return False

        try:
            return await self._tenant_config.get_block_on_critical_shortage(
                tenant_id
            )
        except Exception as e:
            logger.warning(
                "AssetReadinessChecker: failed to check tenant blocking "
                "policy for tenant_id=%s — defaulting to not blocked. "
                "Error: %s",
                tenant_id,
                e,
            )
            return False
