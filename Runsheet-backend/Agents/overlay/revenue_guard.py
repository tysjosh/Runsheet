"""
Revenue Guard — Layer 1 overlay agent for margin protection (margin-feed FR5).

RevenueGuard reads margin records, never ``jobs_current`` (AC-45). Its work
queue is the database, not the in-process SignalBus (design freeze 7):

* ``margin_records.alert_state='pending'`` is set by the margin repository in
  the same insert as a flagged live delivery/invoice record;
* ``margin_recompute_runs.digest_state='pending'`` is set when a recompute run
  completes with flagged records.

A record can be written on any ECS task, but ``monitor_cycle`` runs only on
the sweep leader, so the leader drains both queues. The ``margin_feed``
RiskSignal is a hint for other consumers: ``_on_signal`` discards it, so no
process accumulates a buffer.

Per record (FR5.3, FR5.4):

* ``negative_margin`` → one ``high`` alert; ``missing_cost`` → one ``medium``
  alert. ``dedupe_key=record_id`` plus ``uq_malert_dedupe`` keep it to one
  alert per record across cycles, instances and tasks.
* ``below_floor`` → no per-record alert. When the last ``leakage_threshold``
  sales for (tenant, customer, product) are all below floor, one
  ``PolicyChangeProposal`` plus one ``leakage_proposal`` alert in
  ``pending_review`` (HIGH risk, human approval, 60-minute cooldown per key).

Modes (AC-43): ``disabled`` sends nothing and leaves rows pending (they expire
after 7 days); ``shadow`` writes activity-log entries only; ``active_gated`` /
``active_auto`` write alerts. Proposals and activity entries carry ids and
flag names, never money (FR8.2). RevenueGuard never changes prices (FR5.9).

RevenueGuard owns its loop (design Simplification 11): ``monitor_cycle`` is
overridden completely and ``evaluate`` is a stub.
"""
import logging
import time
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    PolicyChangeProposal,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

#: Consecutive below-floor sales that trigger a leakage proposal (FR5.4).
DEFAULT_LEAKAGE_THRESHOLD = 3
#: Pending rows older than this expire without an alert (AC-43, freeze 7).
PENDING_EXPIRY = timedelta(days=7)
#: Source of the margin service's RiskSignal hints (FR5.2).
MARGIN_SIGNAL_SOURCE = "margin_feed"

#: Activity-log actions (strict ``agent_activity_log`` mapping: ``action_type``).
ACTION_ALERT_SHADOW = "margin_alert_shadow"
ACTION_LEAKAGE_SHADOW = "margin_leakage_shadow"
ACTION_DIGEST_SHADOW = "margin_digest_shadow"

_COMMIT_MODES = ("active_gated", "active_auto")
_FLAG_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("missing_cost", "flag_missing_cost"),
    ("negative_margin", "flag_negative_margin"),
    ("below_floor", "flag_below_floor"),
    ("terminal_unattributed", "flag_terminal_unattributed"),
)


def _flags(record: Mapping[str, Any]) -> List[str]:
    return [name for name, column in _FLAG_COLUMNS if record.get(column)]


def _leakage_stage() -> str:
    """``invoice`` when invoicing is on, else ``delivery`` (Simplification 2)."""

    from config.settings import get_settings

    return "invoice" if getattr(get_settings(), "commerce_invoicing_enabled", False) else "delivery"


def _feed_active() -> bool:
    from commerce.services.margin_service import margin_feed_active

    return margin_feed_active()


class RevenueGuard(OverlayAgentBase):
    """Margin alerts and leakage proposals from the margin DB queue.

    Args:
        signal_bus: SignalBus for pub/sub (proposals are published on it).
        es_service: Document store (only the base class's shadow index uses
            it, and RevenueGuard overrides that path; never ``jobs_current``).
        activity_log_service: Activity log for shadow-mode entries.
        ws_manager: WebSocket manager.
        confirmation_protocol: Base-class dependency; a PolicyChangeProposal
            never reaches it.
        autonomy_config_service: For mode management.
        feature_flag_service: Per-tenant ``overlay.revenue_guard`` mode.
        leakage_threshold: Consecutive below-floor sales that trigger a
            proposal (default 3).
        poll_interval: Seconds between cycles (default 60).
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
        leakage_threshold: int = DEFAULT_LEAKAGE_THRESHOLD,
        poll_interval: int = 60,
    ):
        super().__init__(
            agent_id="revenue_guard",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": RiskSignal,
                    "filters": {"source_agent": MARGIN_SIGNAL_SOURCE},
                },
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            # Explicit: the base default is 15 and AC-40 needs 60.
            cooldown_minutes=60,
        )
        self._leakage_threshold = leakage_threshold
        self._repo: Any = None
        self._warned_no_repo = False
        self._warned_no_activity = False
        self._signals_discarded = 0
        # proposal_id -> (customer_id, product_code) for the shadow activity
        # entry; filled and drained within one cycle.
        self._proposal_keys: Dict[str, Tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def set_margin_repository(self, repository: Any) -> None:
        """Inject the ``MarginRepository`` (bootstrap/agents)."""
        self._repo = repository

    # ------------------------------------------------------------------
    # Signals (hints only, freeze 7)
    # ------------------------------------------------------------------

    async def _on_signal(self, signal) -> None:
        """Discard: the work comes from ``alert_state``, never a buffer."""
        self._signals_discarded += 1
        self.logger.debug(
            "revenue_guard discarded margin hint #%d entity=%s",
            self._signals_discarded,
            getattr(signal, "entity_id", None),
        )

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Abstract-method stub; RevenueGuard's work is in ``monitor_cycle``."""
        return []

    # ------------------------------------------------------------------
    # Cycle (Simplification 11)
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        if self._repo is None:
            if not self._warned_no_repo:
                self.logger.warning(
                    "revenue_guard: no margin repository wired; cycles do nothing"
                )
                self._warned_no_repo = True
            return [], []
        if not _feed_active():
            return [], []
        started = time.monotonic()
        try:
            tenants = await self._repo.discovery.pending_work_tenants()
            # Before the mode read, so a disabled tenant's rows still expire.
            for tenant_id in tenants:
                await self._repo.expire_stale_pending(tenant_id, older_than=PENDING_EXPIRY)
        except Exception:
            self.logger.exception("revenue_guard queue query failed")
            return [], []

        proposals_all: List[PolicyChangeProposal] = []
        for tenant_id in tenants:
            try:
                mode = await self._get_mode(tenant_id)
            except Exception:
                self.logger.exception("revenue_guard mode read failed tenant=%s", tenant_id)
                continue  # rows stay pending
            self._cycle_metrics["mode"] = mode
            if mode == "disabled":
                continue  # rows stay pending (AC-43)
            commit = mode in _COMMIT_MODES  # fail closed on unknown modes
            try:
                proposals = await self._evaluate_tenant(tenant_id, commit)
            except Exception:
                self.logger.exception("revenue_guard tenant pass failed tenant=%s", tenant_id)
                continue
            for proposal in proposals:
                if mode == "shadow":
                    await self._log_shadow_proposal(proposal)
                else:
                    await self._route_proposal(proposal, mode)
            proposals_all.extend(proposals)
        self._proposal_keys.clear()

        self._cycle_metrics.update({
            "signals_consumed": 0,
            "proposals_generated": len(proposals_all),
            "cycle_duration_ms": (time.monotonic() - started) * 1000,
        })
        return [], proposals_all

    async def _evaluate_tenant(
        self, tenant_id: str, commit: bool
    ) -> List[PolicyChangeProposal]:
        """Drain one tenant's digests and up to 500 pending records."""

        for run in await self._repo.pending_digests(tenant_id):
            try:
                await self._process_digest(tenant_id, run, commit)
            except Exception as exc:
                self.logger.error(
                    "revenue_guard digest failed tenant=%s run=%s error=%s",
                    tenant_id, run.get("run_id"), type(exc).__name__,
                )

        leakage_stage = _leakage_stage()
        proposals: List[PolicyChangeProposal] = []
        for record in await self._repo.pending_records(tenant_id):
            try:
                proposal = await self._process_record(tenant_id, record, commit, leakage_stage)
            except Exception as exc:
                # The record stays pending for the next cycle.
                self.logger.error(
                    "revenue_guard record failed tenant=%s record=%s error=%s",
                    tenant_id, record.get("record_id"), type(exc).__name__,
                )
                continue
            if proposal is not None:
                proposals.append(proposal)
        return proposals

    # ------------------------------------------------------------------
    # Digests (FR5.8, AC-44)
    # ------------------------------------------------------------------

    async def _process_digest(self, tenant_id: str, run: Mapping[str, Any], commit: bool) -> None:
        run_id = run["run_id"]
        counts = dict(run.get("counts") or {})
        if commit:
            alert = {
                "alert_type": "recompute_digest",
                "severity": "info",
                "dedupe_key": run_id,
                "run_id": run_id,
                "details": counts,
            }
            await self._repo.mark_digest_done(tenant_id, run_id, alert=alert)
            return
        await self._activity(
            tenant_id,
            ACTION_DIGEST_SHADOW,
            {"run_id": run_id, "counts_by_flag": dict(counts.get("by_flag") or {})},
        )
        await self._repo.mark_digest_done(tenant_id, run_id)

    # ------------------------------------------------------------------
    # Records (FR5.3, FR5.4)
    # ------------------------------------------------------------------

    async def _process_record(
        self,
        tenant_id: str,
        record: Mapping[str, Any],
        commit: bool,
        leakage_stage: str,
    ) -> Optional[PolicyChangeProposal]:
        record_id = record["record_id"]
        if record.get("status") != "active":
            # Guard: the writer normally moves superseded/void rows to done.
            await self._repo.mark_record_done(tenant_id, record_id)
            return None

        alerts: List[Dict[str, Any]] = []
        common = {
            "dedupe_key": record_id,
            "record_id": record_id,
            "order_id": record.get("order_id"),
            "customer_id": record.get("customer_id"),
            "product_code": record.get("product_code"),
        }
        if record.get("flag_negative_margin"):
            alerts.append({
                **common,
                "alert_type": "negative_margin",
                "severity": "high",
                "details": {"stage": record.get("stage"), "flags": _flags(record)},
            })
        if record.get("flag_missing_cost"):
            alerts.append({
                **common,
                "alert_type": "missing_cost",
                "severity": "medium",
                "details": {
                    "stage": record.get("stage"),
                    "flags": _flags(record),
                    "no_cost_reason": record.get("no_cost_reason"),
                },
            })

        proposal: Optional[PolicyChangeProposal] = None
        cooldown_key: Optional[str] = None
        leakage_alert_id: Optional[str] = None
        if record.get("flag_below_floor") and record.get("stage") == leakage_stage:
            found = await self._check_leakage(tenant_id, record, leakage_stage)
            if found is not None:
                proposal, cooldown_key, newest_record_id = found
                if commit:
                    leakage_alert_id = f"malert_{uuid.uuid4().hex}"
                    alerts.append({
                        "alert_id": leakage_alert_id,
                        "alert_type": "leakage_proposal",
                        "severity": "high",
                        "status": "pending_review",
                        "dedupe_key": (
                            f"{record['customer_id']}|{record['product_code']}|{newest_record_id}"
                        ),
                        "record_id": record_id,
                        "order_id": record.get("order_id"),
                        "proposal_id": proposal.proposal_id,
                        "customer_id": record.get("customer_id"),
                        "product_code": record.get("product_code"),
                        "details": {
                            "evidence": list(proposal.evidence),
                            "consecutive": self._leakage_threshold,
                            "stage": leakage_stage,
                        },
                    })

        if commit:
            inserted = await self._repo.record_alert_outcome(tenant_id, record_id, alerts)
            if leakage_alert_id is not None and leakage_alert_id not in inserted:
                # Another instance already raised this leakage alert (dedupe
                # no-op): publish no second proposal.
                proposal = None
        else:
            if alerts:
                await self._activity(
                    tenant_id,
                    ACTION_ALERT_SHADOW,
                    {
                        "record_id": record_id,
                        "order_id": record.get("order_id"),
                        "stage": record.get("stage"),
                        "flags": _flags(record),
                        "alert_types": [a["alert_type"] for a in alerts],
                    },
                )
            await self._repo.mark_record_done(tenant_id, record_id)

        if cooldown_key is not None:
            self._set_cooldown(cooldown_key)
        if proposal is not None:
            self._proposal_keys[proposal.proposal_id] = (
                record["customer_id"], record["product_code"]
            )
        return proposal

    async def _check_leakage(
        self, tenant_id: str, record: Mapping[str, Any], stage: str
    ) -> Optional[Tuple[PolicyChangeProposal, str, str]]:
        """Leakage check for one (tenant, customer, product); ids only."""

        customer_id = record.get("customer_id")
        product_code = record.get("product_code")
        if not customer_id or not product_code:
            return None
        key = f"leak:{tenant_id}:{customer_id}:{product_code}"
        if self._is_on_cooldown(key):
            return None
        threshold = self._leakage_threshold
        sales = await self._repo.leakage_window(
            tenant_id,
            customer_id=customer_id,
            product_code=product_code,
            stage=stage,
            threshold=threshold,
        )
        if len(sales) < threshold or not all(sale["below_floor"] for sale in sales):
            return None
        if await self._repo.has_pending_leakage(
            tenant_id, customer_id=customer_id, product_code=product_code
        ):
            return None
        evidence = [rid for sale in sales for rid in sale["record_ids"]]
        proposal = PolicyChangeProposal(
            source_agent=self.agent_id,
            tenant_id=tenant_id,
            parameter=f"margin.review.{customer_id}.{product_code}",
            old_value={"flag": "below_floor", "consecutive": len(sales)},
            new_value={"action": "review_pricing"},
            evidence=evidence,
            rollback_plan={"action": "none", "reason": "advisory only"},
            confidence=0.9,
        )
        return proposal, key, sales[0]["record_ids"][0]

    # ------------------------------------------------------------------
    # Activity log (shadow mode, AC-43)
    # ------------------------------------------------------------------

    async def _activity(self, tenant_id: str, action: str, details: Dict[str, Any]) -> None:
        """Write one ids-only activity entry; raises if the write fails."""

        if self._activity_log is None:
            if not self._warned_no_activity:
                self.logger.warning("revenue_guard: no activity log service; shadow entries skipped")
                self._warned_no_activity = True
            return
        await self._activity_log.log({
            "agent_id": self.agent_id,
            "action_type": action,
            "outcome": "shadow",
            "tenant_id": tenant_id,
            "details": details,
        })

    async def _log_shadow_proposal(self, proposal) -> None:
        """Shadow leakage: an activity entry, never ``agent_shadow_proposals``."""

        customer_id, product_code = self._proposal_keys.pop(
            getattr(proposal, "proposal_id", None), (None, None)
        )
        try:
            await self._activity(
                proposal.tenant_id,
                ACTION_LEAKAGE_SHADOW,
                {
                    "proposal_id": proposal.proposal_id,
                    "evidence": list(proposal.evidence),
                    "customer_id": customer_id,
                    "product_code": product_code,
                },
            )
        except Exception as exc:
            self.logger.error(
                "revenue_guard shadow leakage entry failed tenant=%s proposal=%s error=%s",
                proposal.tenant_id, proposal.proposal_id, type(exc).__name__,
            )
