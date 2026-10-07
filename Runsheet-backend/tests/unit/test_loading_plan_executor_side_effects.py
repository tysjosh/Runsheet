"""T-U14: applying a loading plan has no dispatch side effects (R2.6, R9.2, R9.3).

* Only ``order.confirmed`` and ``order.scheduled`` subscribers are notified, and
  no driver counter, driver push, dispatch, notification or jobs write happens.
* ``fuel/services/loading_plan_executor.py`` imports none of the dispatch-time
  modules (AST scan).
* Subscriber pin: every ``.subscribe("order.*", ...)`` registration in the
  backend (excluding tests and the venv) is for ``order.dispatched``,
  ``order.delivered``, ``order.cancelled`` or ``order.failed``. A new subscriber on a status the executor reaches would
  turn plan approval into a side effect; this test makes that visible.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec

from fuel.order_repository import FuelOrderRepository
from fuel.services.loading_plan_executor import LoadingPlanExecutor
from fuel.services.order_service import OrderService
from fuel.websocket.orders_ws import OrdersWSManager
from persistence.plan_execution_lock import PlanExecutionLock
from tests.unit._loading_plan_fakes import (
    ORDERS,
    PLANS,
    InMemoryDocStore,
    order_fixture,
    plan_doc,
)

BACKEND = Path(__file__).resolve().parents[2]
EXECUTOR_PATH = BACKEND / "fuel" / "services" / "loading_plan_executor.py"
T = "tenant-1"
_BANNED_IMPORT_PARTS = ("notification", "plan_dispatch_service", "plan_execution_service", "driver_ws", "jobs")


async def test_apply_notifies_only_confirmed_and_scheduled(monkeypatch):
    import driver.ws.driver_ws_manager as driver_ws_module
    import fuel.services.plan_dispatch_service as dispatch_module
    import notifications.services.notification_service as notification_module

    forbidden = []

    def trap(name):
        async def _trap(*args, **kwargs):
            forbidden.append(name)

        return _trap

    monkeypatch.setattr(dispatch_module.FuelPlanDispatchService, "dispatch", trap("dispatch"))
    monkeypatch.setattr(driver_ws_module.DriverWSManager, "send_assignment", trap("send_assignment"))
    for name, member in inspect.getmembers(notification_module.NotificationService):
        if not name.startswith("__") and inspect.iscoroutinefunction(member):
            monkeypatch.setattr(notification_module.NotificationService, name, trap(f"notification.{name}"))

    orders = [
        order_fixture("ord-1", status="placed"),
        order_fixture("ord-2", status="confirmed"),
        order_fixture("ord-3", status="scheduled"),
    ]
    store = InMemoryDocStore()
    for order in orders:
        store.seed(ORDERS, order["order_id"], order)
    store.seed(PLANS, "plan-1", plan_doc("plan-1", orders=orders))
    repo = FuelOrderRepository(store)
    ws = create_autospec(OrdersWSManager, instance=True)
    counters = AsyncMock()
    service = OrderService(order_repo=repo, ws_manager=ws, driver_counter_service=counters)
    notified = []
    for status in ("placed", "confirmed", "scheduled", "dispatched", "in_transit",
                   "delivered", "failed", "cancelled", "on_hold"):
        name = f"order.{status}"

        async def spy(order, _name=name):
            notified.append(_name)

        service.subscribe(name, spy)
    executor = LoadingPlanExecutor(
        es_service=store, order_repository=repo, order_service=service,
        plan_lock=PlanExecutionLock(use_postgres=False, timeout_seconds=2),
    )

    result = await executor.execute(
        tenant_id=T, plan_id="plan-1", expected_order_ids=["ord-1", "ord-2", "ord-3"],
        expected_truck_id="truck-1", order_snapshots={}, actor_user_id="user-1",
        action_id="act-1", approved_at=None, mode="active_gated",
    )

    assert result.outcome == "applied"
    assert set(notified) == {"order.confirmed", "order.scheduled"}
    assert sorted(notified) == ["order.confirmed", "order.scheduled", "order.scheduled"]
    counters.increment_counters.assert_not_awaited()
    # The event type is the typed method: every WS call is order_status_changed.
    assert {name for name, _args, _kwargs in ws.method_calls} == {"broadcast_order_status_changed"}
    assert forbidden == []
    assert store.write_count(index="jobs_current") == 0
    assert set(store.write_counts()["by_index"]) <= {ORDERS, "fuel_order_events", PLANS}


def _imported_modules(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            yield module
            for alias in node.names:
                yield f"{module}.{alias.name}"


def test_executor_imports_no_dispatch_time_modules():
    tree = ast.parse(EXECUTOR_PATH.read_text())
    imported = list(_imported_modules(tree))
    assert imported, "the scan found no imports; the path is wrong"
    offending = [m for m in imported if any(part in m.lower() for part in _BANNED_IMPORT_PARTS)]
    assert offending == []


def _backend_sources():
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        parts = rel.parts
        if parts[0] in ("venv", ".venv", "tests") or "site-packages" in parts:
            continue
        if path.name.startswith("test_"):
            continue
        yield path


def test_order_subscriber_pin():
    constants = set()
    non_constant = []
    for path in _backend_sources():
        source = path.read_text(encoding="utf-8", errors="replace")
        if ".subscribe(" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "subscribe"
                and node.args
            ):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if first.value.startswith("order."):
                    constants.add(first.value)
                continue
            receiver = ast.get_source_segment(source, node.func.value) or ""
            if "order_service" in receiver:
                non_constant.append(f"{path.relative_to(BACKEND)}:{node.lineno}")

    assert non_constant == [], f"non-constant order_service.subscribe(...) calls: {non_constant}"
    # order.cancelled / order.failed: the margin feed voids an order's estimate
    # record (commerce/hooks/margin_order_subscriber.py). The executor never
    # reaches either status (it moves orders to confirmed / scheduled only).
    assert constants == {"order.dispatched", "order.delivered", "order.cancelled", "order.failed"}
