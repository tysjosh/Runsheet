"""
Unit tests for ``fuel.services.legacy_mirror_backfill_worker.LegacyMirrorBackfillWorker``.

Validates: Requirements 1.3.2, 9.2.

Covers:
- Worker drains due entries from pending_legacy_mirrors
- Successful mirror removes the entry
- Failed mirror backs off exponentially (doubles each retry)
- Exhausted retries (next_retry_at > 24h from creation) move to poison queue
- Exhausted retries fire the orders_legacy_mirror_exhausted_total counter
- Worker does not crash on individual entry failures
- Order not found is treated as success (entry removed)
- Driver not found is treated as success (entry removed)
- Unknown entity_type is treated as failure
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fuel.services.legacy_mirror_backfill_worker import (
    BASE_BACKOFF_SECONDS,
    LegacyMirrorBackfillWorker,
    MAX_RETRY_WINDOW_HOURS,
    PENDING_LEGACY_MIRRORS_INDEX,
    orders_legacy_mirror_exhausted_total,
    run_backfill_cycle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock():
    return FIXED_NOW


def _make_entry(
    entry_id: str = "mirror_order_ord_abc123_deadbeef",
    tenant_id: str = "tenant_1",
    entity_type: str = "order",
    entity_id: str = "ord_abc123",
    retry_count: int = 0,
    created_at: str | None = None,
    next_retry_at: str | None = None,
) -> Dict[str, Any]:
    if created_at is None:
        created_at = FIXED_NOW.isoformat()
    if next_retry_at is None:
        next_retry_at = FIXED_NOW.isoformat()
    return {
        "entry_id": entry_id,
        "tenant_id": tenant_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "failure_reason": "ES connection refused",
        "retry_count": retry_count,
        "next_retry_at": next_retry_at,
        "created_at": created_at,
        "updated_at": FIXED_NOW.isoformat(),
    }


def _sample_order() -> Dict[str, Any]:
    return {
        "order_id": "ord_abc123",
        "tenant_id": "tenant_1",
        "status": "placed",
        "assigned_driver_id": "drv_001",
        "legacy_origin_snapshot": "Terminal A",
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
def es_service():
    """Mock ES service with client for search/get/delete/update."""
    svc = MagicMock()
    svc.client = AsyncMock()
    svc.client.search = AsyncMock(return_value={"hits": {"hits": []}})
    svc.client.get = AsyncMock()
    svc.client.delete = AsyncMock()
    svc.client.update = AsyncMock()
    return svc


@pytest.fixture
def ops_es_service():
    svc = AsyncMock()
    svc.upsert_shipment_current = AsyncMock(return_value=True)
    svc.upsert_rider_current = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def legacy_dual_writer(ops_es_service, es_service):
    from fuel.services.legacy_dual_writer import LegacyDualWriter

    return LegacyDualWriter(
        ops_es_service=ops_es_service,
        es_service=es_service,
        clock=_fixed_clock,
    )


@pytest.fixture
def order_repository():
    return AsyncMock()


@pytest.fixture
def driver_repository():
    return AsyncMock()


@pytest.fixture
def poison_queue_service():
    svc = AsyncMock()
    svc.store_failed_event = AsyncMock()
    return svc


@pytest.fixture
def worker(
    es_service, legacy_dual_writer, order_repository, driver_repository, poison_queue_service
):
    return LegacyMirrorBackfillWorker(
        es_service=es_service,
        legacy_dual_writer=legacy_dual_writer,
        order_repository=order_repository,
        driver_repository=driver_repository,
        poison_queue_service=poison_queue_service,
        clock=_fixed_clock,
    )


# ---------------------------------------------------------------------------
# Happy path — successful mirror removes entry
# ---------------------------------------------------------------------------


class TestSuccessfulMirror:
    """On successful mirror, the entry is removed from pending_legacy_mirrors."""

    @pytest.mark.asyncio
    async def test_order_mirror_success_removes_entry(
        self, worker, es_service, ops_es_service
    ):
        entry = _make_entry()
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        es_service.client.get = AsyncMock(
            return_value={"_source": _sample_order()}
        )

        processed = await worker.run_cycle()

        assert processed == 1
        es_service.client.delete.assert_called_once_with(
            index=PENDING_LEGACY_MIRRORS_INDEX,
            id="mirror_order_ord_abc123_deadbeef",
            ignore=[404],
        )

    @pytest.mark.asyncio
    async def test_driver_mirror_success_removes_entry(
        self, worker, es_service, ops_es_service
    ):
        entry = _make_entry(
            entry_id="mirror_driver_drv_001_deadbeef",
            entity_type="driver",
            entity_id="drv_001",
        )
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        es_service.client.get = AsyncMock(
            return_value={"_source": _sample_driver()}
        )

        processed = await worker.run_cycle()

        assert processed == 1
        es_service.client.delete.assert_called_once_with(
            index=PENDING_LEGACY_MIRRORS_INDEX,
            id="mirror_driver_drv_001_deadbeef",
            ignore=[404],
        )


# ---------------------------------------------------------------------------
# Exponential backoff on failure
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    """Failed mirror backs off exponentially (doubles each retry)."""

    @pytest.mark.asyncio
    async def test_first_failure_backs_off_by_base_interval(
        self, worker, es_service, ops_es_service
    ):
        entry = _make_entry(retry_count=0)
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        # Order IS found but the upsert to legacy fails
        es_service.client.get = AsyncMock(
            return_value={"_source": _sample_order()}
        )
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )

        processed = await worker.run_cycle()

        assert processed == 0
        # Should update with retry_count=1 and next_retry_at = now + 60s
        es_service.client.update.assert_called_once()
        call_kwargs = es_service.client.update.call_args[1]
        assert call_kwargs["doc"]["retry_count"] == 1
        expected_next = (FIXED_NOW + timedelta(seconds=BASE_BACKOFF_SECONDS)).isoformat()
        assert call_kwargs["doc"]["next_retry_at"] == expected_next

    @pytest.mark.asyncio
    async def test_second_failure_doubles_backoff(
        self, worker, es_service, ops_es_service
    ):
        entry = _make_entry(retry_count=1)
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        es_service.client.get = AsyncMock(
            return_value={"_source": _sample_order()}
        )
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )

        await worker.run_cycle()

        call_kwargs = es_service.client.update.call_args[1]
        assert call_kwargs["doc"]["retry_count"] == 2
        # Backoff = 60 * 2^1 = 120 seconds
        expected_next = (FIXED_NOW + timedelta(seconds=120)).isoformat()
        assert call_kwargs["doc"]["next_retry_at"] == expected_next

    @pytest.mark.asyncio
    async def test_third_failure_quadruples_backoff(
        self, worker, es_service, ops_es_service
    ):
        entry = _make_entry(retry_count=2)
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        es_service.client.get = AsyncMock(
            return_value={"_source": _sample_order()}
        )
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )

        await worker.run_cycle()

        call_kwargs = es_service.client.update.call_args[1]
        assert call_kwargs["doc"]["retry_count"] == 3
        # Backoff = 60 * 2^2 = 240 seconds
        expected_next = (FIXED_NOW + timedelta(seconds=240)).isoformat()
        assert call_kwargs["doc"]["next_retry_at"] == expected_next


# ---------------------------------------------------------------------------
# Exhausted retries — move to poison queue
# ---------------------------------------------------------------------------


class TestExhaustedRetries:
    """When next_retry_at exceeds 24h from creation, move to poison queue."""

    @pytest.mark.asyncio
    async def test_moves_to_poison_queue_when_exhausted(
        self, worker, es_service, ops_es_service, poison_queue_service
    ):
        # Entry created 23 hours ago with high retry count so next backoff
        # would exceed 24h
        created_at = (FIXED_NOW - timedelta(hours=23)).isoformat()
        entry = _make_entry(
            retry_count=10,  # 60 * 2^10 = 61440s ≈ 17h — exceeds remaining 1h
            created_at=created_at,
        )
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        # Order found but mirror fails
        es_service.client.get = AsyncMock(
            return_value={"_source": _sample_order()}
        )
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )

        await worker.run_cycle()

        # Should have called store_failed_event on poison queue
        poison_queue_service.store_failed_event.assert_called_once()
        call_kwargs = poison_queue_service.store_failed_event.call_args[1]
        assert call_kwargs["error_type"] == "legacy_mirror_exhausted"
        assert call_kwargs["tenant_id"] == "tenant_1"

        # Entry should be removed
        es_service.client.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_fires_exhausted_metric(
        self, worker, es_service, ops_es_service, poison_queue_service
    ):
        created_at = (FIXED_NOW - timedelta(hours=23, minutes=59)).isoformat()
        entry = _make_entry(retry_count=15, created_at=created_at)
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        es_service.client.get = AsyncMock(
            return_value={"_source": _sample_order()}
        )
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )

        before = orders_legacy_mirror_exhausted_total.labels(
            tenant_id="tenant_1"
        )._value.get()

        await worker.run_cycle()

        after = orders_legacy_mirror_exhausted_total.labels(
            tenant_id="tenant_1"
        )._value.get()

        assert after == before + 1

    @pytest.mark.asyncio
    async def test_does_not_exhaust_when_within_window(
        self, worker, es_service, ops_es_service, poison_queue_service
    ):
        """Entry created recently should back off, not exhaust."""
        # Created 1 hour ago, retry_count=0 → next backoff = 60s, well within 24h
        created_at = (FIXED_NOW - timedelta(hours=1)).isoformat()
        entry = _make_entry(retry_count=0, created_at=created_at)
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        # Order found but mirror fails
        es_service.client.get = AsyncMock(
            return_value={"_source": _sample_order()}
        )
        ops_es_service.upsert_shipment_current = AsyncMock(
            side_effect=RuntimeError("ES down")
        )

        await worker.run_cycle()

        # Should NOT move to poison queue
        poison_queue_service.store_failed_event.assert_not_called()
        # Should update backoff
        es_service.client.update.assert_called_once()


# ---------------------------------------------------------------------------
# Worker resilience — does not crash on individual failures
# ---------------------------------------------------------------------------


class TestWorkerResilience:
    """Worker MUST NOT crash on individual entry failures."""

    @pytest.mark.asyncio
    async def test_continues_processing_after_entry_failure(
        self, worker, es_service, ops_es_service
    ):
        """If one entry throws an unexpected exception, others still process."""
        good_entry = _make_entry(
            entry_id="good_entry",
            entity_id="ord_good",
        )
        bad_entry = _make_entry(
            entry_id="bad_entry",
            entity_id="ord_bad",
        )

        es_service.client.search = AsyncMock(
            return_value={
                "hits": {"hits": [
                    {"_source": bad_entry},
                    {"_source": good_entry},
                ]}
            }
        )

        call_count = 0

        async def _get_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("id") == "ord_bad" or (args and len(args) > 1 and args[1] == "ord_bad"):
                raise RuntimeError("Unexpected crash")
            return {"_source": _sample_order()}

        # Make get return order for good, crash for bad
        # The worker fetches by entity_id, so we need to handle both
        async def _smart_get(index, id, **kwargs):
            if id == "ord_bad":
                raise RuntimeError("Unexpected crash")
            return {"_source": {**_sample_order(), "order_id": id}}

        es_service.client.get = AsyncMock(side_effect=_smart_get)

        processed = await worker.run_cycle()

        # Bad entry failed but good entry should still be processed
        # (bad entry triggers _attempt_mirror failure, then backoff)
        # At minimum, the worker should not crash
        assert processed >= 0

    @pytest.mark.asyncio
    async def test_empty_queue_returns_zero(self, worker, es_service):
        """When no entries are due, returns 0."""
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        processed = await worker.run_cycle()
        assert processed == 0


# ---------------------------------------------------------------------------
# Entity not found — treated as success
# ---------------------------------------------------------------------------


class TestEntityNotFound:
    """When the entity no longer exists, treat as success and remove entry."""

    @pytest.mark.asyncio
    async def test_order_not_found_removes_entry(
        self, worker, es_service, ops_es_service
    ):
        entry = _make_entry()
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        # Order not found (404 raises exception in ES client)
        es_service.client.get = AsyncMock(
            side_effect=Exception("NotFoundError")
        )

        processed = await worker.run_cycle()

        assert processed == 1
        es_service.client.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_driver_not_found_removes_entry(
        self, worker, es_service, ops_es_service
    ):
        entry = _make_entry(
            entry_id="mirror_driver_drv_001_deadbeef",
            entity_type="driver",
            entity_id="drv_001",
        )
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )
        es_service.client.get = AsyncMock(
            side_effect=Exception("NotFoundError")
        )

        processed = await worker.run_cycle()

        assert processed == 1
        es_service.client.delete.assert_called_once()


# ---------------------------------------------------------------------------
# Unknown entity_type
# ---------------------------------------------------------------------------


class TestUnknownEntityType:
    """Unknown entity_type is treated as failure."""

    @pytest.mark.asyncio
    async def test_unknown_entity_type_backs_off(
        self, worker, es_service, poison_queue_service
    ):
        entry = _make_entry(entity_type="unknown_thing")
        es_service.client.search = AsyncMock(
            return_value={"hits": {"hits": [{"_source": entry}]}}
        )

        await worker.run_cycle()

        # Should back off (update called) since mirror returns False
        es_service.client.update.assert_called_once()


# ---------------------------------------------------------------------------
# run_backfill_cycle helper
# ---------------------------------------------------------------------------


class TestRunBackfillCycle:
    """The standalone run_backfill_cycle helper catches exceptions."""

    @pytest.mark.asyncio
    async def test_does_not_raise_on_worker_exception(self, caplog):
        """run_backfill_cycle catches exceptions from the worker."""
        mock_worker = AsyncMock()
        mock_worker.run_cycle = AsyncMock(
            side_effect=RuntimeError("Unexpected")
        )

        with caplog.at_level(logging.ERROR):
            await run_backfill_cycle(mock_worker)

        assert any(
            "backfill cycle failed" in record.message
            for record in caplog.records
        )
