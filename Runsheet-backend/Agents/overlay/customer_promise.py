"""
Customer Promise — Layer 1 overlay agent for ETA trust management.

Subscribes to SLA and delay RiskSignals, generates communication
proposals for high-confidence (≥0.7) ETA breaches, and manages
recovery notifications when ETA improves.

Cooldown: 30 minutes per delivery (configurable).
Priority: customer_tier × delivery_value × breach_severity.

SLA-tier personalization (Req 16.1–16.3):
- Premium/enterprise tiers get SMS/WhatsApp regardless of severity.
- Standard tier uses severity-based channel selection.
- Recovery notifications include tier-appropriate apology messages.

Validates: Requirements 7.1–7.8, 16.1–16.3
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
        3. For breach signals: look up SLA tier, check per-delivery cooldown,
           compute priority, select channel (with tier override), select
           template, generate communication proposal, track as flagged
           (Req 7.3, 7.4, 7.5, 7.7, 16.1, 16.3).
        4. For recovery candidates: generate recovery notification with
           tier-appropriate apology and updated ETA, remove from flagged
           (Req 7.8, 16.2).
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

        # Generate breach communication proposals (Req 7.2, 7.3, 16.1, 16.3)
        for signal in breach_signals:
            delivery_id = signal.entity_id

            # Cooldown check (Req 7.4)
            if self._is_on_cooldown(delivery_id):
                continue

            # Look up SLA tier (Req 16.1)
            sla_tier = self._get_sla_tier(signal)

            # Compute priority (Req 7.7)
            priority_score = self._compute_priority(signal)

            # Select channel with SLA-tier override (Req 16.3)
            channel = self._select_channel_for_tier(signal.severity, sla_tier)

            # Select template based on SLA tier (Req 16.1)
            template = self._select_template_for_tier(sla_tier)

            proposal = InterventionProposal(
                source_agent=self.agent_id,
                actions=[
                    {
                        "tool_name": "send_customer_notification",
                        "parameters": {
                            "delivery_id": delivery_id,
                            "notification_type": "eta_breach_warning",
                            "channel": channel,
                            "message_template": template,
                            "sla_tier": sla_tier,
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
                "sla_tier": sla_tier,
            }

        # Generate recovery notifications (Req 7.8, 16.2)
        for signal in recovery_candidates:
            delivery_id = signal.entity_id

            if self._is_on_cooldown(delivery_id):
                continue

            # Retrieve SLA tier from flagged data or signal context (Req 16.2)
            flagged_info = self._flagged_deliveries.get(delivery_id, {})
            sla_tier = flagged_info.get(
                "sla_tier", self._get_sla_tier(signal)
            )

            # Select channel with SLA-tier override for recovery too (Req 16.3)
            channel = self._select_channel_for_tier(signal.severity, sla_tier)

            # Select recovery template based on SLA tier (Req 16.2)
            recovery_template = self._select_recovery_template_for_tier(sla_tier)

            # Build tier-appropriate apology message (Req 16.2)
            apology_message = self._get_recovery_apology(sla_tier)

            # Include updated ETA from signal context (Req 16.2)
            updated_eta = signal.context.get("updated_eta")

            recovery_proposal = InterventionProposal(
                source_agent=self.agent_id,
                actions=[
                    {
                        "tool_name": "send_customer_notification",
                        "parameters": {
                            "delivery_id": delivery_id,
                            "notification_type": "eta_recovery",
                            "channel": channel,
                            "message_template": recovery_template,
                            "sla_tier": sla_tier,
                            "apology_message": apology_message,
                            "updated_eta": updated_eta,
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

    # ------------------------------------------------------------------
    # SLA-tier personalization (Req 16.1, 16.2, 16.3)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_sla_tier(signal: RiskSignal) -> str:
        """Look up customer SLA tier from signal context.

        Returns the ``sla_tier`` value from the signal's context dict,
        defaulting to ``'standard'`` when not present.

        Validates: Requirement 16.1
        """
        return signal.context.get("sla_tier", "standard")

    @staticmethod
    def _select_channel_for_tier(severity: Severity, sla_tier: str) -> str:
        """Select communication channel with SLA-tier override.

        For ``premium`` and ``enterprise`` tiers, always use SMS or
        WhatsApp regardless of severity (Req 16.3):
        - ``enterprise`` → ``whatsapp``
        - ``premium`` → ``sms``

        For ``standard`` tier, fall back to severity-based selection:
        - critical/high → ``sms``
        - medium → ``email``
        - low → ``push``

        Validates: Requirement 16.3
        """
        if sla_tier == "enterprise":
            return "whatsapp"
        if sla_tier == "premium":
            return "sms"
        # Standard tier: severity-based selection
        return CustomerPromise._select_channel(severity)

    @staticmethod
    def _select_template_for_tier(sla_tier: str) -> str:
        """Select notification template based on SLA tier.

        Validates: Requirement 16.1
        """
        templates = {
            "enterprise": "eta_delay_enterprise",
            "premium": "eta_delay_premium",
        }
        return templates.get(sla_tier, "eta_delay_notification")

    @staticmethod
    def _select_recovery_template_for_tier(sla_tier: str) -> str:
        """Select recovery notification template based on SLA tier.

        Validates: Requirement 16.2
        """
        templates = {
            "enterprise": "eta_recovery_enterprise",
            "premium": "eta_recovery_premium",
        }
        return templates.get(sla_tier, "eta_recovery_notification")

    @staticmethod
    def _get_recovery_apology(sla_tier: str) -> str:
        """Return a tier-appropriate apology message for recovery notifications.

        Validates: Requirement 16.2
        """
        apologies = {
            "enterprise": (
                "We sincerely apologize for the disruption to your delivery. "
                "As a valued enterprise partner, your dedicated account team "
                "has been notified and is monitoring your delivery."
            ),
            "premium": (
                "We apologize for the earlier delay. As a premium customer, "
                "your delivery has been prioritized and is now back on track."
            ),
        }
        return apologies.get(
            sla_tier,
            "We apologize for the earlier delay. "
            "Your delivery is now back on schedule.",
        )

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
