"""Plan-execution and notification sockets are tenant-only (W1, W2).

Both managers used to call the base all-clients ``broadcast``, so a plan's stop
progress and a customer notification body reached every tenant's sockets. They
now send with ``broadcast_to_tenant`` and drop tenantless data with a WARNING.
The ops (W3) and driver (W4) cases live in ``test_ops_ws_feature_flags.py`` and
``test_driver_ws_manager.py``.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.support.plan_execution_ws_manager import PlanExecutionWSManager
from notifications.ws.notification_ws_manager import NotificationWSManager


def _ws():
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


def _received(ws):
    return [c[0][0] for c in ws.send_json.call_args_list[1:]]


async def _two_tenants(manager):
    ws_a, ws_b = _ws(), _ws()
    await manager.connect(ws_a, tenant_id="tenant-A")
    await manager.connect(ws_b, tenant_id="tenant-B")
    return ws_a, ws_b


def _execution_update(manager, tenant_id):
    return manager.broadcast_execution_update(
        plan_id="p1",
        route_id="r1",
        stop_data={"station_id": "S1", "sequence": 1, "status": "completed"},
        completed_stops=1,
        total_stops=2,
        tenant_id=tenant_id,
    )


SENDS = {
    "execution_update": (PlanExecutionWSManager, _execution_update),
    "notification_created": (
        NotificationWSManager,
        lambda m, t: m.broadcast_notification(
            {"notification_id": "n1", "message_body": "Your delivery", **({"tenant_id": t} if t else {})}
        ),
    ),
    "notification_status_changed": (
        NotificationWSManager,
        lambda m, t: m.broadcast_status_update(
            "n1", "sent", {"notification_id": "n1", **({"tenant_id": t} if t else {})}
        ),
    ),
}


@pytest.mark.parametrize("name", sorted(SENDS))
async def test_reaches_only_its_tenant(name):
    cls, send = SENDS[name]
    manager = cls()
    ws_a, ws_b = await _two_tenants(manager)
    assert await send(manager, "tenant-A") == 1
    assert [m["type"] for m in _received(ws_a)] == [name]
    assert _received(ws_b) == []


@pytest.mark.parametrize("name", sorted(SENDS))
async def test_without_tenant_reaches_nobody(name, caplog):
    cls, send = SENDS[name]
    manager = cls()
    ws_a, ws_b = await _two_tenants(manager)
    with caplog.at_level(logging.WARNING):
        assert await send(manager, "") == 0
    assert _received(ws_a) == [] and _received(ws_b) == []
    assert "without tenant_id dropped" in caplog.text


async def test_dlq_status_update_carries_the_tenant():
    """retry_pipeline.move_to_dlq used to send a tenantless payload."""
    from notifications.services.retry_pipeline import RetryPipeline

    ws_manager = MagicMock()
    ws_manager.broadcast_status_update = AsyncMock(return_value=1)
    service = MagicMock()
    service._ws_manager = ws_manager
    es = MagicMock()
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    pipeline = RetryPipeline.__new__(RetryPipeline)
    pipeline._es = es
    pipeline._notification_service = service

    await pipeline.move_to_dlq(
        {"notification_id": "n1", "tenant_id": "tenant-A", "failure_reason": "x"}
    )

    payload = ws_manager.broadcast_status_update.await_args.args[2]
    assert payload["tenant_id"] == "tenant-A"
