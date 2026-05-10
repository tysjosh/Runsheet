"""
Asset Certification Expiry Cron Agent — daily fleet compliance checks.

Autonomous background agent that runs once daily (every 86400 seconds)
and executes the asset certification expiry check for all tenants with
certifications in the system:

1. ``check_expiry_alerts()`` — generates 60/30/7-day expiry alerts and
   transitions certification statuses (valid → expiring_soon → expired)

Discovers tenants via a ``terms`` aggregation on the
``asset_certifications`` index (same pattern as
``driver_expiry_cron_agent``). Exceptions from a single tenant are
logged but do not abort the sweep.

Registered with the ``AgentScheduler`` from ``bootstrap/compliance.py``
(Task 8.11).

Validates: Requirements 13.2, 13.3, 13.4
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from compliance.services.compliance_es_mappings import ASSET_CERTIFICATIONS_INDEX
from compliance.services.asset_certification_service import (
    AssetCertificationService,
)
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Poll interval: 24 hours (daily cron)
ASSET_CERT_EXPIRY_POLL_INTERVAL_SECONDS = 86_400

# Cooldown: 24 hours (one run per day, no duplicates)
ASSET_CERT_EXPIRY_COOLDOWN_MINUTES = 1440

# Maximum distinct tenants per sweep
_MAX_TENANTS_PER_SWEEP = 10_000


class AssetCertExpiryCronAgent(AutonomousAgentBase):
    """Daily cron agent for asset certification expiry checks.

    Polls once every 24 hours and, for each tenant with asset
    certifications in the system, runs:
    1. ``check_expiry_alerts()`` — 60/30/7-day threshold alerts and
       status transitions (valid → expiring_soon → expired)

    Args:
        es_service: Elasticsearch service for querying indices and
            constructing per-tenant AssetCertificationService instances.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        poll_interval: Seconds between polling cycles (default 86400).
        cooldown_minutes: Minutes to suppress duplicate runs (default 1440).

    Validates: Requirements 13.2, 13.3, 13.4
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        poll_interval: int = ASSET_CERT_EXPIRY_POLL_INTERVAL_SECONDS,
        cooldown_minutes: int = ASSET_CERT_EXPIRY_COOLDOWN_MINUTES,
    ):
        super().__init__(
            agent_id="asset_cert_expiry_cron_agent",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one daily asset certification expiry cycle.

        Discovers all tenants with asset certifications via a terms
        aggregation, then runs check_expiry_alerts() for each tenant.
        The check_expiry_alerts() method handles both alert generation
        and status transitions (valid → expiring_soon → expired).

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of expiry alerts generated, and *actions* is an empty
            list (status transitions are handled internally by
            check_expiry_alerts).
        """
        detections: List[Any] = []
        actions: List[Any] = []

        # Discover tenants with asset certifications
        tenant_ids = await self._discover_tenants()
        if not tenant_ids:
            self.logger.debug(
                "No tenants with asset certifications — expiry cycle is a no-op"
            )
            return detections, actions

        for tenant_id in tenant_ids:
            try:
                svc = AssetCertificationService(es_service=self._es)

                # Check expiry alerts (Req 13.2, 13.3, 13.4)
                # This also handles status transitions internally
                alerts = await svc.check_expiry_alerts(tenant_id)
                if alerts:
                    detections.extend(alerts)
                    self.logger.info(
                        "Tenant %s: %d asset certification expiry alert(s) generated",
                        tenant_id,
                        len(alerts),
                    )

            except Exception as exc:
                self.logger.error(
                    "Asset cert expiry cycle failed for tenant %s: %s",
                    tenant_id,
                    exc,
                )

        self.logger.info(
            "Asset cert expiry cron complete: %d detection(s) "
            "across %d tenant(s)",
            len(detections),
            len(tenant_ids),
        )
        return detections, actions

    async def _discover_tenants(self) -> List[str]:
        """Discover all tenants with asset certifications via a terms aggregation.

        Returns a list of tenant_id strings. Failures are logged and
        an empty list is returned so the cycle degrades gracefully.
        """
        query: Dict[str, Any] = {
            "size": 0,
            "aggs": {
                "tenants": {
                    "terms": {
                        "field": "tenant_id",
                        "size": _MAX_TENANTS_PER_SWEEP,
                    }
                }
            },
        }

        try:
            response = await self._es.search_documents(
                ASSET_CERTIFICATIONS_INDEX, query, size=0
            )
        except Exception as exc:
            self.logger.error(
                "Asset cert expiry tenant discovery failed: %s", exc
            )
            return []

        buckets = (
            response.get("aggregations", {})
            .get("tenants", {})
            .get("buckets", [])
        )

        tenant_ids: List[str] = [
            bucket["key"] for bucket in buckets if bucket.get("key")
        ]
        return tenant_ids
