"""
Unit tests for the pre-trip existence read that arms the flag-gated gate —
``InspectionService.has_pretrip_inspection`` and the gate in
``driver/services/order_transition_service.py`` that calls it.

Requirement 8.7 is keyed on four things at once: the tenant, the acting driver,
the inspected asset, and the calendar day. So most of these tests vary exactly
one of the four and assert the answer flips — a read that matched on three of
them would let one driver's walk-around clear another driver's truck, or
yesterday's inspection clear today's trip.

The Elasticsearch stand-in is not a mock. It answers ``vehicle_inspections``
searches by genuinely applying the term filters in the query body against the
reports it was given, so the gate tests exercise the real path end to end: a
driver submits a report through ``submit`` and the gate finds that same document.

Validates: Requirements 8.7, 8.11, 8.12
- 8.7: where the tenant enables the requirement, a driver's first ``in_transit``
  of the day is rejected with 409 ``PRETRIP_INSPECTION_REQUIRED`` unless a
  ``pre_trip`` report exists for that driver, asset, and calendar day
- 8.11, 8.12: the read itself consults no feature flag — the single flag read
  lives in the gate — and the flag defaults to disabled
"""

from __future__ import annotations

from typing import Any

import pytest

from driver.services.driver_es_mappings import VEHICLE_INSPECTIONS_INDEX
from driver.services.inspection_service import (
    InspectionService,
    inspection_doc_id,
)
from driver.services.order_transition_service import (
    PRETRIP_FLAG_KEY,
    DriverTransitionGateStack,
)
from errors.exceptions import AppException

from tests.unit.test_inspection_out_of_service_effect import (
    ASSET,
    DRIVER,
    LOCAL_DATE,
    OTHER_TENANT,
    TENANT,
    TIMESTAMP,
    FakeES,
    RecordingFlagService,
)

OTHER_DRIVER = "drv_2"
OTHER_ASSET = "truck_9"
OTHER_DATE = "2026-04-30"
ORDER = {"order_id": "ord_1", "tenant_id": TENANT, "assigned_asset_id": ASSET}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(es: Any, *, flags: Any = None) -> InspectionService:
    return InspectionService(
        es_service=es,
        file_storage_service=None,
        feature_flag_service=flags,
        scheduling_ws_manager=None,
    )


async def _file_pretrip(
    service: InspectionService,
    *,
    tenant_id: str = TENANT,
    driver_id: str = DRIVER,
    asset_id: str = ASSET,
    local_date: str = LOCAL_DATE,
) -> dict:
    """File a clean pre-trip report through the real intake path."""
    return await service.submit(
        tenant_id,
        driver_id,
        asset_id=asset_id,
        odometer_miles=128450.5,
        inspection_timestamp=TIMESTAMP,
        inspection_local_date=local_date,
        defects=[],
    )


async def _store_post_trip(es: FakeES) -> None:
    """Put a ``post_trip`` report on the index directly.

    Phase 1 intake refuses ``post_trip``, and task 24.2 is what opens that path,
    so the document is written straight to the store here. The point under test
    is the ``inspection_type`` term filter, not the accept path.
    """
    await es.index_document(
        VEHICLE_INSPECTIONS_INDEX,
        inspection_doc_id(TENANT, "insp_post"),
        {
            "inspection_id": "insp_post",
            "tenant_id": TENANT,
            "driver_id": DRIVER,
            "asset_id": ASSET,
            "inspection_type": "post_trip",
            "odometer_miles": 128500.0,
            "inspection_timestamp": TIMESTAMP,
            "server_received_at": TIMESTAMP,
            "inspection_local_date": LOCAL_DATE,
            "defects": [],
            "has_out_of_service_defect": False,
            "expires_at": "2027-08-01T06:15:00+00:00",
        },
    )


# ---------------------------------------------------------------------------
# The four keys of R8.7
# ---------------------------------------------------------------------------


class TestPretripExistenceRead:
    @pytest.mark.asyncio
    async def test_finds_the_report_the_driver_filed_today(self):
        es = FakeES()
        service = _service(es)
        await _file_pretrip(service)

        assert (
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)
            is True
        )

    @pytest.mark.asyncio
    async def test_no_report_at_all_is_false(self):
        service = _service(FakeES())

        assert (
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)
            is False
        )

    @pytest.mark.asyncio
    async def test_yesterdays_report_does_not_clear_today(self):
        """The calendar-day key is what makes R8.7 a daily requirement."""
        es = FakeES()
        service = _service(es)
        await _file_pretrip(service, local_date=OTHER_DATE)

        assert (
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)
            is False
        )

    @pytest.mark.asyncio
    async def test_a_report_on_another_asset_does_not_clear_this_one(self):
        es = FakeES()
        service = _service(es)
        await _file_pretrip(service, asset_id=OTHER_ASSET)

        assert (
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)
            is False
        )

    @pytest.mark.asyncio
    async def test_another_drivers_report_does_not_clear_this_driver(self):
        es = FakeES()
        service = _service(es)
        await _file_pretrip(service, driver_id=OTHER_DRIVER)

        assert (
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)
            is False
        )

    @pytest.mark.asyncio
    async def test_another_tenants_report_does_not_clear_this_tenant(self):
        es = FakeES()
        service = _service(es)
        await _file_pretrip(service, tenant_id=OTHER_TENANT)

        assert (
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)
            is False
        )

    @pytest.mark.asyncio
    async def test_a_post_trip_report_does_not_satisfy_the_pretrip_requirement(self):
        es = FakeES()
        service = _service(es)
        await _store_post_trip(es)

        assert (
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)
            is False
        )

    @pytest.mark.asyncio
    async def test_the_read_consults_no_feature_flag(self):
        """R8.11: the single pre-trip flag read lives in the gate, not here."""
        es = FakeES()
        flags = RecordingFlagService(state="active_auto")
        service = _service(es, flags=flags)
        await _file_pretrip(service)

        await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)

        assert flags.reads == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tenant_id,driver_id,asset_id",
        [("", DRIVER, ASSET), (TENANT, "", ASSET), (TENANT, DRIVER, "")],
    )
    async def test_a_missing_key_is_false_rather_than_a_match_all_query(
        self, tenant_id, driver_id, asset_id
    ):
        es = FakeES()
        service = _service(es)
        await _file_pretrip(service)

        assert (
            await service.has_pretrip_inspection(
                tenant_id, driver_id, asset_id, LOCAL_DATE
            )
            is False
        )
        assert es.searched == []


# ---------------------------------------------------------------------------
# Failure postures
# ---------------------------------------------------------------------------


class TestPretripReadFailures:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_date", ["", None, "01-05-2026", "2026-13-01", "today"])
    async def test_an_unusable_calendar_day_is_rejected(self, bad_date):
        """An unparseable day must not become a filter that matches nothing."""
        es = FakeES()
        service = _service(es)

        with pytest.raises(AppException) as exc_info:
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, bad_date)

        assert exc_info.value.error_code == "INVALID_REQUEST"
        assert exc_info.value.status_code == 400
        assert es.searched == []

    @pytest.mark.asyncio
    async def test_an_unreadable_index_fails_closed(self):
        class BrokenES(FakeES):
            async def search_documents(self, index, query, size=100, **kwargs):
                raise RuntimeError("cluster unavailable")

        service = _service(BrokenES())

        with pytest.raises(AppException) as exc_info:
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)

        assert exc_info.value.error_code == "INTERNAL_ERROR"
        assert (
            exc_info.value.details["reason"] == "pretrip_inspection_state_unreadable"
        )

    @pytest.mark.asyncio
    async def test_a_leaked_foreign_document_is_dropped(self):
        """Second line of defence behind ``inject_tenant_filter``."""

        class LeakyES(FakeES):
            async def search_documents(self, index, query, size=100, **kwargs):
                self.searched.append((index, query))
                return {
                    "hits": {
                        "hits": [
                            {
                                "_id": "x",
                                "_source": {
                                    "inspection_id": "x",
                                    "tenant_id": OTHER_TENANT,
                                    "driver_id": DRIVER,
                                    "asset_id": ASSET,
                                    "inspection_type": "pre_trip",
                                    "inspection_local_date": LOCAL_DATE,
                                },
                            }
                        ]
                    }
                }

        service = _service(LeakyES())

        assert (
            await service.has_pretrip_inspection(TENANT, DRIVER, ASSET, LOCAL_DATE)
            is False
        )


# ---------------------------------------------------------------------------
# The armed gate, end to end
# ---------------------------------------------------------------------------


def _stack(service: InspectionService, *, state: str) -> DriverTransitionGateStack:
    return DriverTransitionGateStack(
        driver_qualification_service=None,
        inspection_service=service,
        feature_flag_service=RecordingFlagService(state=state),
        hos_advisory_service=None,
    )


async def _evaluate(stack: DriverTransitionGateStack):
    return await stack.evaluate(
        tenant_id=TENANT,
        driver_id=DRIVER,
        order=dict(ORDER),
        target_status="in_transit",
        local_date=LOCAL_DATE,
    )


class TestArmedGate:
    @pytest.mark.asyncio
    async def test_blocks_the_days_first_trip_with_no_report(self):
        service = _service(FakeES())
        stack = _stack(service, state="active_gated")

        with pytest.raises(AppException) as exc_info:
            await _evaluate(stack)

        assert exc_info.value.error_code == "PRETRIP_INSPECTION_REQUIRED"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details["asset_id"] == ASSET
        assert exc_info.value.details["inspection_local_date"] == LOCAL_DATE

    @pytest.mark.asyncio
    async def test_the_driver_files_a_report_and_the_trip_proceeds(self):
        es = FakeES()
        service = _service(es)
        stack = _stack(service, state="active_gated")

        with pytest.raises(AppException):
            await _evaluate(stack)

        await _file_pretrip(service)
        evaluation = await _evaluate(stack)

        pretrip = evaluation.outcomes[1]
        assert pretrip.gate == "pretrip_inspection"
        assert pretrip.outcome == "passed"
        assert evaluation.allowed is True

    @pytest.mark.asyncio
    async def test_the_gate_reads_the_flag_once_per_evaluation(self):
        """R8.11/R8.12: one read, of one key, defaulting to disabled."""
        flags = RecordingFlagService(state="disabled")
        stack = DriverTransitionGateStack(
            inspection_service=_service(FakeES()),
            feature_flag_service=flags,
        )

        evaluation = await stack.evaluate(
            tenant_id=TENANT,
            driver_id=DRIVER,
            order=dict(ORDER),
            target_status="in_transit",
            local_date=LOCAL_DATE,
        )

        assert flags.reads == [(PRETRIP_FLAG_KEY, TENANT)]
        assert evaluation.outcomes[1].reason_code == "PRETRIP_FLAG_DISABLED"
        assert evaluation.allowed is True
