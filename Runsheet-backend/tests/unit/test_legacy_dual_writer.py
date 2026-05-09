"""
Unit tests for ``fuel.services.legacy_dual_writer.LegacyDualWriter``.

Validates: Requirement 1.3.2.

Covers:
- mirror_order projects FuelOrder into legacy shipment shape
- mirror_order preserves ``legacy_origin_snapshot`` as legacy ``origin``
- mirror_order falls back to ``"depot"`` sentinel when snapshot is null
- mirror_order NEVER raises — failures log a warning, increment metric,
  and enqueue in ``pending_legacy_mirrors``
- mirror_driver projects Driver into legacy rider shape
- mirror_driver NEVER raises — same error handling as mirror_order
- Enqueue failure itself does not raise
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fuel.services.legacy_dual_writer import (
    LegacyDualWriter,
    PENDING_LEGACY_MIRRORS_INDEX,
    orders_legacy_mirror_errors_total,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fixed_clock():
    return datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _sample_order(
    legacy_origin_snapshot: str | None = None,
) -> Dict[str, Any]:
    return {
        "order_id": "ord_abc123",
        "tenant_id": "tenant_1",
        "status": "placed",
        "assigned_driver_id": "drv_001",
        "legacy_origin_snapshot": legacy_origin_snapshot,
        "ship_to_address": "123 Main St, Houston TX",
        "ship_to_lat": 29.7604,
        "ship_to_lon": -95.3698,
        "delivery_window_end": "2026-05-11T08:00:00+00:00",
        "last_event_timestamp": "2026-05-10T12:00:00+00:00",
        "source_schema_version": "1.0",
        "trace_id": "trace_xyz",
        "created_at": "2026-05-10T10:00:00+00:00",
        "updated_at": "2026-05-10T12:00:00+00:00",
    }


def _sample_driver() -> Dict[str, Any]:
    return {
        "driver_id": "drv_001",
        "tenant_id": "tenant_1",
        "driver_name": "John Smith",
        "status": "active",
        "availability": "available",
        "source_schema_version": "1.0",
        "trace_id": "trace_drv",
        "last_seen": "2026-05-10T11:00:00+00:00",
        "last_event_timestamp": "2026-05-10T12:00:00+00:00",
        "updated_at": "2026-05-10T12:00:00+00:00",
        "current_location": {"lat": 29.76, "lon": -95.37},
        "active_order_count": 3,
        "completed_today": 5,
    }


@pytest.fixture
def ops_es_service():
    svc = AsyncMock()
    svc.upsert_shipment_current = AsyncMock(return_value=True)
    svc.upsert_rider_current = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def es_service():
    svc = AsyncMock()
    svc.index_document = AsyncMock()
    return svc


@pytest.fixture
def writer(ops_es_service, es_service):
    return LegacyDualWriter(
        ops_es_service=ops_es_service,
        es_service=es_service,
        clock=_fixed_clock,
    )


# ---------------------------------------------------------------------------
# mirror_order — happy path
# ---------------------------------------------------------------------------


class TestMirrorOrderProjection:
    """mirror_order projects a FuelOrder into the legacy shipment shape."""

    @pytest.mark.asyncio
    async def test_projects_order_to_shipment_shape(
        self, writer, ops_es_service
    ):
        order = _sample_order(legacy_origin_snapshot="Terminal A")
        await writer.mirror_order(order)

        ops_es_service.upsert_shipment_current.assert_called_once()
        doc = ops_es_service.upsert_shipment_current.call_args[0][0]

        assert doc["shipment_id"] == "ord_abc123"
        assert doc["status"] == "placed"
        assert doc["tenant_id"] == "tenant_1"
        assert doc["rider_id"] == "drv_001"
        assert doc["origin"] == "Terminal A"
        assert doc["destination"] == "123 Main St, Houston TX"
        assert doc["estimated_delivery"] == "2026-05-11T08:00:00+00:00"
        assert doc["last_event_timestamp"] == "2026-05-10T12:00:00+00:00"
        assert doc["current_location"] == {"lat": 29.7604, "lon": -95.3698}
        assert doc["source_schema_version"] == "1.0"
        assert doc["trace_id"] == "trace_xyz"
        assert doc["created_at"] == "2026-05-10T10:00:00+00:00"
        assert doc["updated_at"] == "2026-05-10T12:00:00+00:00"
        assert doc["ingested_at"] == "2026-05-10T12:00:00+00:00"

    @pytest.mark.asyncio
    async def test_preserves_legacy_origin_snapshot(
        self, writer, ops_es_service
    ):
        """legacy_origin_snapshot is used as the legacy origin field."""
        order = _sample_order(legacy_origin_snapshot="Depot West")
        await writer.mirror_order(order)

        doc = ops_es_service.upsert_shipment_current.call_args[0][0]
        assert doc["origin"] == "Depot West"

    @pytest.mark.asyncio
    async def test_falls_back_to_depot_sentinel_when_snapshot_is_none(
        self, writer, ops_es_service
    ):
        """When legacy_origin_snapshot is null, origin falls back to 'depot'."""
        order = _sample_order(legacy_origin_snapshot=None)
        await writer.mirror_order(order)

        doc = ops_es_service.upsert_shipment_current.call_args[0][0]
        assert doc["origin"] == "depot"

    @pytest.mark.asyncio
    async def test_falls_back_to_depot_sentinel_when_snapshot_is_empty(
        self, writer, ops_es_service
    ):
        """When legacy_origin_snapshot is empty string, origin falls back to 'depot'."""
        order = _sample_order(legacy_origin_snapshot="")
        await writer.mirror_order(order)

        doc = ops_es_service.upsert_shipment_current.call_args[0][0]
        assert doc["origin"] == "depot"

    @pytest.mark.asyncio
    async def test_handles_missing_lat_lon(self, writer, ops_es_service):
        """When lat/lon are missing, current_location is None."""
        order = _sample_order()
        order.pop("ship_to_lat")
        order.pop("ship_to_lon")
        await writer.mirror_order(order)

        doc = ops_es_service.upsert_shipment_current.call_args[0][0]
        assert doc["current_location"] is None


# ---------------------------------------------------------------------------
# mirror_order — failure handling
# ---------------------------------------------------------------------------


class TestMirrorOrderFailureHandling:
    """mirror_order MUST never raise — failures log, count, and enqueue."""

    @pytest.mark.asyncio
    async def test_does_not_raise_on_upsert_failure(
        self, writer, ops_es_service, es_service, caplog
    ):
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES connection refused")
        )
        order = _sample_order()

        # Should NOT raise
        with caplog.at_level(logging.WARNING):
            await writer.mirror_order(order)

        # Warning was logged
        assert any(
            "Legacy shipment mirror failed" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_increments_error_metric_on_failure(
        self, writer, ops_es_service, es_service
    ):
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("timeout")
        )
        order = _sample_order()

        before = orders_legacy_mirror_errors_total.labels(
            tenant_id="tenant_1", entity_type="order"
        )._value.get()

        await writer.mirror_order(order)

        after = orders_legacy_mirror_errors_total.labels(
            tenant_id="tenant_1", entity_type="order"
        )._value.get()

        assert after == before + 1

    @pytest.mark.asyncio
    async def test_enqueues_in_pending_legacy_mirrors_on_failure(
        self, writer, ops_es_service, es_service
    ):
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )
        order = _sample_order()

        await writer.mirror_order(order)

        # Verify enqueue call
        es_service.index_document.assert_called_once()
        call_args = es_service.index_document.call_args[0]
        assert call_args[0] == PENDING_LEGACY_MIRRORS_INDEX
        doc = call_args[2]
        assert doc["entity_type"] == "order"
        assert doc["entity_id"] == "ord_abc123"
        assert doc["tenant_id"] == "tenant_1"
        assert doc["retry_count"] == 0
        assert "ES down" in doc["failure_reason"]

    @pytest.mark.asyncio
    async def test_does_not_raise_when_enqueue_also_fails(
        self, writer, ops_es_service, es_service, caplog
    ):
        """Even if the enqueue itself fails, mirror_order does not raise."""
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )
        es_service.index_document = AsyncMock(
            side_effect=RuntimeError("Enqueue also failed")
        )
        order = _sample_order()

        # Should NOT raise
        with caplog.at_level(logging.ERROR):
            await writer.mirror_order(order)

        assert any(
            "Failed to enqueue pending legacy mirror" in record.message
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# mirror_driver — happy path
# ---------------------------------------------------------------------------


class TestMirrorDriverProjection:
    """mirror_driver projects a Driver into the legacy rider shape."""

    @pytest.mark.asyncio
    async def test_projects_driver_to_rider_shape(
        self, writer, ops_es_service
    ):
        driver = _sample_driver()
        await writer.mirror_driver(driver)

        ops_es_service.upsert_rider_current.assert_called_once()
        doc = ops_es_service.upsert_rider_current.call_args[0][0]

        assert doc["rider_id"] == "drv_001"
        assert doc["status"] == "active"
        assert doc["tenant_id"] == "tenant_1"
        assert doc["availability"] == "available"
        assert doc["source_schema_version"] == "1.0"
        assert doc["trace_id"] == "trace_drv"
        assert doc["last_seen"] == "2026-05-10T11:00:00+00:00"
        assert doc["last_event_timestamp"] == "2026-05-10T12:00:00+00:00"
        assert doc["ingested_at"] == "2026-05-10T12:00:00+00:00"
        assert doc["current_location"] == {"lat": 29.76, "lon": -95.37}
        assert doc["active_shipment_count"] == 3
        assert doc["completed_today"] == 5
        assert doc["rider_name"] == "John Smith"


# ---------------------------------------------------------------------------
# mirror_driver — failure handling
# ---------------------------------------------------------------------------


class TestMirrorDriverFailureHandling:
    """mirror_driver MUST never raise — failures log, count, and enqueue."""

    @pytest.mark.asyncio
    async def test_does_not_raise_on_upsert_failure(
        self, writer, ops_es_service, es_service, caplog
    ):
        ops_es_service.upsert_rider_current = AsyncMock(
            side_effect=RuntimeError("ES connection refused")
        )
        driver = _sample_driver()

        with caplog.at_level(logging.WARNING):
            await writer.mirror_driver(driver)

        assert any(
            "Legacy rider mirror failed" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_increments_error_metric_on_driver_failure(
        self, writer, ops_es_service, es_service
    ):
        ops_es_service.upsert_rider_current = AsyncMock(
            side_effect=RuntimeError("timeout")
        )
        driver = _sample_driver()

        before = orders_legacy_mirror_errors_total.labels(
            tenant_id="tenant_1", entity_type="driver"
        )._value.get()

        await writer.mirror_driver(driver)

        after = orders_legacy_mirror_errors_total.labels(
            tenant_id="tenant_1", entity_type="driver"
        )._value.get()

        assert after == before + 1

    @pytest.mark.asyncio
    async def test_enqueues_driver_in_pending_legacy_mirrors_on_failure(
        self, writer, ops_es_service, es_service
    ):
        ops_es_service.upsert_rider_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )
        driver = _sample_driver()

        await writer.mirror_driver(driver)

        es_service.index_document.assert_called_once()
        call_args = es_service.index_document.call_args[0]
        assert call_args[0] == PENDING_LEGACY_MIRRORS_INDEX
        doc = call_args[2]
        assert doc["entity_type"] == "driver"
        assert doc["entity_id"] == "drv_001"
        assert doc["tenant_id"] == "tenant_1"
        assert doc["retry_count"] == 0

    @pytest.mark.asyncio
    async def test_does_not_raise_when_enqueue_also_fails(
        self, writer, ops_es_service, es_service, caplog
    ):
        ops_es_service.upsert_rider_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )
        es_service.index_document = AsyncMock(
            side_effect=RuntimeError("Enqueue also failed")
        )
        driver = _sample_driver()

        with caplog.at_level(logging.ERROR):
            await writer.mirror_driver(driver)

        assert any(
            "Failed to enqueue pending legacy mirror" in record.message
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# Tenant ID resolution
# ---------------------------------------------------------------------------


class TestTenantIdResolution:
    """tenant_id can be passed explicitly or derived from the entity."""

    @pytest.mark.asyncio
    async def test_uses_explicit_tenant_id_for_order(
        self, writer, ops_es_service, es_service
    ):
        """When tenant_id is passed explicitly, it's used for metrics."""
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("fail")
        )
        order = _sample_order()

        await writer.mirror_order(order, tenant_id="explicit_tenant")

        call_args = es_service.index_document.call_args[0]
        doc = call_args[2]
        assert doc["tenant_id"] == "explicit_tenant"

    @pytest.mark.asyncio
    async def test_uses_explicit_tenant_id_for_driver(
        self, writer, ops_es_service, es_service
    ):
        ops_es_service.upsert_rider_current = AsyncMock(
            side_effect=RuntimeError("fail")
        )
        driver = _sample_driver()

        await writer.mirror_driver(driver, tenant_id="explicit_tenant")

        call_args = es_service.index_document.call_args[0]
        doc = call_args[2]
        assert doc["tenant_id"] == "explicit_tenant"
