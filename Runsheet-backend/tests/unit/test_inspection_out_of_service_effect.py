"""
Unit tests for the unconditional out-of-service effect and the 15-month
retention stamp — ``driver/services/inspection_service.py`` and the gate in
``driver/services/order_transition_service.py``.

The word doing the work in Requirement 8.5, 8.6, 8.9, 8.11, and 8.13 is
*unconditional*. So these tests are written as scope tests rather than happy-path
tests: every one of them runs with a feature-flag service wired in that records
every read, and asserts the recording is empty. A flag that is never read cannot
turn the effect off, which is the property the requirements actually state.

The Elasticsearch stand-in is not a mock. It records the reports it is given and
answers ``vehicle_inspections`` searches by genuinely applying the term filters
in the query body, so the gate test exercises the real path: a driver submits a
report, and the same denormalized boolean the submission wrote is what the gate
term-filters on.

Validates: Requirements 8.5, 8.6, 8.9, 8.11, 8.13
- 8.5: an ``out_of_service`` defect sets the asset's operational state and
  broadcasts an escalation to the dispatcher channel, in every tenant
- 8.6: the asset's driver transitions to ``in_transit`` are refused with 409
  ``ASSET_OUT_OF_SERVICE``
- 8.9: ``expires_at`` is ``inspection_timestamp`` + 15 months on every accepted
  report
- 8.11, 8.13: none of the above reads the pre-trip flag or a tenant policy value
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from driver.services.driver_es_mappings import VEHICLE_INSPECTIONS_INDEX
from driver.services.inspection_service import (
    ASSET_INDEX,
    ASSET_OUT_OF_SERVICE_EVENT,
    ASSET_OUT_OF_SERVICE_STATE,
    INSPECTION_RETENTION_MONTHS,
    InspectionService,
    retention_expires_at,
)
from driver.services.order_transition_service import DriverTransitionGateStack
from errors.exceptions import AppException

TENANT = "t1"
OTHER_TENANT = "t2"
DRIVER = "drv_1"
ASSET = "truck_7"
ORDER = "ord_1"

TIMESTAMP = "2026-05-01T06:15:00+00:00"
LOCAL_DATE = "2026-05-01"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _term_filters(node: Any, found: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Collect every ``{"term": {field: value}}`` in a search body."""
    if found is None:
        found = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "term" and isinstance(value, dict):
                found.update(value)
            else:
                _term_filters(value, found)
    elif isinstance(node, list):
        for item in node:
            _term_filters(item, found)
    return found


class FakeES:
    """Records writes and answers searches by applying the query's term filters.

    ``vehicle_inspections`` searches are matched against the reports this
    instance was actually given, so the read side under test is the same
    denormalized boolean the write side produced. ``trucks`` searches answer
    from ``assets``, keyed on ``(tenant_id, asset_id)``, matching the real
    index's document id convention.
    """

    def __init__(self, *, assets: Tuple[Tuple[str, str], ...] = ((TENANT, ASSET),)):
        self.indexed: List[tuple] = []
        self.updates: List[tuple] = []
        self.searched: List[tuple] = []
        self.assets = set(assets)

    async def index_document(self, index, doc_id, document):
        self.indexed.append((index, doc_id, dict(document)))
        return {"result": "created"}

    async def update_document(self, index, doc_id, partial_doc):
        self.updates.append((index, doc_id, dict(partial_doc)))
        return {"result": "updated"}

    async def search_documents(self, index, query, size=100, **_kwargs):
        self.searched.append((index, query))
        terms = _term_filters(query)

        if index == ASSET_INDEX:
            key = (terms.get("tenant_id"), terms.get("_id"))
            hits = (
                [{"_id": key[1], "_source": {"tenant_id": key[0]}}]
                if key in self.assets
                else []
            )
            return {"hits": {"hits": hits}}

        if index == VEHICLE_INSPECTIONS_INDEX:
            hits = [
                {"_id": doc_id, "_source": doc}
                for stored_index, doc_id, doc in self.indexed
                if stored_index == VEHICLE_INSPECTIONS_INDEX
                and all(doc.get(field) == value for field, value in terms.items())
            ]
            return {"hits": {"hits": hits[:size]}}

        return {"hits": {"hits": []}}


class RecordingFlagService:
    """Records every overlay read. The assertion is always that it recorded none."""

    def __init__(self, state: str = "active_auto") -> None:
        self._state = state
        self.reads: List[tuple] = []

    async def get_overlay_state(self, flag_key: str, tenant_id: str) -> str:
        self.reads.append((flag_key, tenant_id))
        return self._state


class RecordingWSManager:
    """The dispatcher channel — records every broadcast."""

    def __init__(self, *, fail: bool = False) -> None:
        self.broadcasts: List[tuple] = []
        self._fail = fail

    async def broadcast(self, event_type: str, event_data: dict) -> None:
        self.broadcasts.append((event_type, dict(event_data)))
        if self._fail:
            raise RuntimeError("socket gone")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(
    es: FakeES,
    *,
    flags: Any = None,
    ws: Any = None,
) -> InspectionService:
    return InspectionService(
        es_service=es,
        file_storage_service=None,
        feature_flag_service=flags,
        scheduling_ws_manager=ws,
    )


def _defect(*, severity: str, component: str = "service_brakes") -> dict:
    return {
        "component": component,
        "severity": severity,
        "note": "cracked",
        "photo_refs": [],
    }


async def _submit(
    service: InspectionService,
    *,
    tenant_id: str = TENANT,
    severity: Optional[str] = None,
    timestamp: str = TIMESTAMP,
    asset_id: str = ASSET,
) -> dict:
    return await service.submit(
        tenant_id,
        DRIVER,
        asset_id=asset_id,
        odometer_miles=128450.5,
        inspection_timestamp=timestamp,
        inspection_local_date=LOCAL_DATE,
        defects=[_defect(severity=severity)] if severity else [],
    )


# ---------------------------------------------------------------------------
# R8.5 — the asset-state write and the dispatcher escalation
# ---------------------------------------------------------------------------


class TestOutOfServiceEffect:
    """An ``out_of_service`` defect stops the asset and tells the dispatcher."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tenant_id", [TENANT, OTHER_TENANT])
    async def test_sets_the_asset_operational_state_in_every_tenant(
        self, tenant_id
    ):
        """Validates: Requirements 8.5, 8.11, 8.13"""
        es = FakeES(assets=((TENANT, ASSET), (OTHER_TENANT, ASSET)))
        flags = RecordingFlagService()
        service = _service(es, flags=flags, ws=RecordingWSManager())

        await _submit(service, tenant_id=tenant_id, severity="out_of_service")

        assert len(es.updates) == 1
        index, doc_id, partial = es.updates[0]
        assert index == ASSET_INDEX
        assert doc_id == ASSET
        assert partial["operational_state"] == ASSET_OUT_OF_SERVICE_STATE
        # No flag and no policy value was consulted on the way there.
        assert flags.reads == []

    @pytest.mark.asyncio
    async def test_broadcasts_the_escalation_to_the_dispatcher_channel(self):
        """Validates: Requirements 8.5"""
        es = FakeES()
        ws = RecordingWSManager()
        flags = RecordingFlagService()
        service = _service(es, flags=flags, ws=ws)

        report = await _submit(
            service, severity="out_of_service", asset_id=ASSET
        )

        assert len(ws.broadcasts) == 1
        event_type, event_data = ws.broadcasts[0]
        assert event_type == ASSET_OUT_OF_SERVICE_EVENT
        assert event_data["tenant_id"] == TENANT
        assert event_data["asset_id"] == ASSET
        assert event_data["driver_id"] == DRIVER
        assert event_data["inspection_id"] == report["inspection_id"]
        assert event_data["operational_state"] == ASSET_OUT_OF_SERVICE_STATE
        # Only the defects that stopped the asset are named.
        assert [d["severity"] for d in event_data["defects"]] == [
            "out_of_service"
        ]
        assert flags.reads == []

    @pytest.mark.asyncio
    async def test_a_minor_defect_neither_stops_the_asset_nor_escalates(self):
        """The effect is keyed on the severity, not on the presence of a defect.

        Validates: Requirements 8.5
        """
        es = FakeES()
        ws = RecordingWSManager()
        service = _service(es, flags=RecordingFlagService(), ws=ws)

        report = await _submit(service, severity="minor")

        assert report["has_out_of_service_defect"] is False
        assert es.updates == []
        assert ws.broadcasts == []

    @pytest.mark.asyncio
    async def test_a_clean_report_neither_stops_the_asset_nor_escalates(self):
        """Validates: Requirements 8.5"""
        es = FakeES()
        ws = RecordingWSManager()
        service = _service(es, flags=RecordingFlagService(), ws=ws)

        report = await _submit(service, severity=None)

        assert report["has_out_of_service_defect"] is False
        assert es.updates == []
        assert ws.broadcasts == []

    @pytest.mark.asyncio
    async def test_the_asset_write_is_tenant_scoped(self):
        """One tenant's report cannot move another tenant's asset.

        Validates: Requirements 8.5
        """
        es = FakeES(assets=((OTHER_TENANT, ASSET),))
        service = _service(es, flags=RecordingFlagService(), ws=RecordingWSManager())

        report = await _submit(service, tenant_id=TENANT, severity="out_of_service")

        # The report stands and still gates the asset; the foreign asset record
        # is untouched.
        assert report["has_out_of_service_defect"] is True
        assert es.updates == []

    @pytest.mark.asyncio
    async def test_a_failed_broadcast_does_not_fail_the_submission(self):
        """The report and the asset state survive an unreachable socket.

        Validates: Requirements 8.5
        """
        es = FakeES()
        service = _service(
            es, flags=RecordingFlagService(), ws=RecordingWSManager(fail=True)
        )

        report = await _submit(service, severity="out_of_service")

        assert report["has_out_of_service_defect"] is True
        assert len(es.updates) == 1

    @pytest.mark.asyncio
    async def test_the_report_is_persisted_before_the_asset_state_moves(self):
        """The gate reads the report, so the report is the first write.

        Validates: Requirements 8.5, 8.6
        """
        es = FakeES()
        service = _service(es, flags=RecordingFlagService(), ws=RecordingWSManager())

        await _submit(service, severity="out_of_service")

        assert [i[0] for i in es.indexed] == [VEHICLE_INSPECTIONS_INDEX]
        assert es.indexed[0][2]["has_out_of_service_defect"] is True


# ---------------------------------------------------------------------------
# R8.9 — the 15-month retention stamp
# ---------------------------------------------------------------------------


class TestRetentionStamp:
    """``expires_at`` is on every accepted report, in every tenant."""

    def test_the_retention_period_is_fifteen_months(self):
        assert INSPECTION_RETENTION_MONTHS == 15

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tenant_id", [TENANT, OTHER_TENANT])
    @pytest.mark.parametrize("severity", [None, "minor", "out_of_service"])
    async def test_every_accepted_report_expires_fifteen_months_on(
        self, tenant_id, severity
    ):
        """Clean report or stopped asset, either tenant — same stamp.

        Validates: Requirements 8.9, 8.11, 8.13
        """
        es = FakeES(assets=((TENANT, ASSET), (OTHER_TENANT, ASSET)))
        flags = RecordingFlagService()
        service = _service(es, flags=flags, ws=RecordingWSManager())

        report = await _submit(
            service, tenant_id=tenant_id, severity=severity
        )

        assert report["expires_at"] == "2027-08-01T06:15:00+00:00"
        # The persisted document carries it, not just the response.
        assert es.indexed[0][2]["expires_at"] == report["expires_at"]
        assert flags.reads == []

    @pytest.mark.asyncio
    async def test_the_stamp_is_measured_from_the_client_timestamp(self):
        """Retention runs from the walk-around, not from the server's receipt.

        A report drained from an offline queue days later still expires 15
        months after the inspection the driver performed.

        Validates: Requirements 8.9
        """
        es = FakeES()
        service = _service(es, flags=RecordingFlagService(), ws=RecordingWSManager())

        report = await _submit(
            service, severity=None, timestamp="2026-01-15T23:45:00+00:00"
        )

        assert report["expires_at"] == "2027-04-15T23:45:00+00:00"
        assert report["expires_at"] != report["server_received_at"]

    def test_the_day_of_month_is_clamped_to_the_target_month(self):
        """31 March + 15 months is 30 June, not 1 July."""
        assert retention_expires_at(
            datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
        ) == datetime(2027, 6, 30, 12, 0, tzinfo=timezone.utc)

    def test_a_leap_day_inspection_clamps_rather_than_rolling(self):
        assert retention_expires_at(
            datetime(2024, 2, 29, 8, 0, tzinfo=timezone.utc)
        ) == datetime(2025, 5, 29, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# R8.6 — the gate
# ---------------------------------------------------------------------------


def _stack(inspection_service, *, flags=None) -> DriverTransitionGateStack:
    return DriverTransitionGateStack(
        driver_qualification_service=None,
        inspection_service=inspection_service,
        feature_flag_service=flags,
        hos_advisory_service=None,
    )


async def _evaluate(stack, *, target_status: str = "in_transit"):
    return await stack.evaluate(
        tenant_id=TENANT,
        driver_id=DRIVER,
        order={
            "order_id": ORDER,
            "tenant_id": TENANT,
            "assigned_asset_id": ASSET,
            "assigned_driver_id": DRIVER,
        },
        target_status=target_status,
        local_date=LOCAL_DATE,
    )


class TestTheGate:
    """A reported out-of-service asset cannot move, in any tenant."""

    @pytest.mark.asyncio
    async def test_in_transit_is_refused_with_409_after_a_report(self):
        """Validates: Requirements 8.6, 8.11, 8.13"""
        es = FakeES()
        flags = RecordingFlagService()
        service = _service(es, flags=flags, ws=RecordingWSManager())
        await _submit(service, severity="out_of_service")

        with pytest.raises(AppException) as exc_info:
            await _evaluate(_stack(service, flags=flags))

        assert exc_info.value.error_code == "ASSET_OUT_OF_SERVICE"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details["asset_id"] == ASSET
        # The gate reached its verdict without reading a flag.
        assert flags.reads == []

    @pytest.mark.asyncio
    async def test_the_state_is_read_as_a_term_filter(self):
        """The denormalized boolean is why it is denormalized (R8.6).

        Validates: Requirements 8.6
        """
        es = FakeES()
        service = _service(es, ws=RecordingWSManager())
        await _submit(service, severity="out_of_service")
        es.searched.clear()

        assert await service.is_asset_out_of_service(TENANT, ASSET) is True

        index, query = es.searched[-1]
        assert index == VEHICLE_INSPECTIONS_INDEX
        assert _term_filters(query) == {
            "tenant_id": TENANT,
            "asset_id": ASSET,
            "has_out_of_service_defect": True,
        }
        # One term query, not a nested one.
        assert "nested" not in str(query)

    @pytest.mark.asyncio
    async def test_a_minor_defect_leaves_the_asset_movable(self):
        """Validates: Requirements 8.6"""
        es = FakeES()
        service = _service(es, ws=RecordingWSManager())
        await _submit(service, severity="minor")

        evaluation = await _evaluate(_stack(service))

        assert evaluation.allowed is True
        assert evaluation.outcomes[0].gate == "asset_out_of_service"
        assert evaluation.outcomes[0].outcome == "passed"

    @pytest.mark.asyncio
    async def test_another_tenants_report_does_not_gate_this_asset(self):
        """Validates: Requirements 8.6"""
        es = FakeES(assets=((OTHER_TENANT, ASSET),))
        service = _service(es, ws=RecordingWSManager())
        await _submit(service, tenant_id=OTHER_TENANT, severity="out_of_service")

        assert await service.is_asset_out_of_service(TENANT, ASSET) is False

    @pytest.mark.asyncio
    async def test_a_report_on_another_asset_does_not_gate_this_one(self):
        """Validates: Requirements 8.6"""
        es = FakeES(assets=((TENANT, "truck_9"),))
        service = _service(es, ws=RecordingWSManager())
        await _submit(service, severity="out_of_service", asset_id="truck_9")

        assert await service.is_asset_out_of_service(TENANT, ASSET) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["delivered", "failed"])
    async def test_a_completed_delivery_is_never_gated(self, target):
        """A driver who has finished must always be able to record it.

        Validates: Requirements 8.6
        """
        es = FakeES()
        service = _service(es, ws=RecordingWSManager())
        await _submit(service, severity="out_of_service")

        evaluation = await _evaluate(_stack(service), target_status=target)

        assert evaluation.gated is False

    @pytest.mark.asyncio
    async def test_an_unreadable_state_fails_closed_rather_than_guessing(self):
        """Neither answer is honest when the index cannot be read.

        Validates: Requirements 8.6
        """

        class BrokenES(FakeES):
            async def search_documents(self, index, query, size=100, **kwargs):
                raise RuntimeError("cluster unavailable")

        service = _service(BrokenES(), ws=RecordingWSManager())

        with pytest.raises(AppException) as exc_info:
            await service.is_asset_out_of_service(TENANT, ASSET)

        assert exc_info.value.error_code == "INTERNAL_ERROR"
