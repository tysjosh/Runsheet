"""
Overlay Agent Base Class.

Extends AutonomousAgentBase with signal subscription, decision cycle
scheduling, shadow/active mode toggling, and proposal routing.

Validates: Requirements 3.1–3.8
"""
import asyncio
import logging
import time
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    OutcomeRecord,
    PolicyChangeProposal,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

SHADOW_PROPOSALS_INDEX = "agent_shadow_proposals"


class OverlayAgentBase(AutonomousAgentBase):
    """Base class for Layer 1 and Layer 2 overlay agents.

    Extends AutonomousAgentBase with:
    - Signal Bus subscription and buffering
    - Shadow/active mode with per-tenant granularity
    - Decision cycle that collects signals, evaluates, and routes proposals
    - Per-cycle metrics tracking

    Args:
        agent_id: Unique identifier for this overlay agent.
        signal_bus: The SignalBus instance for pub/sub.
        subscriptions: List of dicts specifying signal subscriptions.
            Each dict has 'message_type' and optional 'filters'.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutations.
        autonomy_config_service: Service for mode management.
        feature_flag_service: Service for per-tenant feature flags.
        es_service: Elasticsearch service for shadow proposal logging.
        poll_interval: Seconds between decision cycles (default 60).
        cooldown_minutes: Minutes for per-entity cooldown (default 15).
    """

    def __init__(
        self,
        agent_id: str,
        signal_bus: SignalBus,
        subscriptions: List[Dict[str, Any]],
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        autonomy_config_service,
        feature_flag_service,
        es_service,
        poll_interval: int = 60,
        cooldown_minutes: int = 15,
    ):
        super().__init__(
            agent_id=agent_id,
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._signal_bus = signal_bus
        self._subscription_specs = subscriptions
        self._autonomy_config = autonomy_config_service
        self._es = es_service
        self._signal_buffer: List[Any] = []
        self._buffer_lock = asyncio.Lock()

        # Per-cycle metrics
        self._cycle_metrics: Dict[str, Any] = {
            "signals_consumed": 0,
            "proposals_generated": 0,
            "cycle_duration_ms": 0.0,
            "mode": "shadow",
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the overlay agent: register subscriptions, then start loop."""
        for spec in self._subscription_specs:
            await self._signal_bus.subscribe(
                subscriber_id=self.agent_id,
                message_type=spec["message_type"],
                callback=self._on_signal,
                filters=spec.get("filters"),
            )
        await super().start()

    async def stop(self) -> None:
        """Stop the overlay agent: unsubscribe and stop loop."""
        await self._signal_bus.unsubscribe(self.agent_id)
        await super().stop()

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    async def _on_signal(self, signal) -> None:
        """Buffer incoming signals for the next decision cycle."""
        async with self._buffer_lock:
            self._signal_buffer.append(signal)

    # ------------------------------------------------------------------
    # Decision cycle (replaces monitor_cycle)
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one decision cycle: collect, evaluate, route.

        Collects buffered signals, groups them by tenant, checks the
        mode for each tenant via ``_get_mode()``, invokes the subclass
        ``evaluate()`` method, and routes resulting proposals based on
        mode (shadow → log, active → ConfirmationProtocol).

        Returns:
            A ``(signals, proposals)`` tuple for activity logging.
        """
        cycle_start = time.monotonic()

        # Collect buffered signals
        async with self._buffer_lock:
            signals = list(self._signal_buffer)
            self._signal_buffer.clear()

        if not signals:
            return [], []

        # Process per-tenant
        proposals_generated: List[Any] = []
        for tenant_id, tenant_signals in self._group_by_tenant(signals).items():
            mode = await self._get_mode(tenant_id)
            self._cycle_metrics["mode"] = mode

            if mode == "disabled":
                continue

            # Evaluate — subclass decision logic
            proposals = await self.evaluate(tenant_signals)

            for proposal in proposals:
                if mode == "shadow":
                    await self._log_shadow_proposal(proposal)
                else:
                    await self._route_proposal(proposal, mode)
                proposals_generated.append(proposal)

        cycle_duration_ms = (time.monotonic() - cycle_start) * 1000
        self._cycle_metrics.update({
            "signals_consumed": len(signals),
            "proposals_generated": len(proposals_generated),
            "cycle_duration_ms": cycle_duration_ms,
        })

        return signals, proposals_generated

    # ------------------------------------------------------------------
    # Mode management
    # ------------------------------------------------------------------

    async def _get_mode(self, tenant_id: str) -> str:
        """Get the overlay agent's mode for a tenant.

        Checks the feature flag service for the overlay-specific flag
        ``overlay.{agent_id}``. Returns one of: ``'disabled'``,
        ``'shadow'``, ``'active_gated'``, or ``'active_auto'``.

        Defaults to ``'shadow'`` when the feature flag service is
        unavailable or the flag is not set.
        """
        if not self._feature_flags:
            return "shadow"
        try:
            flag_key = f"overlay.{self.agent_id}"
            state = await self._feature_flags.get_overlay_state(
                flag_key, tenant_id
            )
            return state or "shadow"
        except AttributeError:
            # Fallback: basic is_enabled check when get_overlay_state
            # is not yet available on the FeatureFlagService.
            try:
                enabled = await self._feature_flags.is_enabled(tenant_id)
                return "shadow" if enabled else "disabled"
            except Exception:
                return "shadow"

    # ------------------------------------------------------------------
    # Proposal routing
    # ------------------------------------------------------------------

    async def _log_shadow_proposal(self, proposal) -> None:
        """Persist a proposal to the shadow proposals ES index.

        In shadow mode, proposals are logged for retrospective analysis
        but never submitted to the ConfirmationProtocol.
        """
        try:
            doc = proposal.model_dump(mode="json")
            doc["shadow_agent"] = self.agent_id
            doc["shadow_timestamp"] = datetime.now(timezone.utc).isoformat()
            await self._es.index_document(
                SHADOW_PROPOSALS_INDEX,
                getattr(proposal, "proposal_id", None),
                doc,
            )
        except Exception as e:
            self.logger.error(
                "Failed to log shadow proposal: %s", e, exc_info=True
            )

    async def _route_proposal(self, proposal, mode: str) -> None:
        """Route a proposal through ConfirmationProtocol and publish to SignalBus.

        For ``InterventionProposal`` instances, creates a ``MutationRequest``
        for each action and submits through the confirmation protocol.
        All proposals are also published to the Signal Bus for downstream
        consumers (e.g. OutcomeTracker, LearningPolicyAgent).
        """
        if isinstance(proposal, InterventionProposal):
            from Agents.confirmation_protocol import MutationRequest

            for action in proposal.actions:
                request = MutationRequest(
                    tool_name=action.get("tool_name", "overlay_action"),
                    parameters=action.get("parameters", {}),
                    tenant_id=proposal.tenant_id,
                    agent_id=self.agent_id,
                )
                await self._confirmation_protocol.process_mutation(request)

        # Publish proposal to Signal Bus for downstream consumers
        await self._signal_bus.publish(proposal)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _group_by_tenant(self, signals) -> Dict[str, List]:
        """Group signals by tenant_id.

        Signals without a ``tenant_id`` attribute are grouped under
        the key ``'default'``.
        """
        groups: Dict[str, List] = {}
        for sig in signals:
            tid = getattr(sig, "tenant_id", "default")
            groups.setdefault(tid, []).append(sig)
        return groups

    @property
    def cycle_metrics(self) -> Dict[str, Any]:
        """Return a snapshot of the most recent cycle metrics."""
        return dict(self._cycle_metrics)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Domain-specific decision logic. Subclasses must implement.

        Called once per tenant per decision cycle with the buffered
        signals for that tenant.

        Args:
            signals: Buffered signals for a single tenant.

        Returns:
            List of InterventionProposals (or PolicyChangeProposals).
        """
        ...  # pragma: no cover
