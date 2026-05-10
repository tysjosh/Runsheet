"""
Driver Qualification Expiry Cron Agent — daily compliance checks.

Autonomous background agent that runs once daily (every 86400 seconds)
and executes the three driver qualification compliance checks for all
tenants with drivers in the system:

1. ``check_expiry_alerts()`` — generates 60/30/7-day expiry alerts
2. ``auto_suspend_expired_drivers()`` — suspends drivers at ≤7-day threshold
3. ``check_drug_test_overdue()`` — flags drivers with overdue drug tests

Discovers tenants via a ``terms`` aggregation on the ``drivers`` index
(same pattern as ``price_protection_expiry_job``). Exceptions from a
single tenant are logged but do not abort the sweep.

Registered with the ``AgentScheduler`` from ``bootstrap/compliance.py``
(Task 6.10).

Validates: Requirements 5.2, 5.3, 5.4, 5.8
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from compliance.services.compliance_es_mappings import DRIVERS_INDEX
from compliance.services.driver_qualification_service import (
    DriverQualificationService,
)
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Poll interval: 24 hours (daily cron)
DRIVER_EXPIRY_POLL_INTERVAL_SECONDS = 86_400

# Cooldown: 24 hours (one run per day, no duplicates)
DRIVER_EXPIRY_COOLDOWN_MINUTES = 1440

# Maximum distinct tenants per sweep
_MAX_TENANTS_PER_SWEEP = 10_000


class DriverExpiryCronAgent(AutonomousAgentBase):
    """Daily cron agent for driver qualification expiry checks.

    Polls once every 24 hours and, for each tenant with drivers in the
    system, runs:
    1. ``check_expiry_alerts()`` — 60/30/7-day threshold alerts
    2. ``auto_suspend_expired_drivers()`` — auto-suspend at ≤7 days
    3. ``check_drug_test_overdue()`` — flag overdue drug tests

    Args:
        es_service: Elasticsearch service for querying indices and
            constructing per-tenant DriverQualificationService instances.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        poll_interval: Seconds between polling cycles (default 86400).
        cooldown_minutes: Minutes to suppress duplicate runs (default 1440).

    Validates: Requirements 5.2, 5.3, 5.4, 5.8
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        poll_interval: int = DRIVER_EXPIRY_POLL_INTERVAL_SECONDS,
        cooldown_minutes: int = DRIVER_EXPIRY_COOLDOWN_MINUTES,
    ):
        super().__init__(
            agent_id="driver_expiry_cron_agent",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one daily driver qualification expiry cycle.

        Discovers all tenants with drivers via a terms aggregation,
        then runs the three compliance checks for each tenant.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of expiry alerts and overdue flags, and *actions* is a
            list of auto-suspension actions taken.
        """
        detections: List[Any] = []
        actions: List[Any] = []

        # Discover tenants with drivers
        tenant_ids = await self._discover_tenants()
        if not tenant_ids:
            self.logger.debug(
                "No tenants with drivers — expiry cycle is a no-op"
            )
            return detections, actions

        for tenant_id in tenant_ids:
            try:
                svc = DriverQualificationService(es_service=self._es)

                # 1. Check expiry alerts (Req 5.2, 5.3, 5.4)
                alerts = await svc.check_expiry_alerts(tenant_id)
                if alerts:
                    detections.extend(alerts)
                    self.logger.info(
                        "Tenant %s: %d expiry alert(s) generated",
                        tenant_id,
                        len(alerts),
                    )

                # 2. Auto-suspend expired drivers (Req 5.4)
                suspensions = await svc.auto_suspend_expired_drivers(tenant_id)
                if suspensions:
                    actions.extend(suspensions)
                    self.logger.info(
                        "Tenant %s: %d driver(s) auto-suspended",
                        tenant_id,
                        len(suspensions),
                    )

                # 3. Check drug test overdue (Req 5.8)
                overdue = await svc.check_drug_test_overdue(tenant_id)
                if overdue:
                    detections.extend(overdue)
                    self.logger.info(
                        "Tenant %s: %d driver(s) with overdue drug tests",
                        tenant_id,
                        len(overdue),
                    )

            except Exception as exc:
                self.logger.error(
                    "Driver expiry cycle failed for tenant %s: %s",
                    tenant_id,
                    exc,
                )

        self.logger.info(
            "Driver expiry cron complete: %d detection(s), %d action(s) "
            "across %d tenant(s)",
            len(detections),
            len(actions),
            len(tenant_ids),
        )
        return detections, actions

    async def _discover_tenants(self) -> List[str]:
        """Discover all tenants with drivers via a terms aggregation.

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
                DRIVERS_INDEX, query, size=0
            )
        except Exception as exc:
            self.logger.error(
                "Driver expiry tenant discovery failed: %s", exc
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
