"""Dispatch Board latency budgets at the N1 tenant size (plan task 39, design K15).

Builds the target day with the real board service over the fake store:
60 lanes (each with a paired-or-suggested driver and 8 stops in one load),
120 orders left in the tray (600 orders that day), and a tomorrow draft
holding 50 orders, so the snapshot's cross-day lookup (K5.1) and the
suggested-driver searches (K5.2) run at full size.

The budgets are the N1 p95 targets (snapshot 2.0 s, batch validate 500 ms,
position validate 300 ms, command 600 ms). With the in-memory store they
measure the service's own CPU cost, not network or database time; staging
numbers are recorded in plan task 41. Each test prints its timings so the
evidence file can quote them (run with ``-s``).
"""
from __future__ import annotations

import copy
import statistics
import time
import uuid
from typing import Awaitable, Callable, List

import pytest

from fuel.services import dispatch_validation as dv
from fuel.services.dispatch_board_models import CommandBody, DragItem, ValidateBody
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
STOPS_PER_LANE = 8
TRAY_ORDERS = 120
TOMORROW_LANES = 10
TOMORROW_STOPS = 5

SNAPSHOT_P95_S = 2.0
BATCH_VALIDATE_P95_S = 0.5
POSITION_VALIDATE_P95_S = 0.3
COMMAND_P95_S = 0.6


def _truck(i: int) -> str:
    return f"PT{i:02d}"


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
        for _ in range(STOPS_PER_LANE):
            n += 1
            h.seed_order(f"po{n:04d}", gallons_requested=300.0)
            ids.append(f"po{n:04d}")
        truck = _truck(i)
        await h.lane_with(truck, *ids, driver_id=f"pd{i:02d}" if i % 2 == 0 else None)
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


_BASE: List[Harness] = []


@pytest.fixture
async def h() -> Harness:
    # Building the day takes seconds; build once and hand each test a copy.
    if not _BASE:
        _BASE.append(await _build())
    return copy.deepcopy(_BASE[0])


async def test_fixture_is_the_n1_size(h):
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert len(snap["lanes"]) == LANES
    assert sum(len(ld["stops"]) for l in snap["lanes"] for ld in l["loads"]) == LANES * STOPS_PER_LANE
    assert len(snap["trays"]["orders"]) == TRAY_ORDERS  # tomorrow's orders are excluded (K5.1)
    assert sum(1 for l in snap["lanes"] if l["suggested_driver"]) == LANES // 2


async def test_snapshot_p95_within_budget(h):
    h.drivers.search_calls.clear()
    samples = await timed(lambda: h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ), 10)
    report("snapshot (60 lanes, 480 stops, 120 tray, cross-day + suggested driver)", samples)
    assert h.drivers.search_calls, "suggested-driver searches ran"
    assert p95(samples) <= SNAPSHOT_P95_S


async def test_batch_validate_p95_within_budget(h):
    body = ValidateBody.model_validate(
        {"item": {"kind": "order", "ids": [f"po{LANES * STOPS_PER_LANE + 1:04d}"]}, "candidates": [_truck(i) for i in range(LANES)]}
    )
    samples = await timed(lambda: h.service.validate(T, TODAY, body, tz=TZ), 20)
    report("batch validate (1 order x 60 lanes)", samples)
    assert p95(samples) <= BATCH_VALIDATE_P95_S


async def test_position_validate_p95_within_budget(h):
    draft = h.draft()
    load_id = draft.lanes[_truck(3)].loads[0].load_id
    body = ValidateBody.model_validate(
        {
            "item": {"kind": "order", "ids": [f"po{LANES * STOPS_PER_LANE + 2:04d}"]},
            "candidates": [_truck(3)],
            "position": {"load_id": load_id, "index": 2},
        }
    )
    samples = await timed(lambda: h.service.validate(T, TODAY, body, tz=TZ), 20)
    report("position validate (1 order, 1 lane, index 2)", samples)
    assert p95(samples) <= POSITION_VALIDATE_P95_S


@pytest.mark.parametrize("kind", ["order", "stop", "load", "driver"])
async def test_scoped_probes_equal_full_copy_probes_and_never_mutate_the_draft(h, monkeypatch, kind):
    draft = h.draft()
    on_lane = draft.lanes[_truck(0)]
    ids = {
        "order": [f"po{LANES * STOPS_PER_LANE + 1:04d}"],
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
    truck = _truck(5)
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
                "target": {"load_id": load.load_id, "index": STOPS_PER_LANE - 1},
            }
        ).root
        started = time.perf_counter()
        res = await h.send(cmd)
        samples.append(time.perf_counter() - started)
        assert [l["truck_id"] for l in res["lanes"]] == [truck], res
    report("command (move_stops reorder on a 60-lane draft)", samples)
    assert p95(samples) <= COMMAND_P95_S
