"""
Unit tests for the driver transition gate stack.

Covers what task 9.1 puts in place: the four gates, the fixed order in which
they run, the unconditional posture of the out-of-service gate versus the
flag-gated pre-trip gate, the Phase 1 dormancy of the pre-trip and HOS gates,
the deterministic combined-failure response, and the wiring function.

Validates: Requirements 8.5, 8.6, 17.30
"""
import pytest

from driver.services.order_transition_service import (
    GATE_ORDER,
    GATED_TARGET_STATUSES,
    HOS_BLOCK_EVENT,
    PRETRIP_FLAG_KEY,
    DriverTransitionGateStack,
    configure_transition_endpoints,
    get_gate_stack,
    get_order_repository,
    get_order_service,
    get_work_ref_resolver,
)
from errors.exceptions import AppException

TENANT = "tenant-a"
DRIVER = "driver-1"
ASSET = "truck-7"
ORDER = {"order_id": "ord-1", "tenant_id": TENANT, "assigned_asset_id": ASSET}


# ---------------------------------------------------------------------------
# Fakes — each records the calls it received so gate order is observable
# ---------------------------------------------------------------------------


class FakeInspectionService:
    def __init__(self, *, out_of_service=False, has_pretrip=True, calls=None):
        self._out_of_service = out_of_service
        self._has_pretrip = has_pretrip
        self.calls = calls if calls is not None else []

    async def is_asset_out_of_service(self, tenant_id, asset_id):
        self.calls.append(("asset_out_of_service", tenant_id, asset_id))
        return self._out_of_service

    async def has_pretrip_inspection(self, tenant_id, driver_id, asset_id, local_date):
        self.calls.append(("pretrip_inspection", tenant_id, driver_id, local_date))
        return self._has_pretrip


class FakeQualificationService:
    def __init__(self, *, eligible=True, reasons=None, raises=None, calls=None):
        self._eligible = eligible
        self._reasons = reasons or []
        self._raises = raises
        self.calls = calls if calls is not None else []

    async def is_dispatch_eligible(self, tenant_id, driver_id, route_requirements=None):
        self.calls.append(("dispatch_eligibility", tenant_id, driver_id))
        if self._raises is not None:
            raise self._raises
        return {
            "driver_id": driver_id,
            "eligible": self._eligible,
            "reasons": self._reasons,
        }


class FakeHOSAdvisoryService:
    """A dict-shaped verdict — the seam is deliberately shape-agnostic."""

    def __init__(self, *, blocked=False, reason_code=None, calls=None):
        self._blocked = blocked
        self._reason_code = reason_code
        self.calls = calls if calls is not None else []

    async def gate_verdict(self, tenant_id, driver_id):
        self.calls.append(("hos", tenant_id, driver_id))
        return {
            "blocked": self._blocked,
            "reason_code": self._reason_code,
            "recorded_at": "2026-06-01T12:00:00+00:00",
        }


class _VerdictService:
    """Returns a full verdict, including the ``outcome`` the stack honours."""

    def __init__(
        self,
        *,
        outcome="passed",
        reason_code=None,
        freshness_state=None,
        override_id=None,
        audit_outcome=None,
        recorded_at="2026-06-01T12:00:00+00:00",
    ):
        self._verdict = {
            "outcome": outcome,
            "blocked": outcome == "blocked",
            "reason_code": reason_code,
            "freshness_state": freshness_state,
            "recorded_at": recorded_at,
            "override_id": override_id,
            "audit_outcome": audit_outcome,
        }

    async def gate_verdict(self, tenant_id, driver_id):
        return dict(self._verdict)


class FakeFeatureFlagService:
    def __init__(self, state="disabled", raises=None):
        self._state = state
        self._raises = raises
        self.reads = []

    async def get_overlay_state(self, flag_key, tenant_id):
        self.reads.append((flag_key, tenant_id))
        if self._raises is not None:
            raise self._raises
        return self._state


class FakeSchedulingWS:
    """The dispatcher channel — records every frame it was handed."""

    def __init__(self, *, raises=None):
        self._raises = raises
        self.broadcasts = []

    async def broadcast(self, event_type, event_data):
        self.broadcasts.append((event_type, event_data))
        if self._raises is not None:
            raise self._raises


def _stack(**kwargs):
    return DriverTransitionGateStack(**kwargs)


async def _evaluate(stack, target_status="in_transit", order=None):
    return await stack.evaluate(
        tenant_id=TENANT,
        driver_id=DRIVER,
        order=order if order is not None else dict(ORDER),
        target_status=target_status,
    )


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestGatedScope:
    """Only ``in_transit`` is gated (R8.6, R8.7, R17.30 all name it)."""

    def test_only_in_transit_is_gated(self):
        assert GATED_TARGET_STATUSES == frozenset({"in_transit"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["delivered", "failed", "cancelled"])
    async def test_ungated_targets_run_no_gate(self, target):
        """A completed delivery must always be recordable."""
        inspection = FakeInspectionService(out_of_service=True)
        qualification = FakeQualificationService(eligible=False)
        stack = _stack(
            inspection_service=inspection,
            driver_qualification_service=qualification,
        )

        evaluation = await _evaluate(stack, target_status=target)

        assert evaluation.gated is False
        assert evaluation.outcomes == ()
        assert inspection.calls == []
        assert qualification.calls == []


# ---------------------------------------------------------------------------
# Gate order
# ---------------------------------------------------------------------------


class TestGateOrder:
    """The first failing gate picks the response code, so order is contractual."""

    def test_declared_order(self):
        assert GATE_ORDER == (
            "asset_out_of_service",
            "pretrip_inspection",
            "dispatch_eligibility",
            "hos",
        )

    @pytest.mark.asyncio
    async def test_evaluation_records_gates_in_declared_order(self):
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(),
            feature_flag_service=FakeFeatureFlagService(state="active_gated"),
            hos_advisory_service=FakeHOSAdvisoryService(),
        )

        evaluation = await _evaluate(stack)

        assert tuple(o.gate for o in evaluation.outcomes) == GATE_ORDER
        assert evaluation.allowed is True

    @pytest.mark.asyncio
    async def test_out_of_service_precedes_dispatch_eligibility(self):
        """An out-of-service asset answers 409 ASSET_OUT_OF_SERVICE even when
        the driver is also ineligible — the earlier gate wins."""
        calls = []
        stack = _stack(
            inspection_service=FakeInspectionService(
                out_of_service=True, calls=calls
            ),
            driver_qualification_service=FakeQualificationService(
                eligible=False, calls=calls
            ),
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "ASSET_OUT_OF_SERVICE"
        assert [c[0] for c in calls] == ["asset_out_of_service"]

    @pytest.mark.asyncio
    async def test_pretrip_precedes_dispatch_eligibility(self):
        calls = []
        stack = _stack(
            inspection_service=FakeInspectionService(has_pretrip=False, calls=calls),
            driver_qualification_service=FakeQualificationService(
                eligible=False, calls=calls
            ),
            feature_flag_service=FakeFeatureFlagService(state="active_gated"),
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "PRETRIP_INSPECTION_REQUIRED"
        assert [c[0] for c in calls] == [
            "asset_out_of_service",
            "pretrip_inspection",
        ]


# ---------------------------------------------------------------------------
# Gate 1 — unconditional (R8.5, R8.6)
# ---------------------------------------------------------------------------


class TestOutOfServiceGateIsUnconditional:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "flag_state", ["disabled", "shadow", "active_gated", "active_auto"]
    )
    async def test_blocks_regardless_of_every_flag_state(self, flag_state):
        """R8.5/R8.6: no feature flag and no tenant policy value is consulted."""
        flags = FakeFeatureFlagService(state=flag_state)
        stack = _stack(
            inspection_service=FakeInspectionService(out_of_service=True),
            feature_flag_service=flags,
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "ASSET_OUT_OF_SERVICE"
        assert exc_info.value.status_code == 409
        # The gate reached its verdict before any flag was read.
        assert flags.reads == []

    @pytest.mark.asyncio
    async def test_blocks_with_no_feature_flag_service_at_all(self):
        stack = _stack(
            inspection_service=FakeInspectionService(out_of_service=True),
            feature_flag_service=None,
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "ASSET_OUT_OF_SERVICE"

    @pytest.mark.asyncio
    async def test_details_name_the_order_and_asset_only(self):
        stack = _stack(inspection_service=FakeInspectionService(out_of_service=True))

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.details == {"order_id": "ord-1", "asset_id": ASSET}

    @pytest.mark.asyncio
    async def test_passes_when_the_asset_is_in_service(self):
        stack = _stack(inspection_service=FakeInspectionService(out_of_service=False))

        evaluation = await _evaluate(stack)

        outcome = evaluation.outcomes[0]
        assert outcome.gate == "asset_out_of_service"
        assert outcome.outcome == "passed"

    @pytest.mark.asyncio
    async def test_order_with_no_assigned_asset_is_a_recorded_skip(self):
        inspection = FakeInspectionService(out_of_service=True)
        stack = _stack(inspection_service=inspection)

        evaluation = await _evaluate(
            stack, order={"order_id": "ord-1", "tenant_id": TENANT}
        )

        outcome = evaluation.outcomes[0]
        assert outcome.outcome == "skipped"
        assert outcome.reason_code == "NO_ASSIGNED_ASSET"
        assert inspection.calls == []

    @pytest.mark.asyncio
    async def test_absent_inspection_service_is_a_recorded_skip(self):
        """No Inspection_Service means no such state can exist to read."""
        stack = _stack(inspection_service=None)

        evaluation = await _evaluate(stack)

        outcome = evaluation.outcomes[0]
        assert outcome.outcome == "skipped"
        assert outcome.reason_code == "INSPECTION_SERVICE_UNAVAILABLE"
        assert evaluation.allowed is True


# ---------------------------------------------------------------------------
# Gate 2 — flag-gated, dormant in Phase 1 (R8.7, R8.12)
# ---------------------------------------------------------------------------


class TestPretripGateIsFlagGated:
    @pytest.mark.asyncio
    async def test_dormant_when_the_flag_is_disabled(self):
        """Phase 1: the seam is present and never fires."""
        inspection = FakeInspectionService(has_pretrip=False)
        flags = FakeFeatureFlagService(state="disabled")
        stack = _stack(inspection_service=inspection, feature_flag_service=flags)

        evaluation = await _evaluate(stack)

        outcome = evaluation.outcomes[1]
        assert outcome.outcome == "skipped"
        assert outcome.reason_code == "PRETRIP_FLAG_DISABLED"
        assert flags.reads == [(PRETRIP_FLAG_KEY, TENANT)]
        assert [c for c in inspection.calls if c[0] == "pretrip_inspection"] == []

    @pytest.mark.asyncio
    async def test_dormant_with_no_feature_flag_service(self):
        stack = _stack(
            inspection_service=FakeInspectionService(has_pretrip=False),
            feature_flag_service=None,
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[1].reason_code == "PRETRIP_FLAG_DISABLED"

    @pytest.mark.asyncio
    async def test_shadow_state_observes_without_blocking(self):
        stack = _stack(
            inspection_service=FakeInspectionService(has_pretrip=False),
            feature_flag_service=FakeFeatureFlagService(state="shadow"),
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[1].outcome == "skipped"

    @pytest.mark.asyncio
    async def test_flag_read_failure_falls_back_to_disabled(self):
        stack = _stack(
            inspection_service=FakeInspectionService(has_pretrip=False),
            feature_flag_service=FakeFeatureFlagService(
                raises=RuntimeError("Redis client not connected")
            ),
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[1].reason_code == "PRETRIP_FLAG_DISABLED"

    @pytest.mark.asyncio
    async def test_blocks_when_armed_and_no_inspection_exists(self):
        stack = _stack(
            inspection_service=FakeInspectionService(has_pretrip=False),
            feature_flag_service=FakeFeatureFlagService(state="active_gated"),
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "PRETRIP_INSPECTION_REQUIRED"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details["order_id"] == "ord-1"
        assert "inspection_local_date" in exc_info.value.details

    @pytest.mark.asyncio
    async def test_passes_when_armed_and_an_inspection_exists(self):
        stack = _stack(
            inspection_service=FakeInspectionService(has_pretrip=True),
            feature_flag_service=FakeFeatureFlagService(state="active_auto"),
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[1].outcome == "passed"


# ---------------------------------------------------------------------------
# Gate 3 — Dispatch_Eligibility (R17.30, R17.31)
# ---------------------------------------------------------------------------


class TestDispatchEligibilityGate:
    @pytest.mark.asyncio
    async def test_blocks_an_ineligible_driver(self):
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(
                eligible=False, reasons=["CDL expired on 2026-01-01"]
            ),
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "DRIVER_NOT_DISPATCH_ELIGIBLE"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details["reasons"] == ["CDL expired on 2026-01-01"]

    @pytest.mark.asyncio
    async def test_permits_an_eligible_driver(self):
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(eligible=True),
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[2].outcome == "passed"
        assert evaluation.allowed is True

    @pytest.mark.asyncio
    async def test_absent_qualification_service_is_a_recorded_skip(self):
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=None,
        )

        evaluation = await _evaluate(stack)

        outcome = evaluation.outcomes[2]
        assert outcome.outcome == "skipped"
        assert outcome.reason_code == "QUALIFICATION_SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_unresolvable_eligibility_is_a_skip_not_a_block(self):
        """A driver with no DQF record must not have every transition rejected."""
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(
                raises=RuntimeError("driver not found")
            ),
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[2].reason_code == "ELIGIBILITY_UNRESOLVED"
        assert evaluation.allowed is True

    @pytest.mark.asyncio
    async def test_combined_failure_answers_dispatch_eligible_with_hos_reason(self):
        """R17.31: deterministic response when both gates fail."""
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(
                eligible=False, reasons=["Medical card expired on 2026-02-02"]
            ),
            hos_advisory_service=FakeHOSAdvisoryService(
                blocked=True, reason_code="HOS_DRIVING_LIMIT"
            ),
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "DRIVER_NOT_DISPATCH_ELIGIBLE"
        assert exc_info.value.details["hos_reason_code"] == "HOS_DRIVING_LIMIT"

    @pytest.mark.asyncio
    async def test_eligibility_failure_alone_carries_no_hos_reason(self):
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(eligible=False),
            hos_advisory_service=FakeHOSAdvisoryService(blocked=False),
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert "hos_reason_code" not in exc_info.value.details


# ---------------------------------------------------------------------------
# Gate 4 — HOS, armed in Phase 2 (R17.17)
# ---------------------------------------------------------------------------


class TestHOSGate:
    @pytest.mark.asyncio
    async def test_unarmed_gate_is_a_recorded_skip(self):
        """``hos_advisory_service=None`` makes the gate a no-op, not a failure."""
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(),
            hos_advisory_service=None,
        )

        evaluation = await _evaluate(stack)

        outcome = evaluation.outcomes[3]
        assert outcome.outcome == "skipped"
        assert outcome.reason_code == "HOS_GATE_NOT_ARMED"
        assert evaluation.allowed is True

    @pytest.mark.asyncio
    async def test_blocks_when_armed_and_the_verdict_blocks(self):
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(eligible=True),
            hos_advisory_service=FakeHOSAdvisoryService(
                blocked=True, reason_code="HOS_DRIVING_LIMIT"
            ),
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "HOS_LIMIT_REACHED"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details["reason_code"] == "HOS_DRIVING_LIMIT"
        assert exc_info.value.details["recorded_at"] == "2026-06-01T12:00:00+00:00"

    @pytest.mark.asyncio
    async def test_verdict_failure_is_fail_open(self):
        class Broken:
            async def gate_verdict(self, tenant_id, driver_id):
                raise RuntimeError("telemetry unavailable")

        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(),
            hos_advisory_service=Broken(),
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[3].outcome == "skipped"
        assert evaluation.allowed is True

    @pytest.mark.asyncio
    async def test_a_skipped_verdict_is_recorded_as_skipped_not_passed(self):
        """R17.18 asks for the distinction, so the stack honours ``outcome``."""
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(),
            hos_advisory_service=_VerdictService(
                outcome="skipped",
                reason_code="HOS_READING_STALE",
                freshness_state="stale",
                audit_outcome="hos_gate_skipped",
            ),
        )

        evaluation = await _evaluate(stack)

        outcome = evaluation.outcomes[3]
        assert outcome.outcome == "skipped"
        assert outcome.reason_code == "HOS_READING_STALE"
        assert evaluation.allowed is True

    @pytest.mark.asyncio
    async def test_the_recorded_detail_is_the_audit_record(self):
        """Validates: Requirements 17.18, 17.26"""
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(),
            hos_advisory_service=_VerdictService(
                outcome="skipped",
                reason_code="HOS_READING_STALE",
                freshness_state="stale",
                audit_outcome="hos_gate_skipped",
            ),
        )

        evaluation = await _evaluate(stack)

        record = evaluation.hos_audit_record()
        assert record["driver_id"] == DRIVER
        assert record["outcome"] == "hos_gate_skipped"
        assert record["gate_outcome"] == "skipped"
        assert record["reason_code"] == "HOS_READING_STALE"
        assert record["freshness_state"] == "stale"
        assert record["recorded_at"] == "2026-06-01T12:00:00+00:00"

    @pytest.mark.asyncio
    async def test_an_override_identifier_reaches_the_audit_record(self):
        """Validates: Requirements 17.25, 17.26"""
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(),
            hos_advisory_service=_VerdictService(
                outcome="passed",
                reason_code="HOS_OVERRIDE_APPLIED",
                freshness_state="fresh",
                override_id="hgo_abc",
                audit_outcome="hos_gate_overridden",
            ),
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[3].outcome == "passed"
        assert evaluation.hos_audit_record()["override_id"] == "hgo_abc"

    @pytest.mark.asyncio
    async def test_gating_disabled_leaves_no_audit_record(self):
        """R17.19 — no gate at all, so nothing to record."""
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(),
            hos_advisory_service=_VerdictService(
                outcome="skipped", reason_code="HOS_GATING_DISABLED"
            ),
        )

        evaluation = await _evaluate(stack)

        assert evaluation.outcomes[3].reason_code == "HOS_GATING_DISABLED"
        assert evaluation.hos_audit_record() is None

    @pytest.mark.asyncio
    async def test_an_ungated_transition_has_no_audit_record(self):
        stack = _stack(
            inspection_service=FakeInspectionService(),
            hos_advisory_service=FakeHOSAdvisoryService(),
        )

        evaluation = await _evaluate(stack, target_status="delivered")

        assert evaluation.hos_audit_record() is None

    @pytest.mark.asyncio
    async def test_a_real_verdict_model_blocks_with_its_reason_code(self):
        """The real :class:`HOSGateVerdict`, not a dict, end to end."""
        from driver.services.hos_advisory_service import (
            HOS_AT_LIMIT,
            HOSGateVerdict,
        )

        class RealVerdictService:
            async def gate_verdict(self, tenant_id, driver_id):
                return HOSGateVerdict(
                    tenant_id=tenant_id,
                    driver_id=driver_id,
                    outcome="blocked",
                    blocked=True,
                    gating_enabled=True,
                    reason_code=HOS_AT_LIMIT,
                    freshness_state="fresh",
                    recorded_at="2026-06-01T12:00:00+00:00",
                    audit_outcome="hos_gate_blocked",
                )

        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(eligible=True),
            hos_advisory_service=RealVerdictService(),
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "HOS_LIMIT_REACHED"
        assert exc_info.value.details["reason_code"] == HOS_AT_LIMIT
        assert exc_info.value.details["recorded_at"] == "2026-06-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# The dispatcher-channel ``hos_block`` broadcast (R17.22)
# ---------------------------------------------------------------------------


def _blocking_stack(scheduling_ws, *, reason_code="HOS_DRIVING_LIMIT"):
    return _stack(
        inspection_service=FakeInspectionService(),
        driver_qualification_service=FakeQualificationService(eligible=True),
        hos_advisory_service=FakeHOSAdvisoryService(
            blocked=True, reason_code=reason_code
        ),
        scheduling_ws_manager=scheduling_ws,
    )


class TestHOSBlockBroadcast:
    """Validates: Requirements 17.22"""

    @pytest.mark.asyncio
    async def test_a_block_broadcasts_hos_block_with_order_driver_and_reason(self):
        ws = FakeSchedulingWS()
        stack = _blocking_stack(ws)

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "HOS_LIMIT_REACHED"
        assert len(ws.broadcasts) == 1
        event_type, event_data = ws.broadcasts[0]
        assert event_type == HOS_BLOCK_EVENT
        assert event_data["order_id"] == "ord-1"
        assert event_data["driver_id"] == DRIVER
        assert event_data["reason_code"] == "HOS_DRIVING_LIMIT"
        assert event_data["tenant_id"] == TENANT

    @pytest.mark.asyncio
    async def test_the_payload_names_no_other_driver(self):
        """R15.14 — the frame carries the blocked driver's identity and no other."""
        ws = FakeSchedulingWS()

        with pytest.raises(AppException):
            await _evaluate(_blocking_stack(ws))

        _, event_data = ws.broadcasts[0]
        assert set(event_data) == {
            "tenant_id",
            "order_id",
            "driver_id",
            "reason_code",
        }

    @pytest.mark.asyncio
    async def test_a_failed_broadcast_does_not_change_the_rejection(self):
        ws = FakeSchedulingWS(raises=RuntimeError("dispatcher channel down"))
        stack = _blocking_stack(ws)

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "HOS_LIMIT_REACHED"
        assert exc_info.value.status_code == 409
        assert len(ws.broadcasts) == 1

    @pytest.mark.asyncio
    async def test_no_dispatcher_channel_still_rejects(self):
        stack = _blocking_stack(None)

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "HOS_LIMIT_REACHED"

    @pytest.mark.asyncio
    async def test_a_permitted_transition_broadcasts_nothing(self):
        ws = FakeSchedulingWS()
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(eligible=True),
            hos_advisory_service=_VerdictService(
                outcome="skipped",
                reason_code="HOS_READING_STALE",
                freshness_state="stale",
                audit_outcome="hos_gate_skipped",
            ),
            scheduling_ws_manager=ws,
        )

        evaluation = await _evaluate(stack)

        assert evaluation.allowed is True
        assert ws.broadcasts == []

    @pytest.mark.asyncio
    async def test_a_combined_failure_broadcasts_nothing(self):
        """R17.31 answers ``DRIVER_NOT_DISPATCH_ELIGIBLE``, so R17.22 does not fire."""
        ws = FakeSchedulingWS()
        stack = _stack(
            inspection_service=FakeInspectionService(),
            driver_qualification_service=FakeQualificationService(
                eligible=False, reasons=["Medical card expired on 2026-02-02"]
            ),
            hos_advisory_service=FakeHOSAdvisoryService(
                blocked=True, reason_code="HOS_DRIVING_LIMIT"
            ),
            scheduling_ws_manager=ws,
        )

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "DRIVER_NOT_DISPATCH_ELIGIBLE"
        assert ws.broadcasts == []


# ---------------------------------------------------------------------------
# Input shapes
# ---------------------------------------------------------------------------


class TestOrderShapes:
    @pytest.mark.asyncio
    async def test_accepts_a_pydantic_style_model(self):
        class OrderModel:
            def model_dump(self, mode="python"):
                return dict(ORDER)

        stack = _stack(inspection_service=FakeInspectionService(out_of_service=True))

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack, order=OrderModel())

        assert exc_info.value.error_code == "ASSET_OUT_OF_SERVICE"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestConfigureTransitionEndpoints:
    def test_stores_collaborators_and_composes_the_stack(self):
        order_repository = object()
        order_service = object()
        qualification = FakeQualificationService()

        stack = configure_transition_endpoints(
            order_repository=order_repository,
            order_service=order_service,
            driver_qualification_service=qualification,
            inspection_service=None,
            feature_flag_service=None,
            hos_advisory_service=None,
        )

        assert stack is get_gate_stack()
        assert get_order_repository() is order_repository
        assert get_order_service() is order_service
        assert get_work_ref_resolver() is not None

    def test_omitted_arguments_are_reset_so_the_last_caller_wins(self):
        configure_transition_endpoints(
            order_repository=object(), order_service=object()
        )
        configure_transition_endpoints()

        assert get_order_repository() is None
        assert get_order_service() is None
        assert get_work_ref_resolver() is None
        assert get_gate_stack() is not None

    def test_threads_the_dispatcher_channel_onto_the_stack(self):
        """Validates: Requirements 17.22"""
        ws = FakeSchedulingWS()

        stack = configure_transition_endpoints(scheduling_ws_manager=ws)

        assert stack._scheduling_ws_manager is ws

        configure_transition_endpoints()

        assert get_gate_stack()._scheduling_ws_manager is None
