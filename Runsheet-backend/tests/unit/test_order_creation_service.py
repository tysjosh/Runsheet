"""
Unit tests for :class:`fuel.services.order_creation_service.OrderCreationService`.

Validates: Requirements 2.3, 2.4.

Covers:
    - Platform fields are stamped correctly (order_id, tenant_id, status,
      timestamps, trace_id).
    - Event documents receive matching platform fields.
    - Order is persisted via upsert_with_last_event_timestamp.
    - Events are appended via append_event.
    - WebSocket broadcast is called with order_placed.
    - Broadcast failure does NOT block the main path.
    - Legacy dual-write is called when overlay state is shadow/active_gated.
    - Legacy dual-write is skipped when overlay state is active_auto/disabled.
    - Legacy dual-write failure does NOT block the main path.
    - Missing legacy_dual_writer or feature_flag_service skips dual-write.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fuel.services.order_creation_service import OrderCreationService


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return FIXED_NOW


@dataclass
class FakeAdapterResult:
    """Mimics the IntakeResult returned by an IntakeAdapter."""

    order_doc: Dict[str, Any]
    event_docs: List[Dict[str, Any]] = field(default_factory=list)


def _sample_order_doc() -> Dict[str, Any]:
    """A minimal adapter output (business fields only)."""
    return {
        "customer_id": "cust_001",
        "customer_name": "Acme Fuel Co",
        "customer_phone": "+15551234567",
        "ship_to_address": "123 Main St, Houston TX",
        "ship_to_lat": 29.76,
        "ship_to_lon": -95.37,
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "fill_to_full": False,
        "call_type": "one_off",
        "delivery_window_start": "2026-05-11T08:00:00+00:00",
        "delivery_window_end": "2026-05-11T12:00:00+00:00",
        "intake_channel": "dispatcher",
        "intake_channel_id": "dispatcher",
        "intake_metadata": {"dispatcher_user_id": "user_42"},
        "source_schema_version": "1.0",
    }


def _sample_event_doc() -> Dict[str, Any]:
    """A minimal adapter-produced event (business fields only)."""
    return {
        "event_type": "order_placed",
        "event_payload": {"source": "dispatcher"},
    }


def _build_service(
    *,
    order_repo: Optional[Any] = None,
    ws_manager: Optional[Any] = None,
    legacy_dual_writer: Optional[Any] = None,
    feature_flag_service: Optional[Any] = None,
    clock=None,
) -> OrderCreationService:
    """Build an OrderCreationService with sensible test defaults."""
    return OrderCreationService(
        order_repo=order_repo or AsyncMock(),
        ws_manager=ws_manager or AsyncMock(),
        legacy_dual_writer=legacy_dual_writer,
        feature_flag_service=feature_flag_service,
        clock=clock or _fixed_clock,
    )


# ---------------------------------------------------------------------------
# Tests — Platform field stamping
# ---------------------------------------------------------------------------


class TestPlatformFieldStamping:
    """Verify that create_order stamps all platform-owned fields."""

    @pytest.mark.asyncio
    async def test_stamps_order_id(self):
        """order_id is minted with the ord_ prefix."""
        svc = _build_service()
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_123",
        )

        assert re.match(r"^ord_[0-9a-f]{32}$", result["order_id"])

    @pytest.mark.asyncio
    async def test_stamps_tenant_id(self):
        """tenant_id is set from the caller, not the adapter."""
        svc = _build_service()
        order_doc = _sample_order_doc()
        order_doc["tenant_id"] = "wrong_tenant"  # adapter tries to set it
        adapter_result = FakeAdapterResult(
            order_doc=order_doc,
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_123",
        )

        assert result["tenant_id"] == "tenant_A"

    @pytest.mark.asyncio
    async def test_stamps_status_placed(self):
        """status is always set to 'placed' regardless of adapter output."""
        svc = _build_service()
        order_doc = _sample_order_doc()
        order_doc["status"] = "delivered"  # adapter tries to set it
        adapter_result = FakeAdapterResult(
            order_doc=order_doc,
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_123",
        )

        assert result["status"] == "placed"

    @pytest.mark.asyncio
    async def test_stamps_timestamps(self):
        """created_at, updated_at, last_event_timestamp are set from clock."""
        svc = _build_service(clock=_fixed_clock)
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_123",
        )

        expected_ts = FIXED_NOW.isoformat()
        assert result["created_at"] == expected_ts
        assert result["updated_at"] == expected_ts
        assert result["last_event_timestamp"] == expected_ts

    @pytest.mark.asyncio
    async def test_stamps_trace_id(self):
        """trace_id is set from request_id."""
        svc = _build_service()
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="trace_abc",
        )

        assert result["trace_id"] == "trace_abc"

    @pytest.mark.asyncio
    async def test_overwrites_adapter_set_platform_fields(self):
        """Platform fields set by the adapter are overwritten."""
        svc = _build_service(clock=_fixed_clock)
        order_doc = _sample_order_doc()
        order_doc["order_id"] = "adapter_set_id"
        order_doc["trace_id"] = "adapter_trace"
        order_doc["created_at"] = "1999-01-01T00:00:00+00:00"
        adapter_result = FakeAdapterResult(
            order_doc=order_doc,
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="platform_trace",
        )

        assert result["order_id"] != "adapter_set_id"
        assert result["trace_id"] == "platform_trace"
        assert result["created_at"] == FIXED_NOW.isoformat()


# ---------------------------------------------------------------------------
# Tests — Event field stamping
# ---------------------------------------------------------------------------


class TestEventFieldStamping:
    """Verify that event documents receive platform-owned fields."""

    @pytest.mark.asyncio
    async def test_events_receive_event_id(self):
        """Each event gets a fresh evt_ prefixed ID."""
        repo = AsyncMock()
        svc = _build_service(order_repo=repo)
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc(), _sample_event_doc()],
        )

        await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        # append_event called twice
        assert repo.append_event.call_count == 2
        for call in repo.append_event.call_args_list:
            event = call[0][1]
            assert re.match(r"^evt_[0-9a-f]{32}$", event["event_id"])

    @pytest.mark.asyncio
    async def test_events_receive_order_id_and_tenant_id(self):
        """Events carry the parent order_id and tenant_id."""
        repo = AsyncMock()
        svc = _build_service(order_repo=repo)
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        event = repo.append_event.call_args[0][1]
        assert event["order_id"] == result["order_id"]
        assert event["tenant_id"] == "tenant_A"

    @pytest.mark.asyncio
    async def test_events_receive_timestamps_and_trace(self):
        """Events carry event_timestamp, ingested_at, trace_id."""
        repo = AsyncMock()
        svc = _build_service(order_repo=repo, clock=_fixed_clock)
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="trace_xyz",
        )

        event = repo.append_event.call_args[0][1]
        assert event["event_timestamp"] == FIXED_NOW.isoformat()
        assert event["ingested_at"] == FIXED_NOW.isoformat()
        assert event["trace_id"] == "trace_xyz"


# ---------------------------------------------------------------------------
# Tests — Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    """Verify that the order and events are persisted correctly."""

    @pytest.mark.asyncio
    async def test_upsert_called_with_tenant_and_order(self):
        """upsert_with_last_event_timestamp is called with the correct args."""
        repo = AsyncMock()
        svc = _build_service(order_repo=repo)
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        repo.upsert_with_last_event_timestamp.assert_called_once_with(
            "tenant_A", result
        )

    @pytest.mark.asyncio
    async def test_append_event_called_for_each_event(self):
        """append_event is called once per event document."""
        repo = AsyncMock()
        svc = _build_service(order_repo=repo)
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc(), _sample_event_doc(), _sample_event_doc()],
        )

        await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        assert repo.append_event.call_count == 3


# ---------------------------------------------------------------------------
# Tests — WebSocket broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    """Verify WebSocket broadcast behavior."""

    @pytest.mark.asyncio
    async def test_broadcast_called_with_order_placed(self):
        """ws_manager.broadcast is called with event_type=order_placed."""
        ws = AsyncMock()
        svc = _build_service(ws_manager=ws)
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        ws.broadcast.assert_called_once_with(
            event_type="order_placed",
            data=result,
            tenant_id="tenant_A",
        )

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_block(self):
        """A broadcast exception does not prevent create_order from returning."""
        ws = AsyncMock()
        ws.broadcast.side_effect = RuntimeError("WebSocket down")
        svc = _build_service(ws_manager=ws)
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        # Should not raise
        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        assert result["order_id"] is not None


# ---------------------------------------------------------------------------
# Tests — Legacy dual-write
# ---------------------------------------------------------------------------


class TestLegacyDualWrite:
    """Verify legacy dual-write behavior."""

    @pytest.mark.asyncio
    async def test_dual_write_called_when_shadow(self):
        """mirror_order is called when overlay state is 'shadow'."""
        dual_writer = AsyncMock()
        ff_service = AsyncMock()
        ff_service.get_overlay_state.return_value = "shadow"
        svc = _build_service(
            legacy_dual_writer=dual_writer,
            feature_flag_service=ff_service,
        )
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        dual_writer.mirror_order.assert_called_once_with(
            result, tenant_id="tenant_A"
        )

    @pytest.mark.asyncio
    async def test_dual_write_called_when_active_gated(self):
        """mirror_order is called when overlay state is 'active_gated'."""
        dual_writer = AsyncMock()
        ff_service = AsyncMock()
        ff_service.get_overlay_state.return_value = "active_gated"
        svc = _build_service(
            legacy_dual_writer=dual_writer,
            feature_flag_service=ff_service,
        )
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        dual_writer.mirror_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_dual_write_skipped_when_active_auto(self):
        """mirror_order is NOT called when overlay state is 'active_auto'."""
        dual_writer = AsyncMock()
        ff_service = AsyncMock()
        ff_service.get_overlay_state.return_value = "active_auto"
        svc = _build_service(
            legacy_dual_writer=dual_writer,
            feature_flag_service=ff_service,
        )
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        dual_writer.mirror_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_dual_write_skipped_when_disabled(self):
        """mirror_order is NOT called when overlay state is 'disabled'."""
        dual_writer = AsyncMock()
        ff_service = AsyncMock()
        ff_service.get_overlay_state.return_value = "disabled"
        svc = _build_service(
            legacy_dual_writer=dual_writer,
            feature_flag_service=ff_service,
        )
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        dual_writer.mirror_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_dual_write_skipped_when_no_writer(self):
        """No error when legacy_dual_writer is None."""
        ff_service = AsyncMock()
        ff_service.get_overlay_state.return_value = "shadow"
        svc = _build_service(
            legacy_dual_writer=None,
            feature_flag_service=ff_service,
        )
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        # Should not raise
        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        assert result["order_id"] is not None

    @pytest.mark.asyncio
    async def test_dual_write_skipped_when_no_feature_flag_service(self):
        """No error when feature_flag_service is None."""
        dual_writer = AsyncMock()
        svc = _build_service(
            legacy_dual_writer=dual_writer,
            feature_flag_service=None,
        )
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        dual_writer.mirror_order.assert_not_called()
        assert result["order_id"] is not None

    @pytest.mark.asyncio
    async def test_dual_write_failure_does_not_block(self):
        """A mirror_order exception does not prevent create_order from returning."""
        dual_writer = AsyncMock()
        dual_writer.mirror_order.side_effect = RuntimeError("ES down")
        ff_service = AsyncMock()
        ff_service.get_overlay_state.return_value = "shadow"
        svc = _build_service(
            legacy_dual_writer=dual_writer,
            feature_flag_service=ff_service,
        )
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        # Should not raise
        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        assert result["order_id"] is not None

    @pytest.mark.asyncio
    async def test_overlay_state_read_failure_does_not_block(self):
        """A feature_flag_service exception does not block creation."""
        dual_writer = AsyncMock()
        ff_service = AsyncMock()
        ff_service.get_overlay_state.side_effect = RuntimeError("Redis down")
        svc = _build_service(
            legacy_dual_writer=dual_writer,
            feature_flag_service=ff_service,
        )
        adapter_result = FakeAdapterResult(
            order_doc=_sample_order_doc(),
            event_docs=[_sample_event_doc()],
        )

        # Should not raise
        result = await svc.create_order(
            tenant_id="tenant_A",
            channel="dispatcher",
            adapter_result=adapter_result,
            request_id="req_1",
        )

        dual_writer.mirror_order.assert_not_called()
        assert result["order_id"] is not None
