"""Integration tests for /ws/commerce/invoices WebSocket channel.

Validates:
- Cross-tenant isolation: tenant A's invoice updates are NOT received by tenant B
- Event ordering: updates arrive in the order they were broadcast
- Connection lifecycle: connect, receive updates, disconnect

Uses the CommerceInvoiceWSManager directly (no HTTP layer) to test the
broadcast filtering logic in isolation from auth concerns.
"""

from __future__ import annotations

import asyncio
import json
import pytest
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from commerce.websocket.commerce_ws import (
    CommerceInvoiceWSManager,
    get_commerce_invoice_ws_manager,
)


# ---------------------------------------------------------------------------
# Helpers — fake WebSocket for testing
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Minimal WebSocket stub that records sent messages."""

    def __init__(self, tenant_id: str = ""):
        self.tenant_id = tenant_id
        self.sent_messages: List[Dict[str, Any]] = []
        self.accepted = False
        self.closed = False
        self.close_code: Optional[int] = None
        self.close_reason: Optional[str] = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent_messages.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def receive_text(self) -> str:
        # Block forever (simulates waiting for client messages)
        await asyncio.sleep(999999)
        return ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_manager():
    """Fresh CommerceInvoiceWSManager for each test."""
    return CommerceInvoiceWSManager()


@pytest.fixture
def tenant_a_invoice():
    """Sample invoice doc for tenant A."""
    return {
        "invoice_id": "inv_001",
        "tenant_id": "tenant-a",
        "customer_id": "cust_001",
        "account_id": "acc_001",
        "status": "open",
        "total_cents": 50000,
        "amount_paid_cents": 0,
        "remaining_cents": 50000,
    }


@pytest.fixture
def tenant_b_invoice():
    """Sample invoice doc for tenant B."""
    return {
        "invoice_id": "inv_002",
        "tenant_id": "tenant-b",
        "customer_id": "cust_002",
        "account_id": "acc_002",
        "status": "open",
        "total_cents": 75000,
        "amount_paid_cents": 0,
        "remaining_cents": 75000,
    }


# ---------------------------------------------------------------------------
# Tests — Cross-tenant isolation
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    """Prove /ws/commerce/invoices broadcasts never cross tenant boundaries."""

    @pytest.mark.asyncio
    async def test_tenant_a_update_not_received_by_tenant_b(
        self, ws_manager, tenant_a_invoice
    ):
        """Tenant A's invoice update must NOT be received by tenant B's connection."""
        ws_a = FakeWebSocket(tenant_id="tenant-a")
        ws_b = FakeWebSocket(tenant_id="tenant-b")

        await ws_manager.connect(ws_a, tenant_id="tenant-a")
        await ws_manager.connect(ws_b, tenant_id="tenant-b")

        # Broadcast tenant A's invoice
        count = await ws_manager.broadcast_invoice_update(tenant_a_invoice)

        # Only tenant A should receive it
        assert count == 1

        # ws_a gets the connection confirmation + the invoice update
        invoice_messages_a = [
            m for m in ws_a.sent_messages if m.get("type") == "invoice_updated"
        ]
        assert len(invoice_messages_a) == 1
        assert invoice_messages_a[0]["data"]["invoice_id"] == "inv_001"
        assert invoice_messages_a[0]["tenant_id"] == "tenant-a"

        # ws_b only gets the connection confirmation, no invoice update
        invoice_messages_b = [
            m for m in ws_b.sent_messages if m.get("type") == "invoice_updated"
        ]
        assert len(invoice_messages_b) == 0

    @pytest.mark.asyncio
    async def test_tenant_b_update_not_received_by_tenant_a(
        self, ws_manager, tenant_b_invoice
    ):
        """Tenant B's invoice update must NOT be received by tenant A's connection."""
        ws_a = FakeWebSocket(tenant_id="tenant-a")
        ws_b = FakeWebSocket(tenant_id="tenant-b")

        await ws_manager.connect(ws_a, tenant_id="tenant-a")
        await ws_manager.connect(ws_b, tenant_id="tenant-b")

        # Broadcast tenant B's invoice
        count = await ws_manager.broadcast_invoice_update(tenant_b_invoice)

        assert count == 1

        # ws_a should NOT receive tenant B's update
        invoice_messages_a = [
            m for m in ws_a.sent_messages if m.get("type") == "invoice_updated"
        ]
        assert len(invoice_messages_a) == 0

        # ws_b should receive it
        invoice_messages_b = [
            m for m in ws_b.sent_messages if m.get("type") == "invoice_updated"
        ]
        assert len(invoice_messages_b) == 1
        assert invoice_messages_b[0]["data"]["invoice_id"] == "inv_002"

    @pytest.mark.asyncio
    async def test_multiple_clients_same_tenant_all_receive(
        self, ws_manager, tenant_a_invoice
    ):
        """All clients for the same tenant receive the broadcast."""
        ws_1 = FakeWebSocket(tenant_id="tenant-a")
        ws_2 = FakeWebSocket(tenant_id="tenant-a")
        ws_3 = FakeWebSocket(tenant_id="tenant-a")

        await ws_manager.connect(ws_1, tenant_id="tenant-a")
        await ws_manager.connect(ws_2, tenant_id="tenant-a")
        await ws_manager.connect(ws_3, tenant_id="tenant-a")

        count = await ws_manager.broadcast_invoice_update(tenant_a_invoice)

        assert count == 3

        for ws in [ws_1, ws_2, ws_3]:
            invoice_messages = [
                m for m in ws.sent_messages if m.get("type") == "invoice_updated"
            ]
            assert len(invoice_messages) == 1
            assert invoice_messages[0]["data"]["invoice_id"] == "inv_001"

    @pytest.mark.asyncio
    async def test_broadcast_requires_tenant_id(self, ws_manager):
        """Broadcast raises ValueError if invoice_doc has no tenant_id."""
        with pytest.raises(ValueError, match="require tenant_id"):
            await ws_manager.broadcast_invoice_update({"invoice_id": "inv_x"})

    @pytest.mark.asyncio
    async def test_no_clients_for_tenant_returns_zero(
        self, ws_manager, tenant_a_invoice
    ):
        """Broadcast returns 0 when no clients are connected for the tenant."""
        # Connect only tenant B
        ws_b = FakeWebSocket(tenant_id="tenant-b")
        await ws_manager.connect(ws_b, tenant_id="tenant-b")

        # Broadcast for tenant A — no clients
        count = await ws_manager.broadcast_invoice_update(tenant_a_invoice)
        assert count == 0


# ---------------------------------------------------------------------------
# Tests — Event ordering
# ---------------------------------------------------------------------------


class TestEventOrdering:
    """Verify updates arrive in the order they were broadcast."""

    @pytest.mark.asyncio
    async def test_updates_arrive_in_broadcast_order(self, ws_manager):
        """Multiple sequential broadcasts arrive in the same order."""
        ws = FakeWebSocket(tenant_id="tenant-a")
        await ws_manager.connect(ws, tenant_id="tenant-a")

        # Broadcast a sequence of state transitions
        states = ["draft", "open", "partial", "paid"]
        for i, status in enumerate(states):
            invoice_doc = {
                "invoice_id": "inv_order_test",
                "tenant_id": "tenant-a",
                "status": status,
                "sequence": i,
            }
            await ws_manager.broadcast_invoice_update(invoice_doc)

        # Extract invoice_updated messages (skip connection confirmation)
        invoice_messages = [
            m for m in ws.sent_messages if m.get("type") == "invoice_updated"
        ]

        assert len(invoice_messages) == 4

        # Verify ordering
        for i, msg in enumerate(invoice_messages):
            assert msg["data"]["status"] == states[i]
            assert msg["data"]["sequence"] == i

    @pytest.mark.asyncio
    async def test_interleaved_tenant_broadcasts_maintain_per_tenant_order(
        self, ws_manager
    ):
        """Interleaved broadcasts for different tenants maintain correct
        per-tenant ordering."""
        ws_a = FakeWebSocket(tenant_id="tenant-a")
        ws_b = FakeWebSocket(tenant_id="tenant-b")

        await ws_manager.connect(ws_a, tenant_id="tenant-a")
        await ws_manager.connect(ws_b, tenant_id="tenant-b")

        # Interleave broadcasts: A1, B1, A2, B2, A3
        broadcasts = [
            {"invoice_id": "inv_a1", "tenant_id": "tenant-a", "seq": 1},
            {"invoice_id": "inv_b1", "tenant_id": "tenant-b", "seq": 1},
            {"invoice_id": "inv_a2", "tenant_id": "tenant-a", "seq": 2},
            {"invoice_id": "inv_b2", "tenant_id": "tenant-b", "seq": 2},
            {"invoice_id": "inv_a3", "tenant_id": "tenant-a", "seq": 3},
        ]

        for doc in broadcasts:
            await ws_manager.broadcast_invoice_update(doc)

        # Tenant A should see 3 messages in order
        msgs_a = [
            m for m in ws_a.sent_messages if m.get("type") == "invoice_updated"
        ]
        assert len(msgs_a) == 3
        assert [m["data"]["seq"] for m in msgs_a] == [1, 2, 3]

        # Tenant B should see 2 messages in order
        msgs_b = [
            m for m in ws_b.sent_messages if m.get("type") == "invoice_updated"
        ]
        assert len(msgs_b) == 2
        assert [m["data"]["seq"] for m in msgs_b] == [1, 2]


# ---------------------------------------------------------------------------
# Tests — Connection lifecycle
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    """Verify connect, receive updates, and disconnect behavior."""

    @pytest.mark.asyncio
    async def test_connect_sends_confirmation(self, ws_manager):
        """On connect, client receives a connection confirmation message."""
        ws = FakeWebSocket(tenant_id="tenant-a")
        await ws_manager.connect(ws, tenant_id="tenant-a")

        assert ws.accepted is True
        assert len(ws.sent_messages) == 1

        confirm = ws.sent_messages[0]
        assert confirm["type"] == "connection"
        assert confirm["status"] == "connected"
        assert confirm["manager"] == "commerce_invoices"
        assert confirm["channel"] == "/ws/commerce/invoices"
        assert "timestamp" in confirm

    @pytest.mark.asyncio
    async def test_disconnect_removes_client(self, ws_manager):
        """After disconnect, client no longer receives broadcasts."""
        ws = FakeWebSocket(tenant_id="tenant-a")
        await ws_manager.connect(ws, tenant_id="tenant-a")

        # Disconnect
        await ws_manager.disconnect(ws)

        # Broadcast should reach 0 clients
        invoice_doc = {
            "invoice_id": "inv_after_disconnect",
            "tenant_id": "tenant-a",
            "status": "open",
        }
        count = await ws_manager.broadcast_invoice_update(invoice_doc)
        assert count == 0

        # Client should only have the connection confirmation
        invoice_messages = [
            m for m in ws.sent_messages if m.get("type") == "invoice_updated"
        ]
        assert len(invoice_messages) == 0

    @pytest.mark.asyncio
    async def test_active_connections_count(self, ws_manager):
        """active_connections reflects current state."""
        assert ws_manager.active_connections == 0

        ws_1 = FakeWebSocket(tenant_id="tenant-a")
        ws_2 = FakeWebSocket(tenant_id="tenant-b")

        await ws_manager.connect(ws_1, tenant_id="tenant-a")
        assert ws_manager.active_connections == 1

        await ws_manager.connect(ws_2, tenant_id="tenant-b")
        assert ws_manager.active_connections == 2

        await ws_manager.disconnect(ws_1)
        assert ws_manager.active_connections == 1

        await ws_manager.disconnect(ws_2)
        assert ws_manager.active_connections == 0

    @pytest.mark.asyncio
    async def test_dead_client_cleaned_up_on_broadcast(self, ws_manager):
        """A client that fails to receive is removed during broadcast."""

        class FailingWebSocket(FakeWebSocket):
            async def send_json(self, data: dict) -> None:
                raise ConnectionError("Client gone")

        ws_good = FakeWebSocket(tenant_id="tenant-a")
        ws_dead = FailingWebSocket(tenant_id="tenant-a")

        await ws_manager.connect(ws_good, tenant_id="tenant-a")
        await ws_manager.connect(ws_dead, tenant_id="tenant-a")

        assert ws_manager.active_connections == 2

        invoice_doc = {
            "invoice_id": "inv_dead_test",
            "tenant_id": "tenant-a",
            "status": "open",
        }
        count = await ws_manager.broadcast_invoice_update(invoice_doc)

        # Only the good client received it
        assert count == 1
        # Dead client was removed
        assert ws_manager.active_connections == 1

    @pytest.mark.asyncio
    async def test_ping_pong_handling(self, ws_manager):
        """Client ping message receives a pong response."""
        ws = FakeWebSocket(tenant_id="tenant-a")
        await ws_manager.connect(ws, tenant_id="tenant-a")

        # Simulate client sending a ping
        await ws_manager.handle_client_message(ws, json.dumps({"type": "ping"}))

        # Should have connection confirmation + pong
        pong_messages = [m for m in ws.sent_messages if m.get("type") == "pong"]
        assert len(pong_messages) == 1
        assert "timestamp" in pong_messages[0]

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_connections(self, ws_manager):
        """Shutdown closes all connected clients."""
        ws_1 = FakeWebSocket(tenant_id="tenant-a")
        ws_2 = FakeWebSocket(tenant_id="tenant-b")

        await ws_manager.connect(ws_1, tenant_id="tenant-a")
        await ws_manager.connect(ws_2, tenant_id="tenant-b")

        assert ws_manager.active_connections == 2

        await ws_manager.shutdown()

        assert ws_manager.active_connections == 0


# ---------------------------------------------------------------------------
# Tests — InvoiceService WS integration
# ---------------------------------------------------------------------------


class TestInvoiceServiceWSBroadcast:
    """Verify InvoiceService hooks broadcast on state transitions."""

    @pytest.mark.asyncio
    async def test_finalize_draft_broadcasts(self):
        """finalize_draft broadcasts the updated invoice projection."""
        from commerce.services.invoice_service import InvoiceService

        ws_manager = CommerceInvoiceWSManager()
        ws = FakeWebSocket(tenant_id="tenant-a")
        await ws_manager.connect(ws, tenant_id="tenant-a")

        # Mock ES service
        es_mock = AsyncMock()
        es_mock.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "invoice_id": "inv_fin",
                                "tenant_id": "tenant-a",
                                "status": "draft",
                                "total_cents": 10000,
                                "amount_paid_cents": 0,
                                "remaining_cents": 10000,
                                "account_id": "acc_1",
                            }
                        }
                    ]
                },
                "aggregations": {"max_seq": {"value": 1}},
            }
        )
        es_mock.index_document = AsyncMock()
        es_mock.update_document = AsyncMock()

        service = InvoiceService(
            es_service=es_mock,
            invoice_ws_manager=ws_manager,
        )

        result = await service.finalize_draft(
            tenant_id="tenant-a",
            invoice_id="inv_fin",
            actor="test",
        )

        # Should have broadcast the updated invoice
        invoice_messages = [
            m for m in ws.sent_messages if m.get("type") == "invoice_updated"
        ]
        assert len(invoice_messages) == 1
        assert invoice_messages[0]["data"]["status"] == "open"

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_affect_service(self):
        """WS broadcast failure must not propagate to the caller."""
        from commerce.services.invoice_service import InvoiceService

        # Create a manager that always fails
        ws_manager = MagicMock()
        ws_manager.broadcast_invoice_update = AsyncMock(
            side_effect=RuntimeError("WS down")
        )

        es_mock = AsyncMock()
        es_mock.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "invoice_id": "inv_fail",
                                "tenant_id": "tenant-a",
                                "status": "open",
                                "total_cents": 10000,
                                "amount_paid_cents": 0,
                                "remaining_cents": 10000,
                                "due_date": "2025-01-01",
                            }
                        }
                    ]
                },
                "aggregations": {"max_seq": {"value": 2}},
            }
        )
        es_mock.index_document = AsyncMock()
        es_mock.update_document = AsyncMock()

        service = InvoiceService(
            es_service=es_mock,
            invoice_ws_manager=ws_manager,
        )

        # mark_overdue should succeed even though WS broadcast fails
        result = await service.mark_overdue(
            tenant_id="tenant-a",
            invoice_id="inv_fail",
            actor="test",
        )

        assert result["status"] == "overdue"
        # Broadcast was attempted
        ws_manager.broadcast_invoice_update.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — Module-level singleton
# ---------------------------------------------------------------------------


class TestModuleSingleton:
    """Verify the module-level get_commerce_invoice_ws_manager works."""

    def test_returns_instance(self):
        """get_commerce_invoice_ws_manager returns a CommerceInvoiceWSManager."""
        mgr = get_commerce_invoice_ws_manager()
        assert isinstance(mgr, CommerceInvoiceWSManager)
        assert mgr.manager_name == "commerce_invoices"
