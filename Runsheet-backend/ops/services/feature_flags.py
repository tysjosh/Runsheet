"""
Feature Flag Service for per-tenant Ops Intelligence Layer rollout.

Controls enabling/disabling the Ops Intelligence Layer per tenant using
Redis-backed flags. Supports rollback with optional data purge from ops indices.
All flag changes are logged with tenant_id, action, and user_id for audit.

Follows the same Redis connection pattern as IdempotencyService.

Requirements: 27.1-27.6
"""

import logging
from typing import Optional

from ops.services.ops_metrics import ops_feature_flag_changes_total

logger = logging.getLogger(__name__)


class FeatureFlagService:
    """
    Redis-backed per-tenant feature flag service for the Ops Intelligence Layer.

    Uses key prefix ``ops_ff:`` for namespace isolation. A tenant is considered
    enabled when its key exists in Redis with value ``"1"``.

    Validates:
    - Req 27.1: Enable/disable per tenant_id via configuration endpoint
    - Req 27.2: Disabled tenants skip webhook processing
    - Req 27.3: Disabled tenants get 404 on ops API
    - Req 27.5: Rollback disables + optional data purge
    - Req 27.6: Log all flag changes with tenant_id, action, user_id
    """

    PREFIX = "ops_ff:"

    def __init__(self, redis_url: str, ops_es_service=None):
        """
        Args:
            redis_url: Redis connection URL.
            ops_es_service: Optional OpsElasticsearchService instance for
                data purge during rollback.
        """
        self.redis_url = redis_url
        self.ops_es_service = ops_es_service
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

    def _get_key(self, tenant_id: str) -> str:
        """Build the Redis key for a tenant's feature flag."""
        return f"{self.PREFIX}{tenant_id}"

    async def is_enabled(self, tenant_id: str) -> bool:
        """
        Check whether the Ops Intelligence Layer is enabled for a tenant.

        Returns True if the flag key exists in Redis, False otherwise.
        Validates: Req 27.1
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        key = self._get_key(tenant_id)
        return await self.client.exists(key) > 0

    async def enable(self, tenant_id: str, user_id: str) -> None:
        """
        Enable the Ops Intelligence Layer for a tenant.

        Sets the Redis flag key to ``"1"`` (no TTL — persists until explicitly
        disabled or rolled back).

        Validates: Req 27.1, 27.6
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        key = self._get_key(tenant_id)
        await self.client.set(key, "1")
        ops_feature_flag_changes_total.labels(tenant_id=tenant_id, action="enable").inc()
        logger.info(
            "Feature flag change: tenant_id=%s, action=enable, user_id=%s",
            tenant_id,
            user_id,
        )

    async def disable(self, tenant_id: str, user_id: str) -> None:
        """
        Disable the Ops Intelligence Layer for a tenant.

        Removes the Redis flag key so that ``is_enabled`` returns False.

        Validates: Req 27.2-27.4, 27.6
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        key = self._get_key(tenant_id)
        await self.client.delete(key)
        ops_feature_flag_changes_total.labels(tenant_id=tenant_id, action="disable").inc()
        logger.info(
            "Feature flag change: tenant_id=%s, action=disable, user_id=%s",
            tenant_id,
            user_id,
        )

    async def rollback(
        self,
        tenant_id: str,
        user_id: str,
        purge_data: bool = False,
    ) -> None:
        """
        Rollback the Ops Intelligence Layer for a tenant.

        Disables the feature flag and optionally purges the tenant's data
        from all ops Elasticsearch indices (shipments_current, shipment_events,
        riders_current, ops_poison_queue).

        Validates: Req 27.5, 27.6
        """
        # Disable the flag first
        await self.disable(tenant_id, user_id)

        if purge_data:
            await self._purge_tenant_data(tenant_id)

        ops_feature_flag_changes_total.labels(tenant_id=tenant_id, action="rollback").inc()
        logger.info(
            "Feature flag change: tenant_id=%s, action=rollback, user_id=%s, purge_data=%s",
            tenant_id,
            user_id,
            purge_data,
        )

    async def _purge_tenant_data(self, tenant_id: str) -> None:
        """
        Delete all documents belonging to *tenant_id* from ops indices.

        Uses ``delete_by_query`` on each ops index. Requires an
        ``OpsElasticsearchService`` instance to be set.
        """
        if self.ops_es_service is None:
            logger.warning(
                "Cannot purge tenant data: OpsElasticsearchService not configured"
            )
            return

        from ops.services.ops_es_service import OpsElasticsearchService

        indices = [
            OpsElasticsearchService.SHIPMENTS_CURRENT,
            OpsElasticsearchService.SHIPMENT_EVENTS,
            OpsElasticsearchService.RIDERS_CURRENT,
            OpsElasticsearchService.POISON_QUEUE,
        ]

        for index_name in indices:
            try:
                es_client = self.ops_es_service.client
                if es_client.indices.exists(index=index_name):
                    result = es_client.delete_by_query(
                        index=index_name,
                        body={
                            "query": {
                                "term": {"tenant_id": tenant_id}
                            }
                        },
                        refresh=True,
                    )
                    deleted = result.get("deleted", 0)
                    logger.info(
                        "Purged %d documents from %s for tenant_id=%s",
                        deleted,
                        index_name,
                        tenant_id,
                    )
            except Exception as e:
                logger.error(
                    "Failed to purge tenant data from %s for tenant_id=%s: %s",
                    index_name,
                    tenant_id,
                    e,
                )

    async def health_check(self) -> bool:
        """Check Redis connectivity."""
        if not self.client:
            return False
        try:
            return await self.client.ping() is True
        except Exception:
            return False
