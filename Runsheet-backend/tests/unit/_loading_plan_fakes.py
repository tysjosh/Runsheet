"""Shared fakes for the loading-plan executor and MVP dispatch tests.

``InMemoryDocumentStore`` mirrors the parts of the ``ElasticsearchService``
facade (and ``PostgresDocumentStore`` behind it) that the order layer uses,
with the same semantics where they matter:

* ``atomic_update`` hands the transform a deep copy, treats ``None`` as a
  no-op, stamps ``updated_at`` with an ISO string on every applied write and
  returns ``(document, applied)``; a missing document with no ``upsert`` is
  ``(None, False)``.
* ``update_document`` is a shallow merge that stamps ``updated_at`` and raises
  on a missing document.
* ``upsert_if_newer`` discards an incoming ``last_event_timestamp`` that is
  lexically ``<=`` the stored one, like the Postgres store.
* ``search_documents`` evaluates the query with ``persistence.document_matcher``.

Nothing here touches Redis, Postgres or the network.

Fault and interleaving injection: ``store.hooks`` is a list of callables
``hook(op, index, doc_id)`` (sync or async) run before every operation. A hook
can raise to inject a store error, or call :meth:`poke` / :meth:`remove` to
simulate another writer (those bypass hooks and the op log).
:meth:`InMemoryDocumentStore.fail_on` is a keyed shortcut for the raise case
and :meth:`InMemoryDocumentStore.set_yield_schedule` makes every store call
yield to the event loop a scheduled number of times, so ``asyncio.gather``
tests can enumerate interleavings (design test plan L951-960).

Also here (FEAT-002): :class:`TickingClock`, plan and approval builders,
:class:`RaisingRedis` and :class:`FakeFeatureFlagService`.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from fuel.order_models import FuelOrder
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from persistence.document_matcher import matches

ORDERS = FUEL_ORDERS_CURRENT_INDEX
EVENTS = FUEL_ORDER_EVENTS_INDEX

_WRITE_OPS = {"index_document", "update_document", "atomic_update", "upsert_if_newer", "delete_document"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryDocumentStore:
    """Document-store fake with real atomic_update semantics."""

    def __init__(self, *, clock: Optional[Callable[[], str]] = None) -> None:
        self.docs: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        self.ops: List[Tuple[str, str, Optional[str], bool]] = []
        self.hooks: List[Callable[[str, str, Optional[str]], Any]] = []
        self._clock = clock or _iso_now
        self._yields: Optional[Iterable[int]] = None
        self._faults: List[Dict[str, Any]] = []

    # -- injection -----------------------------------------------------

    def set_yield_schedule(self, schedule: Optional[Sequence[int]], *, repeat: bool = False) -> None:
        """Yield ``schedule[i]`` times to the event loop before the i-th call.

        Calls past the end of the schedule do not yield unless ``repeat``.
        ``None`` clears the schedule.
        """
        if schedule is None:
            self._yields = None
            return
        values = list(schedule)

        def gen():
            while True:
                yield from values
                if not repeat:
                    break

        self._yields = gen()

    def fail_on(
        self,
        method: str,
        index: str,
        doc_id: Optional[str] = None,
        *,
        exc: Optional[BaseException] = None,
        nth: int = 1,
        times: int = 1,
    ) -> None:
        """Raise ``exc`` on the ``nth`` (and following ``times-1``) matching calls.

        ``doc_id=None`` matches any id on ``index``.
        """
        self._faults.append(
            {
                "key": (method, index, None if doc_id is None else str(doc_id)),
                "exc": exc or RuntimeError("injected store fault"),
                "nth": nth,
                "times": times,
                "seen": 0,
            }
        )

    def write_count(self, method: Optional[str] = None, index: Optional[str] = None) -> int:
        """Applied writes, optionally filtered by method and/or index."""
        return sum(
            1
            for op, idx, _doc_id in self.writes(index)
            if method is None or op == method
        )

    def write_counts(self) -> Dict[str, Counter]:
        """``{"by_method": Counter, "by_index": Counter}`` of applied writes."""
        writes = self.writes()
        return {
            "by_method": Counter(op for op, _i, _d in writes),
            "by_index": Counter(idx for _o, idx, _d in writes),
        }

    # -- helpers for tests ---------------------------------------------

    def seed(self, index: str, doc_id: str, document: Dict[str, Any]) -> None:
        self.docs[index][str(doc_id)] = copy.deepcopy(document)

    def doc(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        found = self.docs[index].get(str(doc_id))
        return copy.deepcopy(found) if found is not None else None

    def poke(self, index: str, doc_id: str, **fields: Any) -> None:
        """Another writer's partial update: merge and stamp, bypassing hooks."""
        current = self.docs[index][str(doc_id)]
        current.update(copy.deepcopy(fields))
        current["updated_at"] = self._clock()

    def remove(self, index: str, doc_id: str) -> None:
        self.docs[index].pop(str(doc_id), None)

    def writes(self, index: Optional[str] = None) -> List[Tuple[str, str, Optional[str]]]:
        """Applied write operations, optionally for one index."""
        return [
            (op, idx, doc_id)
            for op, idx, doc_id, applied in self.ops
            if op in _WRITE_OPS and applied and (index is None or idx == index)
        ]

    def calls(self, op: str, index: Optional[str] = None) -> List[Optional[str]]:
        return [
            doc_id
            for o, idx, doc_id, _applied in self.ops
            if o == op and (index is None or idx == index)
        ]

    def events(self, order_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [
            copy.deepcopy(e)
            for e in self.docs[EVENTS].values()
            if order_id is None or e.get("order_id") == order_id
        ]

    async def _before(self, op: str, index: str, doc_id: Optional[str]) -> None:
        if self._yields is not None:
            for _ in range(next(self._yields, 0)):
                await asyncio.sleep(0)
        key_id = None if doc_id is None else str(doc_id)
        for fault in self._faults:
            method, idx, fid = fault["key"]
            if method == op and idx == index and (fid is None or fid == key_id):
                fault["seen"] += 1
                if fault["nth"] <= fault["seen"] < fault["nth"] + fault["times"]:
                    raise fault["exc"]
        for hook in list(self.hooks):
            result = hook(op, index, doc_id)
            if inspect.isawaitable(result):
                await result

    def _log(self, op: str, index: str, doc_id: Optional[str], applied: bool) -> None:
        self.ops.append((op, index, None if doc_id is None else str(doc_id), applied))

    # -- facade --------------------------------------------------------

    async def get_document(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        await self._before("get_document", index, doc_id)
        self._log("get_document", index, doc_id, False)
        return self.doc(index, doc_id)

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]) -> Dict[str, Any]:
        await self._before("index_document", index, doc_id)
        self.docs[index][str(doc_id)] = copy.deepcopy(document)
        self._log("index_document", index, doc_id, True)
        return {"_index": index, "_id": str(doc_id), "result": "created"}

    async def update_document(self, index: str, doc_id: str, partial_doc: Dict[str, Any]) -> Dict[str, Any]:
        await self._before("update_document", index, doc_id)
        current = self.docs[index].get(str(doc_id))
        if current is None:
            raise LookupError(f"document {index}/{doc_id} not found")
        current.update(copy.deepcopy(partial_doc))
        current["updated_at"] = self._clock()
        self._log("update_document", index, doc_id, True)
        return {"_index": index, "_id": str(doc_id), "result": "updated"}

    async def delete_document(self, index: str, doc_id: str) -> bool:
        await self._before("delete_document", index, doc_id)
        existed = self.docs[index].pop(str(doc_id), None) is not None
        self._log("delete_document", index, doc_id, existed)
        return existed

    async def atomic_update(
        self,
        index: str,
        doc_id: str,
        transform: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
        *,
        upsert: Optional[Dict[str, Any]] = None,
        **_ignored: Any,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        await self._before("atomic_update", index, doc_id)
        current = self.docs[index].get(str(doc_id))
        if current is None:
            if upsert is None:
                self._log("atomic_update", index, doc_id, False)
                return (None, False)
            document = copy.deepcopy(upsert)
            document.setdefault("created_at", self._clock())
            document["updated_at"] = self._clock()
            self.docs[index][str(doc_id)] = document
            self._log("atomic_update", index, doc_id, True)
            return (copy.deepcopy(document), True)
        updated = transform(copy.deepcopy(current))
        if updated is None:
            self._log("atomic_update", index, doc_id, False)
            return (copy.deepcopy(current), False)
        updated = copy.deepcopy(updated)
        updated["updated_at"] = self._clock()
        self.docs[index][str(doc_id)] = updated
        self._log("atomic_update", index, doc_id, True)
        return (copy.deepcopy(updated), True)

    async def upsert_if_newer(
        self,
        index: str,
        doc_id: str,
        document: Dict[str, Any],
        *,
        timestamp_field: str = "last_event_timestamp",
    ) -> bool:
        incoming = document.get(timestamp_field)

        def _transform(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            stored = current.get(timestamp_field)
            if stored is not None and incoming is not None and incoming <= stored:
                return None
            return {**current, **document}

        _doc, applied = await self.atomic_update(index, doc_id, _transform, upsert=dict(document))
        return applied

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 10) -> Dict[str, Any]:
        await self._before("search_documents", index, None)
        self._log("search_documents", index, None, False)
        body = (query or {}).get("query")
        hits = [
            {"_id": doc_id, "_source": copy.deepcopy(doc)}
            for doc_id, doc in self.docs[index].items()
            if matches(doc, body, doc_id=doc_id)
        ]
        sort = []
        for clause in (query or {}).get("sort") or []:
            if isinstance(clause, str):
                sort.append((clause, False))
                continue
            (field, spec), = clause.items()
            sort.append((field, (spec.get("order") if isinstance(spec, dict) else spec) == "desc"))
        for field, reverse in reversed(sort):
            hits.sort(key=lambda h: (h["_source"].get(field) is None, str(h["_source"].get(field) or "")), reverse=reverse)
        if sort:
            for hit in hits:
                hit["sort"] = [hit["_source"].get(field) for field, _r in sort]
        after = (query or {}).get("search_after")
        if after is not None and sort:
            # Strictly after the cursor in the first sort key's direction.
            def _key(values):
                return tuple("" if v is None else str(v) for v in values)

            cursor = _key(after)
            descending = sort[0][1]
            hits = [
                h for h in hits
                if (_key(h["sort"]) < cursor if descending else _key(h["sort"]) > cursor)
            ]
        limit = (query or {}).get("size", size) or size
        offset = (query or {}).get("from", 0) or 0
        page = hits[offset : offset + limit]
        return {"hits": {"hits": page, "total": {"value": len(hits)}}}


def fuel_order_doc(
    order_id: str,
    *,
    tenant_id: str = "tenant-1",
    status: str = "confirmed",
    last_event_timestamp: str = "2026-07-29T12:00:00+00:00",
    **overrides: Any,
) -> Dict[str, Any]:
    """A valid stored fuel order document (``FuelOrder`` JSON dump)."""
    payload: Dict[str, Any] = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "customer_id": "customer-1",
        "customer_name": "Acme Fuels",
        "ship_to_address": "1 Depot Road",
        "ship_to_lat": 40.0,
        "ship_to_lon": -74.0,
        "customer_tank_id": f"tank-{order_id}",
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "call_type": "one_off",
        "delivery_window_start": "2026-07-30T08:00:00+00:00",
        "delivery_window_end": "2026-07-30T12:00:00+00:00",
        "intake_channel": "dispatcher",
        "intake_channel_id": "ch-1",
        "status": status,
        "source_schema_version": "1.0",
        "trace_id": "trace-1",
        "created_at": "2026-07-29T11:00:00+00:00",
        "updated_at": last_event_timestamp,
        "last_event_timestamp": last_event_timestamp,
    }
    payload.update(overrides)
    return FuelOrder(**payload).model_dump(mode="json")


#: The design's name for the store (test plan L951).
InMemoryDocStore = InMemoryDocumentStore

PLANS = "mvp_load_plans"
APPROVALS = "agent_approval_queue"


class TickingClock:
    """Every call returns a strictly later timezone-aware ``datetime``.

    The production clock (``services.time_utils.utcnow``) returns an aware
    ``datetime``; frozen clocks are not allowed in the executor suites.
    :meth:`advance` jumps forward, e.g. past a plan lease.
    """

    def __init__(
        self,
        start: Optional[datetime] = None,
        *,
        step: timedelta = timedelta(milliseconds=1),
    ) -> None:
        self._now = start or datetime.now(timezone.utc)
        self._step = step

    def __call__(self) -> datetime:
        self._now = self._now + self._step
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    @property
    def current(self) -> datetime:
        return self._now


def order_fixture(
    order_id: str,
    *,
    status: str = "confirmed",
    tenant_id: str = "tenant-1",
    **overrides: Any,
) -> Dict[str, Any]:
    """A stored order in ``placed`` / ``confirmed`` / ``scheduled`` (with a window)."""
    return fuel_order_doc(order_id, tenant_id=tenant_id, status=status, **overrides)


def plan_doc(
    plan_id: str = "plan-1",
    *,
    orders: Sequence[Dict[str, Any]] = (),
    tenant_id: str = "tenant-1",
    truck_id: str = "truck-1",
    run_id: Optional[str] = "run-1",
    status: str = "proposed",
    **overrides: Any,
) -> Dict[str, Any]:
    """An ``mvp_load_plans`` document with one assignment per order.

    Each assignment carries the order's ``order_id`` and ``product_code``,
    ``station_id = customer_id`` (as the loader builds it), a compartment and
    the litres.
    """
    assignments = [
        {
            "compartment_id": f"c-{i + 1}",
            "station_id": order.get("customer_id"),
            "order_id": order.get("order_id"),
            "fuel_grade": order.get("product_code"),
            "product_code": order.get("product_code"),
            "quantity_liters": 1500.0,
            "compartment_capacity_liters": 2000.0,
        }
        for i, order in enumerate(orders)
    ]
    doc: Dict[str, Any] = {
        "plan_id": plan_id,
        "truck_id": truck_id,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "status": status,
        "assignments": assignments,
        "created_at": "2026-07-29T11:30:00+00:00",
        "updated_at": "2026-07-29T11:30:00+00:00",
    }
    doc.update(overrides)
    return doc


def approval_entry(
    action_id: str,
    plan: Dict[str, Any],
    *,
    status: str = "pending",
    tenant_id: Optional[str] = None,
    proposed_at: str = "2026-07-29T11:31:00+00:00",
    expiry_time: str = "2099-01-01T00:00:00+00:00",
    **overrides: Any,
) -> Dict[str, Any]:
    """An ``agent_approval_queue`` entry for ``apply_loading_plan`` on ``plan``."""
    order_ids = sorted(a["order_id"] for a in plan.get("assignments") or [] if a.get("order_id"))
    doc: Dict[str, Any] = {
        "action_id": action_id,
        "action_type": "mutation",
        "tool_name": "apply_loading_plan",
        "parameters": {
            "plan_id": plan.get("plan_id"),
            "run_id": plan.get("run_id"),
            "order_ids": order_ids,
            "truck_id": plan.get("truck_id"),
            "assignments": copy.deepcopy(plan.get("assignments") or []),
        },
        "risk_level": "high",
        "proposed_by": "compartment_loading",
        "proposed_at": proposed_at,
        "status": status,
        "reviewed_by": None,
        "reviewed_at": None,
        "expiry_time": expiry_time,
        "impact_summary": "apply loading plan",
        "tenant_id": tenant_id or plan.get("tenant_id"),
    }
    doc.update(overrides)
    return doc


class RaisingRedis:
    """A Redis client whose ``get`` always raises ``ConnectionError``."""

    def __init__(self) -> None:
        self.get_calls: List[str] = []

    async def get(self, key: str) -> Any:
        self.get_calls.append(key)
        raise ConnectionError("redis unavailable")

    async def expire(self, *args: Any, **kwargs: Any) -> bool:  # pragma: no cover - unreachable
        return True

    async def ttl(self, *args: Any, **kwargs: Any) -> int:  # pragma: no cover - unreachable
        return -1


class DictRedis:
    """A minimal in-memory Redis client (``get`` returns bytes) for flag reads."""

    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        self.values: Dict[str, Any] = dict(values or {})

    async def get(self, key: str) -> Any:
        value = self.values.get(key)
        return value.encode("utf-8") if isinstance(value, str) else value

    async def expire(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def ttl(self, *args: Any, **kwargs: Any) -> int:
        return -1


class FakeFeatureFlagService:
    """Stands in for ``FeatureFlagService.get_overlay_state_strict``.

    ``mode`` is returned as is (``None`` = unset); ``raises`` makes the read
    raise. ``modes`` is a list consumed per call (the last value repeats), for
    flip-between-reads tests. ``get_overlay_state_or_none`` raises so a test
    fails if anything consults the lenient read (R6.4).
    """

    def __init__(
        self,
        mode: Optional[str] = "active_gated",
        *,
        raises: Optional[BaseException] = None,
        modes: Optional[Sequence[Optional[str]]] = None,
    ) -> None:
        self.mode = mode
        self.raises = raises
        self.modes = list(modes) if modes else None
        self.calls: List[Tuple[str, str]] = []

    async def get_overlay_state_strict(self, flag_key: str, tenant_id: str) -> Optional[str]:
        self.calls.append((flag_key, tenant_id))
        if self.raises is not None:
            raise self.raises
        if self.modes:
            return self.modes.pop(0) if len(self.modes) > 1 else self.modes[0]
        return self.mode

    async def get_overlay_state_or_none(self, flag_key: str, tenant_id: str) -> Optional[str]:
        raise AssertionError("the lenient overlay read must not be used for loading plans")


__all__ = [
    "APPROVALS",
    "DictRedis",
    "EVENTS",
    "FakeFeatureFlagService",
    "InMemoryDocStore",
    "InMemoryDocumentStore",
    "ORDERS",
    "PLANS",
    "RaisingRedis",
    "TickingClock",
    "approval_entry",
    "fuel_order_doc",
    "order_fixture",
    "plan_doc",
]
