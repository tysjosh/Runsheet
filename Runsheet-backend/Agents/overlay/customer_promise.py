"""
Customer Promise — Layer 1 overlay agent for ETA trust management.

Subscribes to SLA and delay RiskSignals, generates communication
proposals for high-confidence (≥0.7) ETA breaches, and manages
recovery notifications when ETA improves.

Cooldown: 30 minutes per delivery (configurable).
Priority: customer_tier × delivery_value × breach_severity.

Validates: Requirements 7.1–7.8
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

# Confidence threshold for generating communication proposals
CONFIDENCE_THRESHOLD = 0.7

# Default cooldown per delivery in minutes
DEFAULT_DELIVERY_COOLDOWN_MINUTES = 30


class CustomerPromise(OverlayAgentBase):
    """Proactive ETA trust management and customer communication agent.

    Detects high-confidence ETA breach risks from SLA and delay signals,
    generates communication proposals with appropriate channel selection,
    and sends recovery notifications when conditions improve.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager.
        confirmation_protocol: For routing communication proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        delivery_cooldown_minutes: Cooldown per delivery (default 30).
        poll_interval: Decision cycle interval in seconds (default 45).
    """

    def __init__(
        self,
        signal_bus: SignalBus,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        autonomy_config_service,
        feature_flag_service,
        delivery_cooldown_minutes: int = DEFAULT_DELIVERY_COOLDOWN_MINUTES,
        poll_interval: int = 45,
    ):
        super().__init__(
            agent_id="customer_promise",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": RiskSignal,
                    "filters": {
                        "source_agent": [
                            "sla_guardian_agent",
                            "delay_response_agent",
                        ]
                    },
                },
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=delivery_cooldown_minutes,
        )
        # Track previously flagged deliveries for recovery detection (Req 7.8)
        self._flagged_deliveries: Dict[str, Dict[str, Any]] = {}

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Evaluate ETA breach risks and generate communication proposals.

        Steps:
        1. Filter signals with confidence >= 0.7 (Req 7.2).
        2. Separate breach signals from recovery candidates (Req 7.8).
        3. For breach signals: check per-delivery cooldown, compute
           priority, select channel, generate communication proposal,
           track as flagged (Req 7.3, 7.4, 7.5, 7.7).
        4. For recovery candidates: generate recovery notification,
           remove from flagged (Req 7.8).
        5. Cleanup flagged deliveries older than 24 hours.

        Returns:
            List of InterventionProposals for customer communications.
        """
        if not signals:
            return []

        tenant_id = signals[0].tenant_id
        proposals = []

        # Separate high-confidence breach signals from recovery candidates
        breach_signals = []
        recovery_candidates = []

        for signal in signals:
            if signal.confidence < CONFIDENCE_THRESHOLD:
                continue

            delivery_id = signal.entity_id

            if (
                signal.severity == Severity.LOW
                and delivery_id in self._flagged_deliveries
            ):
                # Previously flagged delivery now showing low severity = recovery
                recovery_candidates.append(signal)
            else:
                breach_signals.append(signal)

        # Generate breach communication proposals (Req 7.2, 7.3)
        for signal in breach_signals:
            delivery_id = signal.entity_id

            # Cooldown check (Req 7.4)
            if self._is_on_cooldown(delivery_id):
                continue

            # Compute priority (Req 7.7)
            priority_score = self._compute_priority(signal)

            # Select communication channel based on severity
            channel = self._select_channel(signal.severity)

            proposal = InterventionProposal(
                source_agent=self.agent_id,
                actions=[
                    {
                        "tool_name": "send_customer_notification",
                        "parameters": {
                            "delivery_id": delivery_id,
                            "notification_type": "eta_breach_warning",
                            "channel": channel,
                            "message_template": "eta_delay_notification",
                            "context": signal.context,
                        },
                        "description": (
                            f"Notify customer of ETA breach risk "
                            f"for delivery {delivery_id}"
                        ),
                    }
                ],
                expected_kpi_delta={
                    "customer_satisfaction_score": 0.1,
                    "proactive_notification_count": 1,
                },
                risk_class=RiskClass.MEDIUM,
                confidence=signal.confidence,
                priority=priority_score,
                tenant_id=tenant_id,
            )
            proposals.append(proposal)
            self._set_cooldown(delivery_id)

            # Track as flagged for recovery detection (Req 7.8)
            self._flagged_deliveries[delivery_id] = {
                "flagged_at": datetime.now(timezone.utc),
                "original_severity": signal.severity.value,
            }

        # Generate recovery notifications (Req 7.8)
        for signal in recovery_candidates:
            delivery_id = signal.entity_id

            if self._is_on_cooldown(delivery_id):
                continue

            recovery_proposal = InterventionProposal(
                source_agent=self.agent_id,
                actions=[
                    {
                        "tool_name": "send_customer_notification",
                        "parameters": {
                            "delivery_id": delivery_id,
                            "notification_type": "eta_recovery",
                            "channel": "push",
                            "message_template": "eta_recovery_notification",
                            "context": signal.context,
                        },
                        "description": (
                            f"Notify customer of ETA recovery "
                            f"for delivery {delivery_id}"
                        ),
                    }
                ],
                expected_kpi_delta={
                    "customer_satisfaction_score": 0.05,
                },
                risk_class=RiskClass.LOW,
                confidence=signal.confidence,
                priority=1,
                tenant_id=tenant_id,
            )
            proposals.append(recovery_proposal)
            self._set_cooldown(delivery_id)
            # Remove from flagged
            self._flagged_deliveries.pop(delivery_id, None)

        # Cleanup old flagged deliveries (older than 24 hours)
        self._cleanup_flagged()

        return proposals

    # ------------------------------------------------------------------
    # Priority and channel selection
    # ------------------------------------------------------------------

    def _compute_priority(self, signal: RiskSignal) -> int:
        """Compute priority score: customer_tier_weight × delivery_value × severity_weight.

        Severity weights: low=1, medium=2, high=3, critical=4.

        Validates: Requirement 7.7
        """
        severity_weight = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        tier_weight = signal.context.get("customer_tier_weight", 1)
        delivery_value = signal.context.get("delivery_value", 1.0)
        sev = severity_weight.get(signal.severity.value, 2)
        return int(tier_weight * delivery_value * sev)

    @staticmethod
    def _select_channel(severity: Severity) -> str:
        """Select communication channel based on breach severity.

        - SMS for critical/high
        - email for medium
        - push for low
        """
        if severity in (Severity.CRITICAL, Severity.HIGH):
            return "sms"
        elif severity == Severity.MEDIUM:
            return "email"
        return "push"

    def _cleanup_flagged(self) -> None:
        """Remove flagged deliveries older than 24 hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        to_remove = [
            did
            for did, info in self._flagged_deliveries.items()
            if info["flagged_at"] < cutoff
        ]
        for did in to_remove:
            del self._flagged_deliveries[did]
