"""Shared fakes and builders for the RevenueGuard tests (FEAT-004).

RevenueGuard runs against the real :class:`MarginRepository` on the in-memory
SQLite database from ``conftest.margin_engine``. Everything around it is a
small fake:

* :class:`ExplodingStore` fails on any document-store call, which proves
  RevenueGuard never reads ``jobs_current`` and never indexes
  ``agent_shadow_proposals`` (AC-45, AC-43).
* :class:`FakeFlags` serves ``overlay.revenue_guard`` per tenant through the
  real ``OverlayAgentBase._get_mode``.
* :class:`RecordingRepo` wraps a repository and logs every call with the
  tenant id it carries, sharing one ordered log with :class:`FakeFlags`, so a
  test can assert that nothing touched a tenant after its mode read.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

from sqlalchemy import delete, select

from Agents.overlay.revenue_guard import RevenueGuard
from commerce.services.margin_repository import MarginCandidate, MarginRepository
from commerce.services.margin_service import invoice_source_key, order_source_key
from persistence.database import session_scope
from persistence.models import MarginAlertORM, MarginRecomputeRunORM, MarginRecordORM

from .conftest import _candidate

UTC = timezone.utc


class ExplodingStore:
    """Document store that records and fails every call."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            raise AssertionError(f"RevenueGuard touched the document store: {name}{args[:1]}")

        return _call


class FakeFlags:
    """``get_overlay_state_or_none`` per tenant; logs ``("mode", tenant)``."""

    def __init__(self, modes: Optional[Dict[str, str]] = None, *, default: str = "active_gated",
                 log: Optional[List[Tuple[str, Optional[str]]]] = None) -> None:
        self.modes = dict(modes or {})
        self.default = default
        self.log = log if log is not None else []

    async def get_overlay_state_or_none(self, flag_key: str, tenant_id: str) -> str:
        assert flag_key == "overlay.revenue_guard"
        self.log.append(("mode", tenant_id))
        return self.modes.get(tenant_id, self.default)


class CapturingActivity:
    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []
        self.log_monitoring_cycle = AsyncMock()

    async def log(self, entry: Dict[str, Any]) -> str:
        self.entries.append(copy.deepcopy(entry))
        return f"log-{len(self.entries)}"

    def actions(self) -> List[str]:
        return [e["action_type"] for e in self.entries]


class CapturingBus:
    def __init__(self) -> None:
        self.published: List[Any] = []
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()

    async def publish(self, message: Any) -> int:
        self.published.append(message)
        return 1


class _RecordingDiscovery:
    def __init__(self, inner: Any, log: List[Tuple[str, Optional[str]]]) -> None:
        self._inner = inner
        self._log = log

    async def pending_work_tenants(self) -> List[str]:
        self._log.append(("pending_work_tenants", None))
        return await self._inner.pending_work_tenants()


class RecordingRepo:
    """Proxy that logs ``(method, tenant_id)`` for every repository call."""

    def __init__(self, inner: MarginRepository, log: List[Tuple[str, Optional[str]]]) -> None:
        self._inner = inner
        self.log = log
        self.discovery = _RecordingDiscovery(inner.discovery, log)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        async def _call(*args: Any, **kwargs: Any) -> Any:
            self.log.append((name, args[0] if args else kwargs.get("tenant_id")))
            return await attr(*args, **kwargs)

        return _call

    def tenants_touched_by(self, method: str) -> List[Optional[str]]:
        return [tenant for name, tenant in self.log if name == method]


class Harness:
    def __init__(self, guard: RevenueGuard, *, store: ExplodingStore, flags: FakeFlags,
                 activity: CapturingActivity, bus: CapturingBus, confirmation: Any) -> None:
        self.guard = guard
        self.store = store
        self.flags = flags
        self.activity = activity
        self.bus = bus
        self.confirmation = confirmation


def build_guard(
    repo: Any,
    *,
    modes: Optional[Dict[str, str]] = None,
    default: str = "active_gated",
    log: Optional[List[Tuple[str, Optional[str]]]] = None,
    leakage_threshold: int = 3,
) -> Harness:
    store = ExplodingStore()
    flags = FakeFlags(modes, default=default, log=log)
    activity = CapturingActivity()
    bus = CapturingBus()
    confirmation = AsyncMock()
    guard = RevenueGuard(
        signal_bus=bus,
        es_service=store,
        activity_log_service=activity,
        ws_manager=AsyncMock(),
        confirmation_protocol=confirmation,
        autonomy_config_service=AsyncMock(),
        feature_flag_service=flags,
        leakage_threshold=leakage_threshold,
    )
    if repo is not None:
        guard.set_margin_repository(repo)
    return Harness(guard, store=store, flags=flags, activity=activity, bus=bus,
                   confirmation=confirmation)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

BASE_AS_OF = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)


def delivery(order_id: str, *, n: int = 0, flag: Optional[str] = None,
             customer: str = "CUST-1", product: str = "DIESEL_2", **overrides: Any) -> MarginCandidate:
    """A delivery candidate; ``flag`` in negative | missing | below | None."""

    values: Dict[str, Any] = dict(
        stage="delivery",
        source_key=order_source_key(order_id),
        order_id=order_id,
        customer_id=customer,
        product_code=product,
        as_of=BASE_AS_OF + timedelta(hours=n),
        input_hash=f"h-{order_id}",
    )
    values.update(_flag_values(flag))
    values.update(overrides)
    return _candidate(**values)


def invoice_line(invoice_id: str, line_index: int, order_id: str, *, n: int = 0,
                 flag: Optional[str] = None, customer: str = "CUST-1",
                 product: str = "DIESEL_2", **overrides: Any) -> MarginCandidate:
    values: Dict[str, Any] = dict(
        stage="invoice",
        source_key=invoice_source_key(invoice_id, line_index),
        order_id=order_id,
        invoice_id=invoice_id,
        line_index=line_index,
        customer_id=customer,
        product_code=product,
        as_of=BASE_AS_OF + timedelta(hours=n),
        input_hash=f"h-{invoice_id}-{line_index}",
    )
    values.update(_flag_values(flag))
    values.update(overrides)
    return _candidate(**values)


def _flag_values(flag: Optional[str]) -> Dict[str, Any]:
    if flag is None:
        return {}
    if flag == "negative":
        return dict(flag_negative_margin=True, margin_cents=-10_000, margin_per_gallon_micros=-100_000,
                    margin_bp=-333, cost_cents=310_000)
    if flag == "below":
        return dict(flag_below_floor=True, margin_cents=5_000, margin_per_gallon_micros=50_000,
                    margin_bp=167, cost_cents=295_000)
    if flag == "missing":
        return dict(method="none", product_cost_micros=None, landed_cost_micros=None, cost_cents=None,
                    margin_cents=None, margin_per_gallon_micros=None, margin_bp=None,
                    no_cost_reason="no_lots_no_rack", flag_missing_cost=True)
    raise ValueError(flag)


async def write(repo: MarginRepository, tenant_id: str, candidate: MarginCandidate,
                mode: str = "live", *, now: Optional[datetime] = None) -> Dict[str, Any]:
    result = await repo.write_record(tenant_id, candidate, mode, now=now)
    assert result.written, result
    return result.record


async def record(repo: MarginRepository, tenant_id: str, record_id: str) -> Dict[str, Any]:
    row = await repo.get_record(tenant_id, record_id)
    assert row is not None
    return row


async def alerts(repo: MarginRepository, tenant_id: str, *, alert_type: Optional[str] = None) -> List[Dict[str, Any]]:
    return (await repo.list_alerts(tenant_id, alert_type=alert_type, limit=500)).items


async def completed_run(repo: MarginRepository, tenant_id: str, by_flag: Dict[str, int]) -> str:
    run = await repo.start_run(
        tenant_id,
        requested_by="admin-1",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        stages=["delivery"],
        only_missing=False,
        reason="QA",
    )
    counts = {"sources": 5, "written": 5, "errors": 0, "by_flag": by_flag}
    await repo.finish_run(tenant_id, run["run_id"], status="completed", counts=counts)
    return run["run_id"]


async def clear_margin_tables() -> None:
    async with session_scope() as session:
        for model in (MarginAlertORM, MarginRecordORM, MarginRecomputeRunORM):
            await session.execute(delete(model))


async def alert_state(record_id: str) -> str:
    async with session_scope() as session:
        return (
            await session.execute(
                select(MarginRecordORM.alert_state).where(MarginRecordORM.record_id == record_id)
            )
        ).scalar_one()
