"""
Exception Commander — Layer 1 overlay agent for incident triage.

Subscribes to all Layer 0 RiskSignals, correlates signals within a
30-second window into incidents, produces ranked response plans, and
broadcasts incident summaries via AgentActivityWSManager.

Incident state machine: detected → triaging → plan_proposed →
executing → resolved → escalated.

Escalation timeout: 5 minutes (configurable).

Validates: Requirements 5.1–5.8
"""
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
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


class IncidentState(str, Enum):
    DETECTED = "detected"
    TRIAGING = "triaging"
    PLAN_PROPOSED = "plan_proposed"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class Incident:
    """Represents a correlated incident from multiple RiskSignals."""

    def __init__(self, incident_id: str, tenant_id: str):
        self.incident_id = incident_id
        self.tenant_id = tenant_id
        self.state = IncidentState.DETECTED
        self.signals: List[RiskSignal] = []
        self.affected_entities: set = set()
        self.severity = Severity.LOW
        self.created_at = datetime.now(timezone.utc)
        self.state_changed_at = datetime.now(timezone.utc)
        self.proposal: Optional[InterventionProposal] = None

    def add_signal(self, signal: RiskSignal) -> None:
        """Add a correlated signal and update severity."""
        self.signals.append(signal)
        self.affected_entities.add(signal.entity_id)
        # Incident severity = max severity of constituent signals
        severity_order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        if severity_order.index(signal.severity) > severity_order.index(self.severity):
            self.severity = signal.severity

    def transition(self, new_state: IncidentState) -> None:
        """Transition the incident to a new state."""
        self.state = new_state
        self.state_changed_at = datetime.now(timezone.utc)


class ExceptionCommander(OverlayAgentBase):
    """Incident triage and ranked response plan generator.

    Correlates RiskSignals from all Layer 0 agents within a configurable
    window into incidents, produces ranked response plans, and manages
    incident lifecycle with escalation timeouts.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for incident broadcasts.
        confirmation_protocol: For routing approved actions.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        correlation_window_seconds: Window for correlating signals (default 30).
        escalation_timeout_seconds: Timeout before escalation (default 300).
        poll_interval: Decision cycle interval in seconds (default 30).
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
        correlation_window_seconds: int = 30,
        escalation_timeout_seconds: int = 300,
        poll_interval: int = 30,
    ):
        super().__init__(
            agent_id="exception_commander",
            signal_bus=signal_bus,
            subscriptions=[
                {"message_type": RiskSignal},  # All Layer 0 signals
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=2,
        )
        self._correlation_window = timedelta(seconds=correlation_window_seconds)
        self._escalation_timeout = timedelta(seconds=escalation_timeout_seconds)
        self._active_incidents: Dict[str, Incident] = {}
        self._incident_counter = 0

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Correlate signals into incidents and produce response plans.

        Steps:
        1. Correlate incoming signals into existing or new incidents
           based on entity overlap within the correlation window.
        2. For each incident in DETECTED state, transition to TRIAGING
           and generate a ranked response plan.
        3. Check for escalation timeouts on PLAN_PROPOSED incidents.
        4. Broadcast incident summaries via WebSocket.

        Returns:
            List of InterventionProposals (one per new/updated incident).
        """
        tenant_id = signals[0].tenant_id if signals else "default"
        proposals = []

        # Step 1: Correlate signals into incidents (Req 5.2)
        for signal in signals:
            incident = self._find_or_create_incident(signal)
            incident.add_signal(signal)

        # Step 2: Triage new incidents and generate response plans
        for incident in self._active_incidents.values():
            if incident.tenant_id != tenant_id:
                continue

            if incident.state == IncidentState.DETECTED:
                incident.transition(IncidentState.TRIAGING)
                proposal = self._generate_response_plan(incident)
                incident.proposal = proposal
                incident.transition(IncidentState.PLAN_PROPOSED)
                proposals.append(proposal)

                # Broadcast incident summary (Req 5.5)
                await self._broadcast_incident(incident)

        # Step 3: Check escalation timeouts (Req 5.8)
        await self._check_escalations()

        # Cleanup resolved/escalated incidents older than 1 hour
        self._cleanup_old_incidents()

        return proposals

    # ------------------------------------------------------------------
    # Correlation
    # ------------------------------------------------------------------

    def _find_or_create_incident(self, signal: RiskSignal) -> Incident:
        """Find an existing incident matching the signal or create a new one.

        Matches by entity overlap within the correlation window.
        """
        now = datetime.now(timezone.utc)
        for incident in self._active_incidents.values():
            if incident.tenant_id != signal.tenant_id:
                continue
            if incident.state in (IncidentState.RESOLVED, IncidentState.ESCALATED):
                continue
            # Check time window
            if (now - incident.created_at) > self._correlation_window * 10:
                continue
            # Check entity overlap
            if signal.entity_id in incident.affected_entities:
                return incident
            # Check if any signal arrived within correlation window
            for existing_signal in incident.signals:
                if abs((signal.timestamp - existing_signal.timestamp).total_seconds()) <= self._correlation_window.total_seconds():
                    return incident

        # Create new incident
        self._incident_counter += 1
        incident_id = f"INC-{self._incident_counter:06d}"
        incident = Incident(incident_id=incident_id, tenant_id=signal.tenant_id)
        self._active_incidents[incident_id] = incident
        return incident

    # ------------------------------------------------------------------
    # Response plan generation
    # ------------------------------------------------------------------

    def _generate_response_plan(self, incident: Incident) -> InterventionProposal:
        """Generate a ranked response plan for an incident (Req 5.3, 5.4)."""
        actions = []
        severity_to_risk = {
            Severity.LOW: RiskClass.LOW,
            Severity.MEDIUM: RiskClass.MEDIUM,
            Severity.HIGH: RiskClass.HIGH,
            Severity.CRITICAL: RiskClass.HIGH,
        }

        # Build playbook steps based on signal sources
        source_agents = {s.source_agent for s in incident.signals}

        if "delay_response_agent" in source_agents:
            actions.append({
                "tool_name": "reassign_delayed_jobs",
                "parameters": {
                    "entity_ids": list(incident.affected_entities),
                    "incident_id": incident.incident_id,
                },
                "priority": 1,
                "description": "Reassign delayed jobs to available assets",
                "expected_impact": "Reduce delivery delays",
            })

        if "fuel_management_agent" in source_agents:
            actions.append({
                "tool_name": "emergency_fuel_dispatch",
                "parameters": {
                    "entity_ids": list(incident.affected_entities),
                    "incident_id": incident.incident_id,
                },
                "priority": 2,
                "description": "Dispatch emergency fuel to critical stations",
                "expected_impact": "Prevent fuel stockout",
            })

        if "sla_guardian_agent" in source_agents:
            actions.append({
                "tool_name": "escalate_sla_breach",
                "parameters": {
                    "entity_ids": list(incident.affected_entities),
                    "incident_id": incident.incident_id,
                    "priority": "critical",
                },
                "priority": 3,
                "description": "Escalate SLA breaches to operations lead",
                "expected_impact": "Prevent SLA penalty accumulation",
            })

        avg_confidence = (
            sum(s.confidence for s in incident.signals) / len(incident.signals)
            if incident.signals else 0.5
        )

        return InterventionProposal(
            source_agent=self.agent_id,
            actions=actions,
            expected_kpi_delta={
                "incident_resolution_time_minutes": -15.0,
                "affected_entities_count": len(incident.affected_entities),
            },
            risk_class=severity_to_risk.get(incident.severity, RiskClass.MEDIUM),
            confidence=avg_confidence,
            priority=len(incident.affected_entities),
            tenant_id=incident.tenant_id,
        )

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    async def _check_escalations(self) -> None:
        """Escalate incidents stuck in PLAN_PROPOSED beyond timeout (Req 5.8)."""
        now = datetime.now(timezone.utc)
        for incident in self._active_incidents.values():
            if incident.state != IncidentState.PLAN_PROPOSED:
                continue
            if (now - incident.state_changed_at) > self._escalation_timeout:
                incident.transition(IncidentState.ESCALATED)
                # Increase severity on escalation
                if incident.severity != Severity.CRITICAL:
                    severity_order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
                    idx = severity_order.index(incident.severity)
                    incident.severity = severity_order[min(idx + 1, 3)]
                await self._broadcast_incident(incident, escalated=True)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def _broadcast_incident(
        self, incident: Incident, escalated: bool = False
    ) -> None:
        """Broadcast incident summary via WebSocket (Req 5.5)."""
        event_type = "incident_escalated" if escalated else "incident_detected"
        await self._ws.broadcast_event(event_type, {
            "incident_id": incident.incident_id,
            "state": incident.state.value,
            "severity": incident.severity.value,
            "affected_entities": list(incident.affected_entities),
            "signal_count": len(incident.signals),
            "source_agents": list({s.source_agent for s in incident.signals}),
            "created_at": incident.created_at.isoformat(),
            "tenant_id": incident.tenant_id,
        })

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_old_incidents(self) -> None:
        """Remove stale incidents to prevent unbounded memory growth.

        Removes:
        - Resolved/escalated incidents older than 1 hour
        - Any incident (regardless of state) older than 6 hours
        - Caps total active incidents at 500 (removes oldest first)
        """
        now = datetime.now(timezone.utc)
        terminal_cutoff = now - timedelta(hours=1)
        stale_cutoff = now - timedelta(hours=6)

        to_remove = [
            iid for iid, inc in self._active_incidents.items()
            if (
                # Terminal states older than 1 hour
                (inc.state in (IncidentState.RESOLVED, IncidentState.ESCALATED)
                 and inc.state_changed_at < terminal_cutoff)
                # Any state older than 6 hours (stuck incidents)
                or inc.created_at < stale_cutoff
            )
        ]
        for iid in to_remove:
            del self._active_incidents[iid]

        # Cap total size to prevent unbounded growth
        max_incidents = 500
        if len(self._active_incidents) > max_incidents:
            sorted_incidents = sorted(
                self._active_incidents.items(),
                key=lambda x: x[1].created_at,
            )
            excess = len(sorted_incidents) - max_incidents
            for iid, _ in sorted_incidents[:excess]:
                del self._active_incidents[iid]
