"""Dispatch Board latency budgets at the N1 tenant size (plan task 39, design K15).

Builds the target day with the real board service over the fake store:
60 lanes holding 1,500 stops in one load each (N1), 120 orders left in the
tray, and a tomorrow draft holding 50 orders, so the snapshot's cross-day
lookup (K5.1) and the suggested-driver searches (K5.2) run at full size.
Every lane has a paired-or-suggested driver.

N1 says "600 orders, 1,500 stops per day", but a stop is one order on one
lane (I1), so both can't hold. The fixture uses the larger figure: 1,500 lane
stops, 1,620 orders that day. Half the lanes hold 29 stops, one under
``MAX_STOPS_PER_LOAD``, so dropping an order there probes a load at the limit
(phase 7 review P7-2); the other half hold 21.

The budgets are the N1 p95 targets (snapshot 2.0 s, batch validate 500 ms,
position validate 300 ms, command 600 ms). With the in-memory store they
measure the service's own CPU cost, not network or database time. The fake
store copies documents through JSON here, as the jsonb column does, instead
of ``copy.deepcopy`` (``_jsonb_store_copies``). Staging numbers are recorded
in plan task 41. Each test
prints its timings so the evidence file can quote them (run with ``-s``).
"""
from __future__ import annotations

import copy
import gc
import json
import statistics
import time
import types
import uuid
from typing import Awaitable, Callable, List

import pytest

from fuel.services import dispatch_validation as dv
from fuel.services.dispatch_board_models import MAX_STOPS_PER_LOAD, CommandBody, DragItem, ValidateBody
from tests.unit import _dispatch_board_fakes, _loading_plan_fakes
from tests.unit._dispatch_board_fakes import (
    COMPARTMENTS,
    TODAY,
    TOMORROW,
    TZ,
    T,
    Harness,
    compartment,
    driver,
)

LANES = 60
FULL_STOPS = MAX_STOPS_PER_LOAD - 1  # even lanes
SHORT_STOPS = 21  # odd lanes
LANE_STOPS = (LANES // 2) * (FULL_STOPS + SHORT_STOPS)
TRAY_ORDERS = 120
TOMORROW_LANES = 10
TOMORROW_STOPS = 5

SNAPSHOT_P95_S = 2.0
BATCH_VALIDATE_P95_S = 0.5
POSITION_VALIDATE_P95_S = 0.3
COMMAND_P95_S = 0.6


def _truck(i: int) -> str:
    return f"PT{i:02d}"


def _stops(i: int) -> int:
    return FULL_STOPS if i % 2 == 0 else SHORT_STOPS


TRAY_ORDER = f"po{LANE_STOPS + 1:04d}"
TRAY_ORDER_2 = f"po{LANE_STOPS + 2:04d}"


def p95(samples: List[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, int(round(0.95 * len(ordered))) - 1)]


async def timed(fn: Callable[[], Awaitable[object]], n: int) -> List[float]:
    out: List[float] = []
    for _ in range(n):
        started = time.perf_counter()
        await fn()
        out.append(time.perf_counter() - started)
    return out


def _coverage_tracing() -> bool:
    try:
        import coverage
    except ImportError:  # pragma: no cover - coverage is a dev dependency
        return False
    return coverage.Coverage.current() is not None


def assert_within(samples: List[float], budget_s: float) -> None:
    """Enforce a p95 budget, except under coverage tracing.

    Line tracing makes this code 3–8x slower (snapshot p95 3.7 s under
    ``--cov`` against 0.45 s without), so a timing there says nothing about N1.
    CI's backend job runs the suite with ``--cov`` and then this module again
    with ``--no-cov``, where the budgets are enforced.
    """
    if _coverage_tracing():
        pytest.skip(f"p95 {p95(samples) * 1000:.0f} ms under coverage tracing; budgets run with --no-cov")
    assert p95(samples) <= budget_s


def report(name: str, samples: List[float]) -> None:
    ms = [s * 1000 for s in samples]
    print(
        f"[perf] {name}: n={len(ms)} first={ms[0]:.1f}ms median={statistics.median(ms):.1f}ms "
        f"p95={p95(ms):.1f}ms max={max(ms):.1f}ms"
    )


async def _build() -> Harness:
    # One driver per truck, assigned to it (suggested-driver search hits) and
    # half of them paired; a few spare drivers.
    drivers = [driver(f"pd{i:02d}", assigned_truck_id=_truck(i)) for i in range(LANES + TOMORROW_LANES)]
    h = Harness(drivers=drivers)
    for i in range(LANES + TOMORROW_LANES):
        for c in range(1, 5):
            h.store.seed(COMPARTMENTS, f"{_truck(i)}_C{c}", compartment(_truck(i), f"C{c}"))
    n = 0
    for i in range(LANES):
        ids = []
        for _ in range(_stops(i)):
            n += 1
            h.seed_order(f"po{n:04d}", gallons_requested=300.0)
            ids.append(f"po{n:04d}")
        truck = _truck(i)
        # A command carries at most 25 orders: the rest join the same load.
        await h.lane_with(truck, *ids[:20], driver_id=f"pd{i:02d}" if i % 2 == 0 else None)
        load_id = h.draft().lanes[truck].loads[0].load_id
        await h.run("assign_orders", order_ids=ids[20:], truck_id=truck, target={"load_id": load_id}, lanes=(truck,))
    for _ in range(TRAY_ORDERS):
        n += 1
        h.seed_order(f"po{n:04d}", gallons_requested=300.0)
    for i in range(LANES, LANES + TOMORROW_LANES):
        ids = []
        for _ in range(TOMORROW_STOPS):
            n += 1
            h.seed_order(f"po{n:04d}", gallons_requested=300.0, day=TOMORROW)
            ids.append(f"po{n:04d}")
        await h.lane_with(_truck(i), *ids, day=TOMORROW)
    return h


def _json_copy(value: object, memo: object = None) -> object:
    """A stored document's round trip through jsonb, the Postgres store's copy."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return _deepcopy(value)


_deepcopy = copy.deepcopy


@pytest.fixture(autouse=True)
def _jsonb_store_copies(monkeypatch):
    # The fake store deep-copies every document on read and write (three
    # times per atomic_update), which costs several times what decoding and
    # encoding the jsonb column costs. Model that store, so the budgets
    # measure the board service, not the fake.
    shim = types.SimpleNamespace(deepcopy=_json_copy)
    monkeypatch.setattr(_loading_plan_fakes, "copy", shim)
    monkeypatch.setattr(_dispatch_board_fakes, "copy", shim)


@pytest.fixture(autouse=True)
def _fresh_process_heap():
    # In the full suite, thousands of earlier tests leave a large heap, and a
    # full collection over it adds 0.5 s pauses to random samples. Move that
    # heap out of the collector's view, as in a fresh service process.
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


_BASE: List[Harness] = []


@pytest.fixture
async def h() -> Harness:
    # Building the day takes seconds; build once and hand each test a copy.
    if not _BASE:
        _BASE.append(await _build())
    return copy.deepcopy(_BASE[0])


async def test_fixture_is_the_n1_size(h):
    assert LANE_STOPS == 1500
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert len(snap["lanes"]) == LANES
    assert sum(len(ld["stops"]) for l in snap["lanes"] for ld in l["loads"]) == LANE_STOPS
    assert max(len(ld["stops"]) for l in snap["lanes"] for ld in l["loads"]) == MAX_STOPS_PER_LOAD - 1
    # The 1,500 drafted orders don't count against the tray's page (K5, N1);
    # tomorrow's orders are excluded (K5.1).
    assert len(snap["trays"]["orders"]) == TRAY_ORDERS
    assert not snap["trays"]["orders_truncated"]
    assert sum(1 for l in snap["lanes"] if l["suggested_driver"]) == LANES // 2


async def test_snapshot_p95_within_budget(h):
    h.drivers.search_calls.clear()
    samples = await timed(lambda: h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ), 10)
    report("snapshot (60 lanes, 1,500 stops, 120 tray, cross-day + suggested driver)", samples)
    assert h.drivers.search_calls, "suggested-driver searches ran"
    assert_within(samples, SNAPSHOT_P95_S)


async def test_batch_validate_p95_within_budget(h):
    body = ValidateBody.model_validate(
        {"item": {"kind": "order", "ids": [TRAY_ORDER]}, "candidates": [_truck(i) for i in range(LANES)]}
    )
    samples = await timed(lambda: h.service.validate(T, TODAY, body, tz=TZ), 20)
    report("batch validate (1 order x 60 lanes, 30 of them reaching 30 stops)", samples)
    assert_within(samples, BATCH_VALIDATE_P95_S)


async def test_position_validate_p95_within_budget(h):
    draft = h.draft()
    # A 29-stop lane: the probe fills the load to MAX_STOPS_PER_LOAD (P7-2).
    load_id = draft.lanes[_truck(4)].loads[0].load_id
    body = ValidateBody.model_validate(
        {
            "item": {"kind": "order", "ids": [TRAY_ORDER_2]},
            "candidates": [_truck(4)],
            "position": {"load_id": load_id, "index": 2},
        }
    )
    samples = await timed(lambda: h.service.validate(T, TODAY, body, tz=TZ), 20)
    report("position validate (1 order, 29-stop lane, index 2)", samples)
    assert_within(samples, POSITION_VALIDATE_P95_S)


@pytest.mark.parametrize("kind", ["order", "stop", "load", "driver"])
async def test_scoped_probes_equal_full_copy_probes_and_never_mutate_the_draft(h, monkeypatch, kind):
    draft = h.draft()
    on_lane = draft.lanes[_truck(0)]
    ids = {
        "order": [TRAY_ORDER],
        "stop": [on_lane.loads[0].stops[1].order_id],
        "load": [on_lane.loads[0].load_id],
        "driver": ["pd00"],  # paired to PT00: probing another lane unpairs it there
    }[kind]
    item = DragItem(kind=kind, ids=ids)
    candidates = [_truck(i) for i in range(LANES)]
    ctx = await h.service._context_for(
        draft,
        candidates,
        extra_drivers=ids if kind == "driver" else [],
        extra_orders=ids if kind in ("order", "stop") else [],
    )
    before = draft.model_dump(mode="json")
    scoped = dv.candidate_results(ctx, draft, item, candidates)
    assert draft.model_dump(mode="json") == before
    # Reference: every lane in scope, i.e. one full copy per candidate.
    monkeypatch.setattr(dv, "_probe_sources", lambda d, _item: set(d.lanes))
    full = dv.candidate_results(ctx, draft, item, candidates)
    assert {t: r.model_dump(mode="json") for t, r in scoped.items()} == {
        t: r.model_dump(mode="json") for t, r in full.items()
    }


async def test_command_p95_within_budget(h):
    # Reorder within a lane, alternating, so every command commits and the
    # draft keeps its full size.
    truck = _truck(4)  # 29 stops
    samples: List[float] = []
    for k in range(20):
        draft = h.draft()
        load = draft.lanes[truck].loads[0]
        moved = load.stops[0].order_id
        cmd = CommandBody.model_validate(
            {
                "type": "move_stops",
                "client_command_id": str(uuid.uuid4()),
                "expected_lane_versions": {truck: draft.lanes[truck].version},
                "order_ids": [moved],
                "truck_id": truck,
                "target": {"load_id": load.load_id, "index": FULL_STOPS - 1},
            }
        ).root
        started = time.perf_counter()
        res = await h.send(cmd)
        samples.append(time.perf_counter() - started)
        assert [l["truck_id"] for l in res["lanes"]] == [truck], res
    report("command (move_stops reorder, 29-stop lane, 1,500-stop draft)", samples)
    assert_within(samples, COMMAND_P95_S)
