"""Versioned write protocol, DB constraints and skip rows (SQLite).

Design "Versioned write protocol": every (latest-row state x mode) cell of the
rule table, AC-17 idempotency, ``alert_state`` assignment, ``pending -> done``
on void and supersede, skipped-source cleanup, the one-retry rule, and the
CHECK constraints that make a missing cost stored as 0 impossible (AC-8).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from commerce.services import margin_repository as mr
from persistence.database import session_scope
from persistence.models import (
    MarginCostEntryORM,
    MarginRecordORM,
    MarginSkippedSourceORM,
)

from .conftest import AS_OF, TENANT_A, TENANT_B

STAGE, KEY = "delivery", "order:ORD-1"


async def _rows(tenant_id=TENANT_A, stage=STAGE, key=KEY):
    async with session_scope() as session:
        result = await session.execute(
            select(MarginRecordORM)
            .where(
                MarginRecordORM.tenant_id == tenant_id,
                MarginRecordORM.stage == stage,
                MarginRecordORM.source_key == key,
            )
            .order_by(MarginRecordORM.version)
        )
        return list(result.scalars())


def _shape(rows):
    return [(r.version, r.status, r.frozen_at is not None) for r in rows]


async def _arrange(repo, make_candidate, state):
    base = make_candidate(input_hash="h1")
    if state == "none":
        return
    if state == "void":
        await repo.write_record(TENANT_A, base, "live")
        await repo.write_record(TENANT_A, base, "void")
    elif state.startswith("frozen"):
        await repo.write_record(TENANT_A, base, "finalize")
    elif state.startswith("active"):
        await repo.write_record(TENANT_A, base, "live")


# (state, mode) -> (outcome, resulting [(version, status, frozen)])
RULES = {
    ("none", "live"): ("inserted", [(1, "active", False)]),
    ("none", "finalize"): ("inserted", [(1, "active", True)]),
    ("none", "void"): ("inserted", [(1, "void", False)]),
    ("none", "recompute"): ("inserted", [(1, "active", False)]),
    ("void", "live"): ("skipped", [(1, "void", False)]),
    ("void", "finalize"): ("skipped", [(1, "void", False)]),
    ("void", "void"): ("skipped", [(1, "void", False)]),
    ("void", "recompute"): ("skipped", [(1, "void", False)]),
    ("frozen_same", "live"): ("skipped", [(1, "active", True)]),
    ("frozen_same", "finalize"): ("skipped", [(1, "active", True)]),
    ("frozen_same", "void"): ("voided", [(1, "void", True)]),
    ("frozen_same", "recompute"): ("skipped", [(1, "active", True)]),
    ("frozen_diff", "live"): ("skipped", [(1, "active", True)]),
    ("frozen_diff", "finalize"): ("skipped", [(1, "active", True)]),
    ("frozen_diff", "void"): ("voided", [(1, "void", True)]),
    ("frozen_diff", "recompute"): (
        "superseded",
        [(1, "superseded", True), (2, "active", True)],
    ),
    ("active_same", "live"): ("skipped", [(1, "active", False)]),
    ("active_same", "finalize"): ("frozen", [(1, "active", True)]),
    ("active_same", "void"): ("voided", [(1, "void", False)]),
    ("active_same", "recompute"): ("skipped", [(1, "active", False)]),
    ("active_diff", "live"): ("superseded", [(1, "superseded", False), (2, "active", False)]),
    ("active_diff", "finalize"): (
        "superseded",
        [(1, "superseded", False), (2, "active", True)],
    ),
    ("active_diff", "void"): ("voided", [(1, "void", False)]),
    ("active_diff", "recompute"): (
        "superseded",
        [(1, "superseded", False), (2, "active", False)],
    ),
}


@pytest.mark.parametrize("state, mode", sorted(RULES), ids=lambda v: str(v))
async def test_rule_table(repo, make_candidate, state, mode):
    await _arrange(repo, make_candidate, state)
    new_hash = "h2" if state.endswith("diff") else "h1"
    result = await repo.write_record(TENANT_A, make_candidate(input_hash=new_hash), mode)

    outcome, shape = RULES[(state, mode)]
    assert result.outcome == outcome
    assert result.written is (outcome != "skipped")
    rows = await _rows()
    assert _shape(rows) == shape
    assert sum(r.status == "active" for r in rows) <= 1
    if outcome in ("inserted", "superseded"):
        newest = rows[-1]
        assert result.record["record_id"] == newest.record_id
        assert newest.origin == ("recompute" if mode == "recompute" else "live")


def test_rule_table_covers_every_cell():
    states = {"none", "void", "frozen_same", "frozen_diff", "active_same", "active_diff"}
    modes = {"live", "finalize", "void", "recompute"}
    assert set(RULES) == {(s, m) for s in states for m in modes}


async def test_recompute_freezes_rows_for_final_sources(repo, make_candidate):
    candidate = make_candidate(stage="invoice", source_key="invoice:INV-1:line:0", source_final=True)
    await repo.write_record(TENANT_A, candidate, "recompute")
    rows = await _rows(stage="invoice", key="invoice:INV-1:line:0")
    assert _shape(rows) == [(1, "active", True)]
    assert rows[0].origin == "recompute"

    await repo.write_record(TENANT_A, replace(candidate, source_key="invoice:INV-2:line:0",
                                              source_final=False, input_hash="h1"), "live")
    result = await repo.write_record(
        TENANT_A,
        replace(candidate, source_key="invoice:INV-2:line:0", input_hash="h2"),
        "recompute",
    )
    assert result.outcome == "superseded"
    assert _shape(await _rows(stage="invoice", key="invoice:INV-2:line:0")) == [
        (1, "superseded", False),
        (2, "active", True),
    ]


async def test_idempotent_replay_keeps_one_active_row(repo, make_candidate):
    """AC-17: the same event processed twice gives one active record."""

    candidate = make_candidate()
    first = await repo.write_record(TENANT_A, candidate, "live")
    second = await repo.write_record(TENANT_A, candidate, "live")
    assert (first.outcome, second.outcome) == ("inserted", "skipped")
    assert _shape(await _rows()) == [(1, "active", False)]


async def test_void_then_replay_never_resurrects(repo, make_candidate):
    candidate = make_candidate()
    await repo.write_record(TENANT_A, candidate, "void")
    for mode in ("live", "finalize", "recompute"):
        assert (await repo.write_record(TENANT_A, candidate, mode)).outcome == "skipped"
    assert _shape(await _rows()) == [(1, "void", False)]


# ---------------------------------------------------------------------------
# alert_state assignment
# ---------------------------------------------------------------------------

_FLAGS = {
    "none": {},
    "negative": {"flag_negative_margin": True},
    "below_floor": {"flag_below_floor": True},
    "terminal_unattributed": {"flag_terminal_unattributed": True},
}


@pytest.mark.parametrize("stage", ["order_estimate", "delivery", "invoice"])
@pytest.mark.parametrize("mode", ["live", "finalize", "recompute"])
@pytest.mark.parametrize("flags", sorted(_FLAGS) + ["missing"])
async def test_alert_state_on_insert(repo, make_candidate, make_missing_cost, stage, mode, flags):
    if flags == "missing":
        candidate = make_missing_cost(stage=stage)
    else:
        candidate = make_candidate(stage=stage, **_FLAGS[flags])
    result = await repo.write_record(TENANT_A, candidate, mode)
    alerting = (
        mode != "recompute"
        and stage in ("delivery", "invoice")
        and flags in ("negative", "below_floor", "missing")
    )
    assert result.record["alert_state"] == ("pending" if alerting else "none")


async def test_void_tombstone_never_alerts(repo, make_missing_cost):
    result = await repo.write_record(TENANT_A, make_missing_cost(), "void")
    assert (result.record["status"], result.record["alert_state"]) == ("void", "none")


async def test_void_moves_pending_to_done(repo, make_candidate):
    await repo.write_record(TENANT_A, make_candidate(flag_negative_margin=True), "live")
    result = await repo.write_record(TENANT_A, make_candidate(), "void")
    assert (result.record["status"], result.record["alert_state"]) == ("void", "done")


async def test_supersede_moves_pending_to_done(repo, make_candidate):
    await repo.write_record(TENANT_A, make_candidate(flag_below_floor=True), "live")
    await repo.write_record(
        TENANT_A, make_candidate(flag_below_floor=True, input_hash="h2"), "live"
    )
    rows = await _rows()
    assert [(r.status, r.alert_state) for r in rows] == [
        ("superseded", "done"),
        ("active", "pending"),
    ]


async def test_freeze_in_place_keeps_alert_state(repo, make_candidate):
    await repo.write_record(TENANT_A, make_candidate(flag_negative_margin=True), "live")
    result = await repo.write_record(TENANT_A, make_candidate(flag_negative_margin=True), "finalize")
    assert result.outcome == "frozen"
    assert result.record["alert_state"] == "pending"


# ---------------------------------------------------------------------------
# void_latest (void needs no cost)
# ---------------------------------------------------------------------------


async def test_void_latest(repo, make_candidate):
    assert (await repo.void_latest(TENANT_A, STAGE, KEY)).outcome == "missing"
    await repo.write_record(TENANT_A, make_candidate(), "live")
    result = await repo.void_latest(TENANT_A, STAGE, KEY)
    assert result.outcome == "voided"
    # The original cost fields are kept as they were.
    assert result.record["status"] == "void"
    assert (result.record["method"], result.record["cost_cents"]) == ("wac", 250_000)
    assert (await repo.void_latest(TENANT_A, STAGE, KEY)).outcome == "skipped"
    # Another tenant's identical key is untouched by A's void.
    assert (await repo.void_latest(TENANT_B, STAGE, KEY)).outcome == "missing"


# ---------------------------------------------------------------------------
# Skipped sources
# ---------------------------------------------------------------------------


async def test_record_skip_counts_and_insert_clears_it(repo, make_candidate):
    assert await repo.record_skip(
        TENANT_A, stage=STAGE, source_key=KEY, error_type="gallons_not_positive", order_id="ORD-1"
    ) == 1
    assert await repo.record_skip(
        TENANT_A, stage=STAGE, source_key=KEY, error_type="gallons_not_positive"
    ) == 2
    await repo.record_skip(TENANT_A, stage=STAGE, source_key="order:ORD-2", error_type="x")
    await repo.record_skip(TENANT_B, stage=STAGE, source_key=KEY, error_type="x")
    assert (await repo.skipped_sources(TENANT_A))["count"] == 2

    await repo.write_record(TENANT_A, make_candidate(), "live")

    async with session_scope() as session:
        keys = sorted(
            (r.tenant_id, r.source_key, r.seen_count)
            for r in (await session.execute(select(MarginSkippedSourceORM))).scalars()
        )
    assert keys == [(TENANT_A, "order:ORD-2", 1), (TENANT_B, KEY, 1)]
    assert await repo.skipped_sources(TENANT_A) == {"count": 1, "sample": ["order:ORD-2"]}


async def test_skip_row_stores_no_values(repo):
    await repo.record_skip(TENANT_A, stage=STAGE, source_key=KEY, error_type="InvalidOperation")
    columns = {c.key for c in MarginSkippedSourceORM.__table__.columns}
    assert not {c for c in columns if "cents" in c or "micros" in c or "gallons" in c}


# ---------------------------------------------------------------------------
# Retry on a racing IntegrityError
# ---------------------------------------------------------------------------


def _unique_error() -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: margin_records"))


async def test_one_retry_on_unique_race(repo, make_candidate, monkeypatch):
    real = mr.MarginRepository._write_once
    calls = []

    async def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _unique_error()
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(mr.MarginRepository, "_write_once", flaky)
    result = await repo.write_record(TENANT_A, make_candidate(), "live")
    assert result.outcome == "inserted" and len(calls) == 2


async def test_second_race_gives_up_with_error(repo, make_candidate, monkeypatch, caplog):
    async def always(self, *args, **kwargs):
        raise _unique_error()

    monkeypatch.setattr(mr.MarginRepository, "_write_once", always)
    with caplog.at_level(logging.ERROR, logger=mr.__name__):
        result = await repo.write_record(TENANT_A, make_candidate(), "live")
    assert (result.written, result.outcome) == (False, "conflict")
    assert any("conflict after retry" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Database CHECK constraints (SQLite)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    ["cost_cents", "margin_cents", "product_cost_micros", "margin_per_gallon_micros",
     "landed_cost_micros"],
)
async def test_method_none_with_zero_cost_is_rejected(repo, make_missing_cost, column):
    """AC-8: a missing cost stored as 0 is refused by ck_mr_cost_null_iff_none."""

    with pytest.raises(IntegrityError) as exc:
        await repo.write_record(TENANT_A, make_missing_cost(**{column: 0}), "live")
    assert "CHECK constraint failed" in str(exc.value)
    assert await _rows() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "wac", "cost_cents": None},  # cost missing without method=none
        {"method": "override", "landed_cost_micros": None},
        {"method": "none"},  # none with cost fields set
        {"flag_missing_cost": True},  # missing flag on a costed record
    ],
    ids=["wac-null-cost", "override-null-landed", "none-with-cost", "flag-on-costed"],
)
async def test_cost_and_flag_checks(repo, make_candidate, overrides):
    with pytest.raises(IntegrityError):
        await repo.write_record(TENANT_A, make_candidate(**overrides), "live")


async def test_missing_flag_must_match_method_none(repo, make_missing_cost):
    with pytest.raises(IntegrityError):
        await repo.write_record(TENANT_A, make_missing_cost(flag_missing_cost=False), "live")
    with pytest.raises(IntegrityError):
        await repo.write_record(TENANT_A, make_missing_cost(margin_bp=0), "live")


async def test_zero_revenue_costed_record_may_have_null_margin_bp(repo, make_candidate):
    result = await repo.write_record(
        TENANT_A, make_candidate(revenue_cents=0, margin_cents=-250_000, margin_bp=None), "live"
    )
    assert result.record["margin_bp"] is None


async def test_alert_state_check(repo, make_candidate):
    await repo.write_record(TENANT_A, make_candidate(), "live")
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            row = (await session.execute(select(MarginRecordORM))).scalar_one()
            row.alert_state = "bogus"
            await session.flush()


_ENTRY = dict(
    entry_id="mce_x",
    tenant_id=TENANT_A,
    product_code="DIESEL_2",
    effective_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    unit_cost_micros=2_500_000,
    natural_key="nk",
    status="active",
    source="manual",
    created_by="admin@example.com",
)


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "purchase", "terminal_id": "T-1"},  # purchase without gallons
        {"kind": "purchase", "gallons_milli": 1000},  # purchase without terminal
        {"kind": "purchase", "gallons_milli": 1000, "terminal_id": "T-1",
         "effective_to": datetime(2026, 9, 2, tzinfo=timezone.utc)},
        {"kind": "override", "gallons_milli": 1000},
        {"kind": "override", "bol_id": "BOL-1"},
        {"kind": "override", "adder_type": "freight"},
        {"kind": "adder"},  # adder without adder_type
    ],
)
async def test_cost_entry_kind_fields_check(margin_engine, fields):
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(MarginCostEntryORM(**{**_ENTRY, **fields}))
            await session.flush()


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "purchase", "terminal_id": "T-1", "gallons_milli": 1000, "bol_id": "BOL-1"},
        {"kind": "override"},
        {"kind": "override", "terminal_id": "T-1",
         "effective_to": datetime(2026, 9, 2, tzinfo=timezone.utc)},
        {"kind": "adder", "adder_type": "fee"},
    ],
)
async def test_cost_entry_kind_fields_accepts_valid_rows(margin_engine, fields):
    async with session_scope() as session:
        session.add(MarginCostEntryORM(**{**_ENTRY, **fields}))
        await session.flush()


async def test_as_of_round_trips_as_utc(repo, make_candidate):
    result = await repo.write_record(TENANT_A, make_candidate(), "live")
    stored = await repo.get_record(TENANT_A, result.record["record_id"])
    assert stored["as_of"] == AS_OF and stored["as_of"].tzinfo is not None


async def test_naive_datetimes_are_refused(repo, make_candidate):
    with pytest.raises(ValueError):
        await repo.write_record(TENANT_A, make_candidate(as_of=datetime(2026, 10, 1)), "live")
