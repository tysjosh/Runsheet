"""
Meter Calibration Cron Agent — daily meter calibration expiry checks.

Autonomous background agent that runs once daily (every 86400 seconds)
and executes the meter calibration alert check for all tenants with
meters in the system:

1. ``check_calibration_alerts()`` — generates alerts for meters whose
   calibration_expiry_date is within 30 days (warning) or 7 days (critical)

Discovers tenants via a ``terms`` aggregation on the
``meter_registry`` index (same pattern as
``asset_cert_expiry_cron_agent``). Exceptions from a single tenant are
logged but do not abort the sweep.

Registered with the ``AgentScheduler`` from ``bootstrap/compliance.py``
(Task 10.10).

Validates: Requirement 8.4
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from compliance.services.compliance_es_mappings import METER_REGISTRY_INDEX
from compliance.services.meter_audit_service import MeterAuditService
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Poll interval: 24 hours (daily cron)
METER_CALIBRATION_POLL_INTERVAL_SECONDS = 86_400

# Cooldown: 24 hours (one run per day, no duplicates)
METER_CALIBRATION_COOLDOWN_MINUTES = 1440

# Maximum distinct tenants per sweep
_MAX_TENANTS_PER_SWEEP = 10_000


class MeterCalibrationCronAgent(AutonomousAgentBase):
    """Daily cron agent for meter calibration expiry checks.

    Polls once every 24 hours and, for each tenant with meters in the
    system, runs:
    1. ``check_calibration_alerts()`` — generates alerts for meters
       expiring within 30 days (warning) or 7 days (critical)

    Args:
        es_service: Elasticsearch service for querying indices and
            constructing per-tenant MeterAuditService instances.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        poll_interval: Seconds between polling cycles (default 86400).
        cooldown_minutes: Minutes to suppress duplicate runs (default 1440).

    Validates: Requirement 8.4
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        poll_interval: int = METER_CALIBRATION_POLL_INTERVAL_SECONDS,
        cooldown_minutes: int = METER_CALIBRATION_COOLDOWN_MINUTES,
    ):
        super().__init__(
            agent_id="meter_calibration_cron_agent",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one daily meter calibration expiry cycle.

        Discovers all tenants with meters via a terms aggregation,
        then runs check_calibration_alerts() for each tenant. The
        check_calibration_alerts() method generates alerts for meters
        whose calibration_expiry_date is within 30 days.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of calibration alerts generated, and *actions* is an
            empty list (alerts are informational).
        """
        detections: List[Any] = []
        actions: List[Any] = []

        # Discover tenants with meters
        tenant_ids = await self._discover_tenants()
        if not tenant_ids:
            self.logger.debug(
                "No tenants with meters — calibration cycle is a no-op"
            )
            return detections, actions

        for tenant_id in tenant_ids:
            try:
                svc = MeterAuditService(es_service=self._es)

                # Check calibration alerts (Req 8.4)
                alerts = await svc.check_calibration_alerts(tenant_id)
                if alerts:
                    detections.extend(alerts)
                    self.logger.info(
                        "Tenant %s: %d meter calibration alert(s) generated",
                        tenant_id,
                        len(alerts),
                    )

            except Exception as exc:
                self.logger.error(
                    "Meter calibration cycle failed for tenant %s: %s",
                    tenant_id,
                    exc,
                )

        self.logger.info(
            "Meter calibration cron complete: %d detection(s) "
            "across %d tenant(s)",
            len(detections),
            len(tenant_ids),
        )
        return detections, actions

    async def _discover_tenants(self) -> List[str]:
        """Discover all tenants with meters via a terms aggregation.

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
                METER_REGISTRY_INDEX, query, size=0
            )
        except Exception as exc:
            self.logger.error(
                "Meter calibration tenant discovery failed: %s", exc
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
