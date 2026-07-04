"""
Property-based tests for Surface B driver endpoints.

# Feature: dinee-voice-integration, Property 20: Driver verification
# Feature: dinee-voice-integration, Property 21: Active assignment presence
# Feature: dinee-voice-integration, Property 22: Driver report submission and
# no-write-on-failure

These properties exercise the ``GET /drivers/verify``,
``GET /drivers/{id}/active-assignment`` and
``POST /drivers/{driverId}/assignments/{assignmentId}/reports`` handlers
implemented in ``fuel/voice/voice_read_driver_router.py`` (task 8.6).

Property 20 (**Validates: Requirements 19.1, 19.2, 19.3**):

    ``GET /drivers/verify`` returns ``pinVerified == True`` *and* a ``driver``
    object iff the presented identity resolves to a driver of the authenticated
    tenant, the presented phone matches, *and* the PIN is correct (Req 19.1);
    otherwise it returns ``pinVerified == False`` and omits the ``driver``
    object — a wrong PIN (Req 19.2) and a non-match (Req 19.3) are
    indistinguishable.

Property 21 (**Validates: Requirements 20.1, 20.2**):

    for a driver owned by the authenticated tenant,
    ``GET /drivers/{id}/active-assignment`` returns an ``assignment`` object iff
    the driver has an owned order whose status is in ``{dispatched, in_transit}``
    (Req 20.1); otherwise it returns ``{assignment: null}`` with HTTP 200
    (Req 20.2).

Property 22 (**Validates: Requirements 21.1, 21.2, 21.3, 21.5**):

    a report against an owned driver/assignment is persisted (storing the
    supplied ``detail``/``etaMinutes`` verbatim) and ``{recorded: true,
    reportId}`` is returned iff ``kind`` is one of
    ``delay``/``terminal_wait``/``exception``/``note`` (Req 21.1/21.3); a
    request with an absent or invalid ``kind`` is rejected with HTTP 422 and
    **nothing is persisted** (Req 21.2/21.5); an unknown assignment is rejected
    with HTTP 404 and **nothing is persisted** (Req 21.5). Persistence is
    asserted against a recording fake ``DriverReportRepository`` so the
    no-write-on-failure guarantee is directly observable.

The handlers are driven directly; the repositories/PIN vault are wired via
``configure_voice_read_driver_router`` with recording in-memory fakes that
mirror the real ES-backed contracts (tenant scoping, validate-before-write) so
no live Elasticsearch or vault is required.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import from_regex

from errors.exceptions import AppException
from fuel.driver_report_repository import DriverAssignmentNotFoundError
from fuel.order_models import Driver
from fuel.voice.voice_auth import VoiceTenantContext
from fuel.voice.voice_read_driver_router import (
    DriverReportRequest,
    _VALID_REPORT_KINDS,
    configure_voice_read_driver_router,
    get_active_assignment,
    submit_driver_report,
    verify_driver,
)


# ---------------------------------------------------------------------------
# Recording in-memory fakes
# ---------------------------------------------------------------------------
class FakeDriverRepository:
    """Tenant-scoped fake mirroring ``DriverRepository.get``.

    Returns only drivers owned by the ``tenant_id`` it is called with, so a
    cross-tenant / unknown driver degrades to ``None`` exactly like the real
    ES-backed repository.
    """

    def __init__(self, drivers_by_tenant: dict[str, dict[str, Driver]]) -> None:
        self.drivers_by_tenant = drivers_by_tenant

    async def get(self, tenant_id, driver_id):
        return self.drivers_by_tenant.get(tenant_id, {}).get(driver_id)


class FakeDriverPinVault:
    """Fake mirroring ``DriverPinVault.verify_pin`` (constant-time in reality).

    Verifies the presented PIN against the stored correct PIN for
    ``(tenant_id, driver_id)``; returns ``False`` when no PIN is on file so
    "unknown driver" and "wrong PIN" are indistinguishable (Req 19.2/19.3).
    """

    def __init__(self, pins_by_tenant: dict[str, dict[str, str]]) -> None:
        self.pins_by_tenant = pins_by_tenant

    async def verify_pin(self, tenant_id, driver_id, pin):
        correct = self.pins_by_tenant.get(tenant_id, {}).get(driver_id)
        return correct is not None and pin == correct


class FakeOrder:
    """Minimal order projection carrying only the attributes the active
    -assignment handler reads."""

    def __init__(
        self,
        order_id,
        status,
        assigned_run_id=None,
        delivery_window_start=None,
        delivery_window_end=None,
    ):
        self.order_id = order_id
        self.status = status
        self.assigned_run_id = assigned_run_id
        self.delivery_window_start = delivery_window_start
        self.delivery_window_end = delivery_window_end


class FakeFuelOrderRepository:
    """Fake mirroring ``FuelOrderRepository.search`` for the driver path.

    Returns the ``{"orders": [...]}`` envelope scoped to ``(tenant_id,
    driver_id)`` — anything else yields an empty list, matching the real
    repository's tenant scoping.
    """

    def __init__(self, orders_by_key: dict[tuple, list]) -> None:
        self.orders_by_key = orders_by_key

    async def search(self, tenant_id, *, driver_id=None, sort=None, **kwargs):
        return {"orders": list(self.orders_by_key.get((tenant_id, driver_id), []))}


class RecordingDriverReportRepository:
    """Recording fake mirroring ``DriverReportRepository.create``.

    ``create`` validates assignment ownership *before* any write, exactly like
    the real repository: an unknown ``(driver_id, assignment_id)`` raises
    :class:`DriverAssignmentNotFoundError` and persists nothing. Every attempt
    is recorded in ``create_calls`` while only successful writes land in
    ``persisted`` — so a test can assert the no-write-on-failure guarantee
    directly (Req 21.5).
    """

    def __init__(self, known_assignments: set[tuple[str, str]]) -> None:
        self.known_assignments = known_assignments
        self.create_calls: list[tuple] = []
        self.persisted: dict = {}

    async def create(self, tenant_id, report):
        self.create_calls.append((tenant_id, report))
        if (report.driver_id, report.assignment_id) not in self.known_assignments:
            raise DriverAssignmentNotFoundError(
                tenant_id=tenant_id,
                driver_id=report.driver_id,
                assignment_id=report.assignment_id,
            )
        self.persisted[report.report_id] = report
        return report


def _run(coro):
    return asyncio.run(coro)


def _ctx(tenant_id: str) -> VoiceTenantContext:
    return VoiceTenantContext(tenant_id=tenant_id, channel_id=f"chan-{tenant_id}")


def _make_driver(tenant_id: str, driver_id: str, phone: str) -> Driver:
    """Build a valid ``Driver`` model for the given identity."""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return Driver(
        driver_id=driver_id,
        tenant_id=tenant_id,
        driver_name="Test Driver",
        phone=phone,
        status="active",
        last_event_timestamp=now,
        source_schema_version="v1",
        trace_id="trace-1",
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_tenant_ids = from_regex(r"tenant-[a-z0-9]{6,12}", fullmatch=True)
_driver_ids = from_regex(r"drv-[a-z0-9]{6,12}", fullmatch=True)
_assignment_ids = from_regex(r"asn-[a-z0-9]{6,12}", fullmatch=True)
_phones = from_regex(r"\+1[0-9]{10}", fullmatch=True)
_pins = from_regex(r"[0-9]{4,6}", fullmatch=True)

_ORDER_STATUSES = [
    "placed", "confirmed", "scheduled", "dispatched",
    "in_transit", "delivered", "failed", "cancelled", "on_hold",
]
_ACTIVE_STATUSES = {"dispatched", "in_transit"}
_order_statuses = st.sampled_from(_ORDER_STATUSES)

_valid_kinds = st.sampled_from(list(_VALID_REPORT_KINDS))
_details = st.one_of(st.none(), st.text(min_size=1, max_size=40))
_etas = st.one_of(st.none(), st.integers(min_value=0, max_value=600))
# Any value that is NOT an accepted report kind — including the empty string and
# ``None`` (an absent kind).
_invalid_kinds = st.one_of(
    st.none(),
    st.just(""),
    st.text(min_size=1, max_size=20).filter(lambda s: s not in _VALID_REPORT_KINDS),
)


@st.composite
def _order_lists(draw):
    """A list of distinct-id orders with arbitrary statuses."""
    statuses = draw(st.lists(_order_statuses, min_size=0, max_size=6))
    return [
        FakeOrder(order_id=f"ord-{i}", status=status, assigned_run_id=f"run-{i}")
        for i, status in enumerate(statuses)
    ]


# ===========================================================================
# Property 20 — Driver verification
# ===========================================================================
class TestDriverVerification:
    """# Feature: dinee-voice-integration, Property 20: Driver verification

    **Validates: Requirements 19.1, 19.2, 19.3**
    """

    @given(
        tenant=_tenant_ids,
        driver_id=_driver_ids,
        phone=_phones,
        correct_pin=_pins,
        presented_pin=_pins,
        use_known_driver=st.booleans(),
        use_correct_phone=st.booleans(),
    )
    @settings(max_examples=100)
    def test_verify_true_iff_identity_and_pin_match(
        self,
        tenant,
        driver_id,
        phone,
        correct_pin,
        presented_pin,
        use_known_driver,
        use_correct_phone,
    ):
        async def scenario():
            driver = _make_driver(tenant, driver_id, phone)
            repo = FakeDriverRepository({tenant: {driver_id: driver}})
            vault = FakeDriverPinVault({tenant: {driver_id: correct_pin}})
            configure_voice_read_driver_router(
                driver_repository=repo, driver_pin_vault=vault
            )

            presented_identifier = driver_id if use_known_driver else "drv-unknown0"
            # phone + "0" is 11 trailing digits and can never equal the record.
            presented_phone = phone if use_correct_phone else phone + "0"

            resp = await verify_driver(
                voice=_ctx(tenant),
                phone=presented_phone,
                driverIdentifier=presented_identifier,
                pin=presented_pin,
            )

            expected_verified = (
                use_known_driver
                and use_correct_phone
                and presented_pin == correct_pin
            )
            assert resp.get("pinVerified") is expected_verified
            if expected_verified:
                # Correct PIN → driver object is present (Req 19.1).
                assert "driver" in resp
                assert resp["driver"]["driver_id"] == driver_id
            else:
                # Wrong PIN / non-match → the driver object is omitted
                # (Req 19.2/19.3), leaking nothing distinguishing the two.
                assert "driver" not in resp

        _run(scenario())


# ===========================================================================
# Property 21 — Active assignment presence
# ===========================================================================
class TestActiveAssignment:
    """# Feature: dinee-voice-integration, Property 21: Active assignment
    presence

    **Validates: Requirements 20.1, 20.2**
    """

    @given(
        tenant=_tenant_ids,
        driver_id=_driver_ids,
        orders=_order_lists(),
    )
    @settings(max_examples=100)
    def test_assignment_present_iff_active_order_exists(
        self, tenant, driver_id, orders
    ):
        async def scenario():
            driver = _make_driver(tenant, driver_id, "+15551230000")
            drepo = FakeDriverRepository({tenant: {driver_id: driver}})
            forepo = FakeFuelOrderRepository({(tenant, driver_id): orders})
            configure_voice_read_driver_router(
                driver_repository=drepo, fuel_order_repository=forepo
            )

            resp = await get_active_assignment(
                driver_id=driver_id, voice=_ctx(tenant)
            )

            first_active = next(
                (o for o in orders if o.status in _ACTIVE_STATUSES), None
            )
            if first_active is None:
                # No owned order in an active status → null, HTTP 200 (Req 20.2).
                assert resp == {"assignment": None}
            else:
                # Owned active order → the assignment is projected (Req 20.1).
                assert resp["assignment"] is not None
                assert resp["assignment"]["orderId"] == first_active.order_id
                assert resp["assignment"]["status"] == first_active.status
                assert resp["assignment"]["runId"] == first_active.assigned_run_id

        _run(scenario())


# ===========================================================================
# Property 22 — Driver report submission and no-write-on-failure
# ===========================================================================
class TestDriverReportSubmission:
    """# Feature: dinee-voice-integration, Property 22: Driver report submission
    and no-write-on-failure

    **Validates: Requirements 21.1, 21.2, 21.3, 21.5**
    """

    @given(
        tenant=_tenant_ids,
        driver_id=_driver_ids,
        assignment_id=_assignment_ids,
        kind=_valid_kinds,
        detail=_details,
        eta=_etas,
    )
    @settings(max_examples=100)
    def test_valid_report_is_recorded_and_persisted(
        self, tenant, driver_id, assignment_id, kind, detail, eta
    ):
        async def scenario():
            repo = RecordingDriverReportRepository({(driver_id, assignment_id)})
            configure_voice_read_driver_router(driver_report_repository=repo)

            body = DriverReportRequest(kind=kind, detail=detail, etaMinutes=eta)
            resp = await submit_driver_report(
                driver_id=driver_id,
                assignment_id=assignment_id,
                body=body,
                voice=_ctx(tenant),
            )

            # {recorded: true, reportId} on success (Req 21.1).
            assert resp["recorded"] is True
            assert resp["reportId"]

            # Exactly one report persisted, scoped to the tenant, storing the
            # supplied detail/etaMinutes verbatim (Req 21.3).
            assert len(repo.persisted) == 1
            stored = repo.persisted[resp["reportId"]]
            assert stored.tenant_id == tenant
            assert stored.driver_id == driver_id
            assert stored.assignment_id == assignment_id
            assert stored.kind == kind
            assert stored.detail == detail
            assert stored.eta_minutes == eta

        _run(scenario())

    @given(
        tenant=_tenant_ids,
        driver_id=_driver_ids,
        assignment_id=_assignment_ids,
        bad_kind=_invalid_kinds,
        detail=_details,
        eta=_etas,
    )
    @settings(max_examples=100)
    def test_invalid_or_absent_kind_is_422_and_persists_nothing(
        self, tenant, driver_id, assignment_id, bad_kind, detail, eta
    ):
        assume(bad_kind not in _VALID_REPORT_KINDS)

        async def scenario():
            repo = RecordingDriverReportRepository({(driver_id, assignment_id)})
            configure_voice_read_driver_router(driver_report_repository=repo)

            body = DriverReportRequest(kind=bad_kind, detail=detail, etaMinutes=eta)
            with pytest.raises(AppException) as exc_info:
                await submit_driver_report(
                    driver_id=driver_id,
                    assignment_id=assignment_id,
                    body=body,
                    voice=_ctx(tenant),
                )

            # Rejected with HTTP 422 (Req 21.2) ...
            assert exc_info.value.status_code == 422
            # ... before the repository is ever touched — nothing persisted
            # (Req 21.5).
            assert repo.create_calls == []
            assert repo.persisted == {}

        _run(scenario())

    @given(
        tenant=_tenant_ids,
        driver_id=_driver_ids,
        assignment_id=_assignment_ids,
        kind=_valid_kinds,
        detail=_details,
        eta=_etas,
    )
    @settings(max_examples=100)
    def test_unknown_assignment_is_404_and_persists_nothing(
        self, tenant, driver_id, assignment_id, kind, detail, eta
    ):
        async def scenario():
            # No known assignments — the ownership check fails closed.
            repo = RecordingDriverReportRepository(set())
            configure_voice_read_driver_router(driver_report_repository=repo)

            body = DriverReportRequest(kind=kind, detail=detail, etaMinutes=eta)
            with pytest.raises(AppException) as exc_info:
                await submit_driver_report(
                    driver_id=driver_id,
                    assignment_id=assignment_id,
                    body=body,
                    voice=_ctx(tenant),
                )

            # Uniform HTTP 404 (Req 21.4) and nothing persisted despite the
            # write being attempted (Req 21.5).
            assert exc_info.value.status_code == 404
            assert repo.persisted == {}
            assert len(repo.create_calls) == 1

        _run(scenario())
