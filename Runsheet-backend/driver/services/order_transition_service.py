"""
The driver transition gate stack — four safety gates, driver path only.

``OrderService.apply_status_transition`` is the single writer of fuel-order
status and it is **shared**: the agent mutation tools call it
(``Agents/tools/order_mutation_tools.py:279``) and so do dispatcher-initiated
transitions. A driver-specific inspection, qualification, or Hours-of-Service
gate placed inside it would therefore apply to callers the requirements never
mention. The gates live here instead, composed into one stack that the driver
router runs **before** it calls ``apply_status_transition``, and nowhere else.

Gate order is fixed and is part of the contract, because the first failing gate
determines the response code:

1. **Asset out of service** → 409 ``ASSET_OUT_OF_SERVICE``. *Unconditional.*
   No feature flag and no tenant policy value is consulted, in any tenant
   (R8.5, R8.6). This is the one gate in the stack that cannot be turned off.
2. **Pre-trip inspection** → 409 ``PRETRIP_INSPECTION_REQUIRED``. *Flag-gated*
   on the overlay key ``driver.pretrip_inspection_required``, which defaults to
   disabled, so in Phase 1 the seam is present and dormant (R8.7, R8.12).
3. **``Dispatch_Eligibility``** → 409 ``DRIVER_NOT_DISPATCH_ELIGIBLE``. The
   DQF-derived boolean from ``DriverQualificationService.is_dispatch_eligible``
   (R17.30, R17.31).
4. **Hours-of-Service verdict** → 409 ``HOS_LIMIT_REACHED``. Armed in Phase 2;
   ``hos_advisory_service`` is ``None`` in Phase 1, which makes the gate a
   no-op rather than a failure (R17.17).

The distinction between gate 1 and gate 2 is the point of this module and is
deliberately visible in the code: gate 1 never touches ``_feature_flag_service``
and gate 2 begins with a flag read. R8.11 is a scoping rule — the pre-trip flag
is consulted in exactly two places, the R8.7 gate here and the R8.8 post-trip
accept path, and nowhere in the R8.5 / R8.6 / R8.9 paths.

Two determinism rules shape the ordering of gates 3 and 4. R17.30 requires the
HOS gate to be evaluated *independently* of ``Dispatch_Eligibility``, and
R17.31 requires that when **both** fail the response is
``DRIVER_NOT_DISPATCH_ELIGIBLE`` carrying the HOS reason code in details. So the
eligibility gate resolves the HOS verdict before it raises, and only ever
raises its own error code — the combined-failure response is deterministic
rather than a race between two gates.

Only transitions to ``in_transit`` are gated. Every requirement in this stack
(R8.6, R8.7, R17.30) names that target status specifically, and a transition to
``delivered`` or ``failed`` must never be blocked by a gate — a driver who has
already completed a delivery has to be able to record it.

Collaborators arrive through the constructor from
:func:`configure_transition_endpoints`, matching the wiring pattern of
``driver/services/pod_service.py`` and ``driver/services/work_service.py``: no
container lookup, no service locator, no FastAPI ``Depends``.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Order Transition Path.

Validates: Requirements 8.5, 8.6, 17.30
- 8.5, 8.6: the out-of-service gate rejects with 409 ``ASSET_OUT_OF_SERVICE`` in
  every tenant, consulting no feature flag and no tenant policy value
- 17.30: a transition to ``in_transit`` is permitted only when the HOS gate
  passes **and** ``Dispatch_Eligibility`` is true, the two evaluated
  independently
- 17.31 (partial, completed in Phase 2): combined failure answers
  ``DRIVER_NOT_DISPATCH_ELIGIBLE`` with the HOS reason code in details
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from errors.exceptions import (
    asset_out_of_service,
    driver_not_dispatch_eligible,
    hos_limit_reached,
    pretrip_inspection_required,
)

logger = logging.getLogger(__name__)

#: The only target status the stack gates. Every requirement in the stack names
#: ``in_transit``; ``delivered`` and ``failed`` are never gated, because a
#: completed delivery must always be recordable.
GATED_TARGET_STATUSES = frozenset({"in_transit"})

#: Gate identifiers, in evaluation order. Exposed so a test can pin the order
#: without reaching into the stack's internals — the order is contractual,
#: since the first failing gate picks the response code.
GATE_ORDER: Tuple[str, ...] = (
    "asset_out_of_service",
    "pretrip_inspection",
    "dispatch_eligibility",
    "hos",
)

#: Overlay feature-flag key for the pre-trip requirement (R8.12). Consulted by
#: gate 2 only. Gate 1 must never read a flag (R8.5, R8.6).
PRETRIP_FLAG_KEY = "driver.pretrip_inspection_required"

#: Overlay states that mean "enforce". ``shadow`` observes without blocking and
#: ``disabled`` is the default, so both leave the pre-trip gate dormant.
_ENFORCING_OVERLAY_STATES = frozenset({"active_gated", "active_auto"})


# ---------------------------------------------------------------------------
# Evaluation record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateOutcome:
    """What one gate decided, and why.

    Attributes:
        gate: One of :data:`GATE_ORDER`.
        outcome: ``passed``, ``blocked``, or ``skipped``. ``skipped`` means the
            gate could not be evaluated (flag disabled, collaborator absent) —
            recorded rather than silently dropped, because Phase 2 writes an
            audit event carrying ``hos_gate_skipped`` and its reason code.
        reason_code: A machine-readable reason, when the gate has one.
        detail: Extra context for the audit record and the error details.
    """

    gate: str
    outcome: str
    reason_code: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateEvaluation:
    """The result of running the whole stack.

    ``allowed`` is true only when no gate blocked. A blocking gate raises, so a
    returned evaluation always has ``allowed=True``; the record exists so the
    caller can log or audit which gates ran, which were skipped, and why.
    """

    target_status: str
    gated: bool
    outcomes: Tuple[GateOutcome, ...] = ()

    @property
    def allowed(self) -> bool:
        return all(o.outcome != "blocked" for o in self.outcomes)

    def skipped_reasons(self) -> Dict[str, Optional[str]]:
        """``{gate: reason_code}`` for every gate that could not be evaluated."""
        return {
            o.gate: o.reason_code for o in self.outcomes if o.outcome == "skipped"
        }


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------


class DriverTransitionGateStack:
    """Composes the four driver-path gates and applies them in a fixed order.

    Args:
        driver_qualification_service: Anything exposing
            ``is_dispatch_eligible(tenant_id, driver_id, route_requirements)``
            returning an object (or dict) with ``eligible`` and ``reasons``.
            Absent → the eligibility gate is skipped.
        inspection_service: Anything exposing
            ``is_asset_out_of_service(tenant_id, asset_id)`` and, for Phase 2,
            ``has_pretrip_inspection(tenant_id, driver_id, asset_id,
            local_date)``. Absent → both inspection-derived gates are skipped;
            see :meth:`_gate_asset_out_of_service` for why that is sound rather
            than a hole.
        feature_flag_service: ``FeatureFlagService``. Read by the pre-trip gate
            only. Absent or unreachable → the flag reads as disabled, which is
            the documented fail-closed-to-off posture of
            ``get_overlay_state``.
        hos_advisory_service: Anything exposing
            ``gate_verdict(tenant_id, driver_id)``. ``None`` in Phase 1.
    """

    def __init__(
        self,
        *,
        driver_qualification_service=None,
        inspection_service=None,
        feature_flag_service=None,
        hos_advisory_service=None,
    ) -> None:
        self._driver_qualification_service = driver_qualification_service
        self._inspection_service = inspection_service
        self._feature_flag_service = feature_flag_service
        self._hos_advisory_service = hos_advisory_service

    # -- public entry point --------------------------------------------

    async def evaluate(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        order: Any,
        target_status: str,
        local_date: Optional[str] = None,
    ) -> GateEvaluation:
        """Run the stack, raising on the first gate that blocks.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The acting driver, from the verified session claim.
            order: The fuel-order document or model the transition targets.
            target_status: The requested new status.
            local_date: The driver's calendar day as ``YYYY-MM-DD``, for the
                pre-trip gate's "first transition in a calendar day" rule.
                Defaults to the UTC date; Phase 2 resolves the tenant timezone.

        Returns:
            A :class:`GateEvaluation` recording what every gate decided.

        Raises:
            AppException: 409 ``ASSET_OUT_OF_SERVICE``,
                ``PRETRIP_INSPECTION_REQUIRED``,
                ``DRIVER_NOT_DISPATCH_ELIGIBLE``, or ``HOS_LIMIT_REACHED``,
                from the first gate that blocks.

        Validates: Requirements 8.5, 8.6, 17.30
        """
        if target_status not in GATED_TARGET_STATUSES:
            # delivered / failed / cancelled are never gated: a driver who has
            # finished a delivery must always be able to record it.
            return GateEvaluation(target_status=target_status, gated=False)

        doc = _as_document(order)
        asset_id = doc.get("assigned_asset_id") or None
        order_id = doc.get("order_id") or doc.get("id") or None
        day = local_date or datetime.now(timezone.utc).date().isoformat()

        outcomes: list[GateOutcome] = []

        # 1 — unconditional
        outcomes.append(
            await self._gate_asset_out_of_service(
                tenant_id=tenant_id, asset_id=asset_id, order_id=order_id
            )
        )
        # 2 — flag-gated, dormant in Phase 1
        outcomes.append(
            await self._gate_pretrip_inspection(
                tenant_id=tenant_id,
                driver_id=driver_id,
                asset_id=asset_id,
                order_id=order_id,
                local_date=day,
            )
        )
        # 3 — DQF, armed in Phase 1. Resolves the HOS verdict before raising so
        #     a combined failure is deterministic (R17.31).
        outcomes.append(
            await self._gate_dispatch_eligibility(
                tenant_id=tenant_id, driver_id=driver_id, order_id=order_id
            )
        )
        # 4 — HOS, armed in Phase 2
        outcomes.append(
            await self._gate_hos(
                tenant_id=tenant_id, driver_id=driver_id, order_id=order_id
            )
        )

        evaluation = GateEvaluation(
            target_status=target_status, gated=True, outcomes=tuple(outcomes)
        )
        skipped = evaluation.skipped_reasons()
        if skipped:
            logger.debug(
                "Driver transition gates skipped for tenant=%s driver=%s: %s",
                tenant_id,
                driver_id,
                skipped,
            )
        return evaluation

    # -- gate 1: out of service (unconditional, R8.5/R8.6) --------------

    async def _gate_asset_out_of_service(
        self, *, tenant_id: str, asset_id: Optional[str], order_id: Optional[str]
    ) -> GateOutcome:
        """Reject when the assigned asset carries state ``out_of_service``.

        Unconditional by construction: this method holds no reference to
        ``_feature_flag_service`` and reads no tenant policy value, in any
        tenant (R8.5, R8.6).

        Two skip paths, neither of them a hole:

        * **No ``assigned_asset_id`` on the order.** There is no asset whose
          state could be out of service.
        * **No ``Inspection_Service``.** The out-of-service state originates
          from driver-submitted inspection reports, and ``Inspection_Service``
          is its only writer and its only reader (the denormalized
          ``vehicle_inspections.has_out_of_service_defect`` term filter). With
          no such service wired, no such state can exist to be read.

        Both are recorded as ``skipped`` rather than ``passed`` so a degraded
        boot is visible in the evaluation record instead of looking like a
        clean pass.
        """
        if not asset_id:
            return GateOutcome(
                gate="asset_out_of_service",
                outcome="skipped",
                reason_code="NO_ASSIGNED_ASSET",
            )

        check = getattr(self._inspection_service, "is_asset_out_of_service", None)
        if not callable(check):
            return GateOutcome(
                gate="asset_out_of_service",
                outcome="skipped",
                reason_code="INSPECTION_SERVICE_UNAVAILABLE",
                detail={"asset_id": asset_id},
            )

        out_of_service = await check(tenant_id, asset_id)
        if out_of_service:
            raise asset_out_of_service(
                details=_details(order_id=order_id, asset_id=asset_id),
            )
        return GateOutcome(
            gate="asset_out_of_service",
            outcome="passed",
            detail={"asset_id": asset_id},
        )

    # -- gate 2: pre-trip inspection (flag-gated, Phase 2) --------------

    async def _gate_pretrip_inspection(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        asset_id: Optional[str],
        order_id: Optional[str],
        local_date: str,
    ) -> GateOutcome:
        """Reject the day's first ``in_transit`` with no pre-trip inspection.

        Unlike gate 1 this gate **is** flag-gated: it begins with a read of the
        overlay key ``driver.pretrip_inspection_required``, which defaults to
        disabled, so in Phase 1 the seam is present and never fires (R8.7,
        R8.12). ``get_overlay_state`` fails closed to disabled when Redis is
        unavailable, which is the correct default here.
        """
        if not await self._pretrip_required(tenant_id):
            return GateOutcome(
                gate="pretrip_inspection",
                outcome="skipped",
                reason_code="PRETRIP_FLAG_DISABLED",
            )
        if not asset_id:
            return GateOutcome(
                gate="pretrip_inspection",
                outcome="skipped",
                reason_code="NO_ASSIGNED_ASSET",
            )

        check = getattr(self._inspection_service, "has_pretrip_inspection", None)
        if not callable(check):
            return GateOutcome(
                gate="pretrip_inspection",
                outcome="skipped",
                reason_code="INSPECTION_SERVICE_UNAVAILABLE",
                detail={"asset_id": asset_id},
            )

        has_inspection = await check(tenant_id, driver_id, asset_id, local_date)
        if not has_inspection:
            raise pretrip_inspection_required(
                details=_details(
                    order_id=order_id,
                    asset_id=asset_id,
                    inspection_local_date=local_date,
                ),
            )
        return GateOutcome(
            gate="pretrip_inspection",
            outcome="passed",
            detail={"asset_id": asset_id, "inspection_local_date": local_date},
        )

    async def _pretrip_required(self, tenant_id: str) -> bool:
        """Read the pre-trip overlay flag, defaulting to disabled.

        The only flag read in this module (R8.11). An absent service, a
        disconnected Redis client, or any read failure means disabled.
        """
        get_state = getattr(self._feature_flag_service, "get_overlay_state", None)
        if not callable(get_state):
            return False
        try:
            state = await get_state(PRETRIP_FLAG_KEY, tenant_id)
        except Exception as exc:
            logger.warning(
                "Pre-trip inspection flag unreadable for tenant=%s (%s) — "
                "treating as disabled",
                tenant_id,
                exc,
            )
            return False
        return state in _ENFORCING_OVERLAY_STATES

    # -- gate 3: Dispatch_Eligibility (R17.30, R17.31) ------------------

    async def _gate_dispatch_eligibility(
        self, *, tenant_id: str, driver_id: str, order_id: Optional[str]
    ) -> GateOutcome:
        """Reject when ``Dispatch_Eligibility`` is false.

        The HOS verdict is resolved **before** this gate raises, so a combined
        failure answers ``DRIVER_NOT_DISPATCH_ELIGIBLE`` carrying the HOS
        reason code in details rather than depending on which gate ran first
        (R17.31). The two evaluations stay independent (R17.30): neither
        verdict is derived from the other.

        A failure to *compute* eligibility is a skip, not a block. The DQF
        record lives in the compliance ``drivers`` index and a driver with no
        DQF record would otherwise have every transition rejected; an explicit
        ``eligible=False`` verdict is the only thing that blocks.
        """
        check = getattr(
            self._driver_qualification_service, "is_dispatch_eligible", None
        )
        if not callable(check):
            return GateOutcome(
                gate="dispatch_eligibility",
                outcome="skipped",
                reason_code="QUALIFICATION_SERVICE_UNAVAILABLE",
            )

        try:
            verdict = await check(tenant_id, driver_id)
        except Exception as exc:
            logger.warning(
                "Dispatch eligibility unresolved for tenant=%s driver=%s (%s) — "
                "gate skipped",
                tenant_id,
                driver_id,
                exc,
            )
            return GateOutcome(
                gate="dispatch_eligibility",
                outcome="skipped",
                reason_code="ELIGIBILITY_UNRESOLVED",
            )

        eligible, reasons = _eligibility(verdict)
        if eligible:
            return GateOutcome(gate="dispatch_eligibility", outcome="passed")

        hos_reason_code = await self._hos_reason_code(
            tenant_id=tenant_id, driver_id=driver_id
        )
        raise driver_not_dispatch_eligible(
            details=_details(
                order_id=order_id,
                reasons=reasons or None,
                hos_reason_code=hos_reason_code,
            ),
        )

    # -- gate 4: Hours-of-Service (Phase 2) ----------------------------

    async def _gate_hos(
        self, *, tenant_id: str, driver_id: str, order_id: Optional[str]
    ) -> GateOutcome:
        """Reject when the HOS gate verdict blocks the transition.

        ``hos_advisory_service`` is ``None`` in Phase 1, so this is a recorded
        skip and never a block. Phase 2 supplies ``gate_verdict``, whose own
        posture is fail-open: only a fresh reading at or past a limit, with
        gating enabled on both switches and no unexpired override, blocks.
        """
        verdict = await self._hos_verdict(tenant_id=tenant_id, driver_id=driver_id)
        if verdict is None:
            return GateOutcome(
                gate="hos",
                outcome="skipped",
                reason_code="HOS_GATE_NOT_ARMED",
            )

        blocked, reason_code, recorded_at = verdict
        if blocked:
            raise hos_limit_reached(
                details=_details(
                    order_id=order_id,
                    reason_code=reason_code,
                    recorded_at=recorded_at,
                ),
            )
        return GateOutcome(
            gate="hos",
            outcome="passed",
            reason_code=reason_code,
            detail={"recorded_at": recorded_at} if recorded_at else {},
        )

    async def _hos_verdict(
        self, *, tenant_id: str, driver_id: str
    ) -> Optional[Tuple[bool, Optional[str], Optional[str]]]:
        """Resolve ``(blocked, reason_code, recorded_at)``, or ``None``.

        ``None`` means the gate is not armed or the verdict could not be
        resolved — the fail-open posture R17.19 requires.
        """
        gate_verdict = getattr(self._hos_advisory_service, "gate_verdict", None)
        if not callable(gate_verdict):
            return None
        try:
            verdict = await gate_verdict(tenant_id, driver_id)
        except Exception as exc:
            logger.warning(
                "HOS gate verdict unresolved for tenant=%s driver=%s (%s) — "
                "gate skipped (fail-open)",
                tenant_id,
                driver_id,
                exc,
            )
            return None
        if verdict is None:
            return None

        blocked = bool(_attr(verdict, "blocked", False))
        reason_code = _attr(verdict, "reason_code", None)
        recorded_at = _attr(verdict, "recorded_at", None)
        if isinstance(recorded_at, datetime):
            recorded_at = recorded_at.isoformat()
        return blocked, reason_code, recorded_at

    async def _hos_reason_code(
        self, *, tenant_id: str, driver_id: str
    ) -> Optional[str]:
        """The HOS reason code to fold into a combined failure (R17.31)."""
        verdict = await self._hos_verdict(tenant_id=tenant_id, driver_id=driver_id)
        if verdict is None:
            return None
        blocked, reason_code, _ = verdict
        return reason_code if blocked else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_document(order: Any) -> Dict[str, Any]:
    """Normalize a ``FuelOrder`` model or raw document into a plain dict."""
    if order is None:
        return {}
    if isinstance(order, dict):
        return order
    model_dump = getattr(order, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    try:
        return dict(order)
    except (TypeError, ValueError):
        return {}


def _attr(source: Any, name: str, default: Any) -> Any:
    """Read ``name`` from a model or a dict, so the seam is shape-agnostic."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _eligibility(verdict: Any) -> Tuple[bool, list]:
    """Normalize a ``DriverEligibility`` (or dict) into ``(eligible, reasons)``.

    An unreadable verdict is treated as eligible: only an explicit
    ``eligible=False`` blocks a transition.
    """
    eligible = _attr(verdict, "eligible", True)
    reasons = _attr(verdict, "reasons", None) or []
    return bool(eligible), list(reasons)


def _details(**kwargs: Any) -> Dict[str, Any]:
    """Build error details, dropping empty values.

    Details name the order, the asset, and the caller's own reason codes. They
    never carry the caller's held roles and never another driver's identity
    (R15.14).
    """
    return {key: value for key, value in kwargs.items() if value not in (None, "", [])}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

#: Module globals, assigned by :func:`configure_transition_endpoints` in the
#: established pattern of the other driver ``configure_*`` functions. The
#: driver status endpoint (task 9.2) reads them through the accessors below.
_order_repository = None
_order_service = None
_gate_stack: Optional[DriverTransitionGateStack] = None
_work_ref_resolver = None


def configure_transition_endpoints(
    order_repository=None,
    order_service=None,
    driver_qualification_service=None,
    inspection_service=None,
    feature_flag_service=None,
    hos_advisory_service=None,
) -> DriverTransitionGateStack:
    """Wire the driver transition surface. Called from ``bootstrap/driver.py``.

    This cannot be wired before ``bootstrap/compliance.py`` has run, because
    ``Dispatch_Eligibility`` comes from
    ``compliance/services/driver_qualification_service.py``. ``bootstrap/driver``
    sits after ``compliance`` in ``_BOOT_ORDER``, which is what makes this the
    right home for the call.

    Every argument is reset on each call, as the other driver ``configure_*``
    functions do, so the last caller wins and a partial argument set cannot
    leave a stale collaborator behind.

    Args:
        order_repository: ``FuelOrderRepository``, for the status endpoint's
            order resolution (task 9.2).
        order_service: ``OrderService``, the single writer of order status.
        driver_qualification_service: Supplies ``Dispatch_Eligibility``.
        inspection_service: Supplies the out-of-service and pre-trip verdicts.
        feature_flag_service: Read by the pre-trip gate only.
        hos_advisory_service: ``None`` in Phase 1; Phase 2 arms the HOS gate.

    Returns:
        The composed gate stack, also retrievable via :func:`get_gate_stack`.
    """
    global _order_repository, _order_service, _gate_stack, _work_ref_resolver

    _order_repository = order_repository
    _order_service = order_service
    _gate_stack = DriverTransitionGateStack(
        driver_qualification_service=driver_qualification_service,
        inspection_service=inspection_service,
        feature_flag_service=feature_flag_service,
        hos_advisory_service=hos_advisory_service,
    )

    if order_repository is not None:
        from driver.services.work_ref import WorkRefResolver

        _work_ref_resolver = WorkRefResolver(order_repository=order_repository)
    else:
        _work_ref_resolver = None

    return _gate_stack


def get_gate_stack() -> Optional[DriverTransitionGateStack]:
    """The composed gate stack, or ``None`` before wiring."""
    return _gate_stack


def get_order_service():
    """``OrderService``, or ``None`` when it is not wired."""
    return _order_service


def get_order_repository():
    """``FuelOrderRepository``, or ``None`` when it is not wired."""
    return _order_repository


def get_work_ref_resolver():
    """The order-keyed ``WorkRefResolver``, or ``None`` before wiring."""
    return _work_ref_resolver
