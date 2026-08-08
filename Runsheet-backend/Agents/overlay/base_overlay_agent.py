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


# ---------------------------------------------------------------------------
# Degradation reporting convention (agent → orchestrator)
# ---------------------------------------------------------------------------
#
# An overlay agent can finish a cycle without raising and still have produced
# nothing useful: RoutePlanningAgent skips every truck it was handed,
# DeliveryPrioritizationAgent scores no orders, CompartmentLoadingAgent builds
# no plan. ``monitor_cycle`` returning normally therefore does not mean the
# cycle succeeded, and an orchestrator that treats it that way reports a silent
# skip as success.
#
# These two keys are the agent-agnostic channel for saying so. Any agent may
# set them on ``self._cycle_metrics`` at the end of ``evaluate()``; they are
# read back off the public ``cycle_metrics`` snapshot by
# ``FuelDistributionPipeline``, which imports these very names so the writer
# and the reader cannot drift apart. Route-specific detail (``route_skips``,
# ``trucks_skipped``) still rides alongside for anyone who wants it, but the
# orchestrator only needs the generic pair.
#
# Contract:
#   * ``CYCLE_METRIC_DEGRADED`` — ``bool``. ``True`` when the cycle completed
#     but did not do the whole job. Absent is equivalent to ``False``.
#   * ``CYCLE_METRIC_DEGRADATION_REASONS`` — ``list``. Structured, serialisable
#     entries explaining the degradation. Never the sole signal: a degraded
#     cycle with an empty reason list is still degraded.
#
# Reading is deliberately fail-safe on the consumer side. A monitoring signal
# must never take down the run it monitors, so a missing ``cycle_metrics``, a
# non-mapping one, or a property that raises all read as *not degraded*.
CYCLE_METRIC_DEGRADED = "degraded"
CYCLE_METRIC_DEGRADATION_REASONS = "degradation_reasons"

# Every reason entry carries a ``kind`` drawn from the two below. Both mean the
# cycle produced nothing, so both make the run DEGRADED rather than COMPLETE —
# an explicitly requested plan that contains no plan is not a success under
# either. They are distinguished because they need different *attention*:
#
#   * ``PRODUCED_NOTHING`` — the stage had input and turned none of it into
#     output. Always a defect or a real operational block worth paging on.
#   * ``NO_INPUT`` — there was nothing to work on (no tanks, no pending
#     orders, no buffered priority list). Newsworthy when a dispatcher just
#     asked for a plan, unremarkable on the 30-minute sweep of a quiet tenant.
#
# Without the distinction the orchestrator would have to log every quiet sweep
# at ERROR, and a signal that fires 48 times a day for a tenant with no orders
# is one people learn to ignore — which would cost us the ``PRODUCED_NOTHING``
# case this convention exists to surface.
DEGRADATION_KIND_PRODUCED_NOTHING = "produced_nothing"
DEGRADATION_KIND_NO_INPUT = "no_input"


def build_degradation_reason(
    *,
    reason_code: str,
    kind: str = DEGRADATION_KIND_PRODUCED_NOTHING,
    detail: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Build one entry for ``CYCLE_METRIC_DEGRADATION_REASONS``.

    Matches the shape ``RoutePlanningAgent`` already emits for route skips
    (``reason_code`` + ``detail``) so a consumer can read every stage's reasons
    the same way, and adds the ``kind`` marker the orchestrator keys its log
    severity off.

    ``extra`` carries stage-shaped counters (``tanks=0``, ``trucks=0``). Keep it
    to counts and identifiers: these entries are serialized into an API
    response, so no customer data or credentials belong here.
    """
    entry: Dict[str, Any] = {"reason_code": reason_code, "kind": kind}
    if detail:
        entry["detail"] = detail
    entry.update(extra)
    return entry


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

    def _pending_work_tenants(self) -> List[str]:
        """Tenants with buffered work that does **not** live in ``_signal_buffer``.

        Subclasses that override :meth:`_on_signal` to route typed messages
        into their own buffer must override this too, because
        :meth:`monitor_cycle` otherwise has no way to know they have work: it
        reads ``_signal_buffer`` alone, and an agent that files every message it
        cares about elsewhere leaves that buffer empty forever.

        That was a live defect. ``CompartmentLoadingAgent`` put every
        ``DeliveryPriorityList`` in ``_priority_buffer`` and
        ``RoutePlanningAgent`` put every loading proposal in
        ``_proposal_buffer``, so on the SignalBus path both agents accumulated
        work indefinitely while ``monitor_cycle`` returned ``([], [])`` before
        reaching ``evaluate()``. Nothing failed and nothing was logged.

        Returning a tenant here makes ``monitor_cycle`` evaluate for it even
        with an empty ``_signal_buffer``. ``evaluate()`` receives an empty
        signal list for such a tenant, which is correct: these agents read
        their typed buffer rather than the signals argument.

        Returns:
            Tenant ids to evaluate for. The default is empty — an agent whose
            messages all land in ``_signal_buffer`` needs nothing here.
        """
        return []

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

        # Work reaches an overlay agent through two doors: ``_signal_buffer``,
        # and a subclass's own typed buffer filled by an overridden
        # ``_on_signal``. Gating the cycle on ``signals`` alone ignored the
        # second door, so agents that route everything they consume into a
        # typed buffer were never evaluated at all. Evaluate for the union.
        tenant_groups = self._group_by_tenant(signals)
        for tenant_id in self._pending_work_tenants():
            tenant_groups.setdefault(tenant_id, [])

        if not tenant_groups:
            return [], []

        # Process per-tenant
        proposals_generated: List[Any] = []
        for tenant_id, tenant_signals in tenant_groups.items():
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

    def overlay_flag_key(self) -> str:
        """Redis flag key this agent is gated on: ``overlay.{agent_id}``.

        Exposed so the bootstrap seeder and its guard test derive the key from
        the same place the agent reads it. These keys and the fuel-ops
        *capability* flags (``overlay.bol_generation`` and friends) were two
        disjoint sets: the capability names were seeded and the agent-level
        names — the ones that actually decide whether an agent runs — were not
        set anywhere, so every overlay agent skipped every tenant.
        """
        return f"overlay.{self.agent_id}"

    async def _get_mode(self, tenant_id: str) -> str:
        """Get the overlay agent's mode for a tenant.

        Checks the feature flag service for the overlay-specific flag
        ``overlay.{agent_id}``. Returns one of: ``'disabled'``,
        ``'shadow'``, ``'active_gated'``, or ``'active_auto'``.

        When the tenant has no value for the flag, returns the deployment-wide
        default from ``settings.overlay_default_mode``.

        This used to claim a ``'shadow'`` default it could not deliver.
        ``get_overlay_state`` returns the *string* ``"disabled"`` for a missing
        key, so ``state or "shadow"`` never fell through and every unset tenant
        resolved to ``disabled`` — which ``monitor_cycle`` skips outright. The
        distinction now comes from ``get_overlay_state_or_none``, and the
        fallback is a setting rather than a literal so "the overlay agents do
        nothing" is answerable from configuration instead of from this line.
        """
        # Pipeline mode override: when running inside a pipeline context,
        # bypass feature flags and use the override mode directly.
        if hasattr(self, '_pipeline_mode_override') and self._pipeline_mode_override:
            return self._pipeline_mode_override

        default_mode = self._default_overlay_mode()

        if not self._feature_flags:
            return default_mode

        flag_key = self.overlay_flag_key()

        # Preferred: the variant that distinguishes "unset" from "disabled".
        # AttributeError covers a FeatureFlagService predating it; TypeError
        # covers a service (or test double) that exposes the name without a
        # coroutine behind it. Either way the legacy read below still answers,
        # it just cannot express "unset" — so an unset tenant resolves to
        # ``disabled`` on that path, which is the historical behaviour.
        try:
            state = await self._feature_flags.get_overlay_state_or_none(
                flag_key, tenant_id
            )
            return state or default_mode
        except (AttributeError, TypeError):
            pass
        except Exception:
            return default_mode

        try:
            state = await self._feature_flags.get_overlay_state(flag_key, tenant_id)
            return state or default_mode
        except (AttributeError, TypeError):
            pass
        except Exception:
            return default_mode

        # Last resort: the ops master flag only tells us enabled/disabled.
        try:
            enabled = await self._feature_flags.is_enabled(tenant_id)
            return default_mode if enabled else "disabled"
        except Exception:
            return default_mode

    @staticmethod
    def _default_overlay_mode() -> str:
        """Deployment-wide fallback mode for a tenant with no flag set.

        Read per call rather than cached so an operator can flip it without a
        restart in environments that reload settings. Falls back to
        ``"disabled"`` if settings cannot be loaded at all, which preserves the
        historical behaviour rather than silently activating twelve agents.
        """
        try:
            from config.settings import get_settings

            return get_settings().overlay_default_mode
        except Exception:  # noqa: BLE001 — never let config break a cycle
            return "disabled"

    async def _is_active_commit_mode(self, tenant_id: str) -> bool:
        """Whether this tenant's mode represents a real commit path.

        Shadow-mode evaluation must run the full decision logic so the
        proposal can be logged for retrospective analysis, but it must not
        mutate live state. Anything that is neither ``shadow`` nor
        ``disabled`` is a commit path: ``active_gated`` and ``active_auto``
        both flow through the ConfirmationProtocol.

        Fails closed. A mode that cannot be resolved returns ``False``, so a
        misconfigured environment never writes live state while an operator
        believes the overlay is in shadow.
        """
        try:
            mode = await self._get_mode(tenant_id)
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning(
                "%s: failed to resolve overlay mode for tenant %s, "
                "defaulting to shadow (no state write): %s",
                self.agent_id,
                tenant_id,
                exc,
            )
            return False
        if mode is None:
            return False
        return mode not in ("shadow", "disabled")

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

        Signals without a ``tenant_id`` attribute are skipped so overlay
        actions never run under an implicit tenant.
        """
        groups: Dict[str, List] = {}
        for sig in signals:
            tid = getattr(sig, "tenant_id", None)
            if not tid:
                self.logger.warning(
                    "Skipping signal without tenant_id in %s: entity_id=%s",
                    self.agent_id,
                    getattr(sig, "entity_id", None),
                )
                continue
            groups.setdefault(tid, []).append(sig)
        return groups

    @property
    def cycle_metrics(self) -> Dict[str, Any]:
        """Return a snapshot of the most recent cycle metrics."""
        return dict(self._cycle_metrics)

    def report_degradation(self, *reasons: Dict[str, Any]) -> None:
        """Record that this cycle finished without producing its output.

        The producer half of the convention at the top of this module. Call it
        from ``evaluate()`` on every path that returns without the output the
        stage exists to produce — including the early ``return []`` guards,
        which are exactly the paths that used to look like success.

        Accumulates rather than overwrites, so a stage that gives up on several
        tenants in one cycle reports all of them.
        Idempotent in effect: calling it with no reasons still marks the cycle
        degraded, because the flag rather than the list is the signal.
        """
        existing = self._cycle_metrics.get(CYCLE_METRIC_DEGRADATION_REASONS)
        merged = list(existing) if isinstance(existing, (list, tuple)) else []
        merged.extend(reasons)
        self._cycle_metrics[CYCLE_METRIC_DEGRADED] = True
        self._cycle_metrics[CYCLE_METRIC_DEGRADATION_REASONS] = merged

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
