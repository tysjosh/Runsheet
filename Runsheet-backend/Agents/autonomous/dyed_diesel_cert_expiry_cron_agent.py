"""
Dyed Diesel Certificate Expiry Cron Agent — daily IRS 637M compliance sweep.

Autonomous background agent that runs once daily (every 86400 seconds)
and calls ``DyedDieselEnforcer.check_expiring_certificates()`` for every
tenant with tax-exemption certificates on file, so customers with an
IRS 637M dyed-diesel exemption approaching expiry get proactive advance
warning before ``validate_order()`` starts blocking their orders.

Discovers tenants via a ``terms`` aggregation on the ``tax_exemptions``
index (same pattern as ``driver_expiry_cron_agent.py`` /
``price_protection_expiry_job.py``). Exceptions from a single tenant are
logged but do not abort the sweep.

Registered with the ``AgentScheduler`` from ``bootstrap/compliance.py``.

Validates: Requirement 6.6
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from compliance.services.compliance_es_mappings import TAX_EXEMPTIONS_INDEX
from compliance.services.dyed_diesel_enforcer import DyedDieselEnforcer
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Poll interval: 24 hours (daily cron)
DYED_DIESEL_CERT_EXPIRY_POLL_INTERVAL_SECONDS = 86_400

# Cooldown: 24 hours (one run per day, no duplicates)
DYED_DIESEL_CERT_EXPIRY_COOLDOWN_MINUTES = 1440

# Advance-warning window, matching check_expiring_certificates' default.
DEFAULT_DAYS_AHEAD = 30

# Maximum distinct tenants per sweep
_MAX_TENANTS_PER_SWEEP = 10_000


class DyedDieselCertExpiryCronAgent(AutonomousAgentBase):
    """Daily cron agent for IRS 637M dyed-diesel certificate expiry checks.

    Polls once every 24 hours and, for each tenant with tax-exemption
    certificates on file, runs
    ``DyedDieselEnforcer.check_expiring_certificates()`` to surface
    certificates expiring within :data:`DEFAULT_DAYS_AHEAD` days.

    Args:
        es_service: Elasticsearch service for tenant discovery and for
            constructing the ``DyedDieselEnforcer`` instance.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        signal_bus: Optional SignalBus passed through to the
            DyedDieselEnforcer so sales-team notification hooks stay
            wired the same way as the intake-hook path.
        days_ahead: Advance-warning window in days (default 30).
        poll_interval: Seconds between polling cycles (default 86400).
        cooldown_minutes: Minutes to suppress duplicate runs (default 1440).

    Validates: Requirement 6.6
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        signal_bus=None,
        days_ahead: int = DEFAULT_DAYS_AHEAD,
        poll_interval: int = DYED_DIESEL_CERT_EXPIRY_POLL_INTERVAL_SECONDS,
        cooldown_minutes: int = DYED_DIESEL_CERT_EXPIRY_COOLDOWN_MINUTES,
    ):
        super().__init__(
            agent_id="dyed_diesel_cert_expiry_cron_agent",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service
        self._signal_bus = signal_bus
        self._days_ahead = days_ahead

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one daily dyed-diesel certificate expiry cycle.

        Discovers all tenants with tax-exemption certificates via a
        terms aggregation, then runs the expiry check for each tenant.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is the
            list of expiring certificate documents found across every
            tenant. This sweep is read-only (advance warning only), so
            *actions* is always empty.
        """
        detections: List[Any] = []
        actions: List[Any] = []

        tenant_ids = await self._discover_tenants()
        if not tenant_ids:
            self.logger.debug(
                "No tenants with tax exemptions — dyed-diesel cert "
                "expiry cycle is a no-op"
            )
            return detections, actions

        enforcer = DyedDieselEnforcer(
            es_service=self._es, signal_bus=self._signal_bus
        )

        for tenant_id in tenant_ids:
            try:
                expiring = await enforcer.check_expiring_certificates(
                    tenant_id, days_ahead=self._days_ahead
                )
                if expiring:
                    detections.extend(expiring)
                    self._note_tenant_activity(tenant_id, detections=len(expiring))
                    self.logger.info(
                        "Tenant %s: %d dyed-diesel certificate(s) expiring "
                        "within %d days",
                        tenant_id,
                        len(expiring),
                        self._days_ahead,
                    )
            except Exception as exc:
                self.logger.error(
                    "Dyed-diesel certificate expiry check failed for "
                    "tenant %s: %s",
                    tenant_id,
                    exc,
                )

        self.logger.info(
            "Dyed-diesel certificate expiry cron complete: %d "
            "detection(s) across %d tenant(s)",
            len(detections),
            len(tenant_ids),
        )
        return detections, actions

    async def _discover_tenants(self) -> List[str]:
        """Discover all tenants with tax-exemption certificates.

        Returns a list of tenant_id strings via a terms aggregation.
        Failures are logged and an empty list is returned so the cycle
        degrades gracefully.
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
                TAX_EXEMPTIONS_INDEX, query, size=0
            )
        except Exception as exc:
            self.logger.error(
                "Dyed-diesel cert expiry tenant discovery failed: %s", exc
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
