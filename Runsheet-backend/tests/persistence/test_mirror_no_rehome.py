"""L1: the Postgres mirror never re-homes or overwrites another tenant's row.

The hybrid tables are keyed by a global id. Before the guard, an upsert by
tenant B on an id tenant A owned replaced A's ``document`` while the
``tenant_id`` column kept A, so a read-cutover deployment would serve B's body
to A. Each upsert now returns ``None`` with no write and no outbox event.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from persistence.database import session_scope
from persistence.models import OutboxEventORM
from persistence.repositories import (
    ArAgingSnapshotRepository,
    ComplianceConfigRepository,
    CurrentStateRepository,
    PricingRuleRepository,
)

A = "tenant-a"
B = "tenant-b"


def _current_state(aggregate, pk):
    repo = CurrentStateRepository(aggregate)

    async def upsert(session, doc):
        return await repo.upsert(session, doc=doc)

    return repo.model, pk, upsert, {"status": "active"}


def _compliance(aggregate, pk):
    repo = ComplianceConfigRepository(aggregate)

    async def upsert(session, doc):
        return await repo.upsert(session, doc=doc)

    return repo.model, pk, upsert, {"status": "active"}


def _ar_snapshot():
    from persistence.models import ArAgingSnapshotORM

    async def upsert(session, doc):
        return await ArAgingSnapshotRepository().upsert(session, doc=doc)

    return ArAgingSnapshotORM, "snapshot_id", upsert, {
        "snapshot_date": "2026-10-01",
        "total_open_cents": 100,
    }


def _pricing_rule():
    from persistence.models import PricingRuleORM

    async def upsert(session, doc):
        return await PricingRuleRepository().upsert(session, rule=doc)

    return PricingRuleORM, "rule_id", upsert, {
        "price_book_id": "pb_1",
        "product_code": "DSL",
        "scope_type": "default",
        "scope_value": "*",
        "unit_price_cents": 300,
    }


CASES = {
    "customer_tank": lambda: _current_state("customer_tank", "customer_tank_id"),
    "terminal": lambda: _current_state("terminal", "terminal_id"),
    "intake_channel": lambda: _current_state("intake_channel", "channel_id"),
    "supplier_contract": lambda: _compliance("supplier_contract", "contract_id"),
    "ar_aging_snapshot": _ar_snapshot,
    "pricing_rule": _pricing_rule,
}


def _doc(pk, tenant, extra, marker):
    return {pk: "X", "tenant_id": tenant, "name": marker, **extra}


async def _snapshot(model, pk):
    async with session_scope() as s:
        row = (
            await s.execute(select(model).where(getattr(model, pk) == "X"))
        ).scalar_one()
        cols = {c.name: getattr(row, c.key) for c in model.__table__.columns}
        cols.pop("updated_at", None)
        return cols


async def _outbox_tenants():
    async with session_scope() as s:
        rows = (await s.execute(select(OutboxEventORM))).scalars().all()
    return [r.tenant_id for r in rows]


@pytest.mark.parametrize("case", sorted(CASES))
async def test_other_tenant_upsert_is_refused(engine, case):
    model, pk, upsert, extra = CASES[case]()
    async with session_scope() as s:
        await upsert(s, _doc(pk, A, extra, "a-body"))
    before = await _snapshot(model, pk)

    async with session_scope() as s:
        result = await upsert(s, _doc(pk, B, extra, "b-body"))

    assert result is None
    assert await _snapshot(model, pk) == before
    assert before["tenant_id"] == A
    assert B not in await _outbox_tenants()


@pytest.mark.parametrize("case", sorted(CASES))
async def test_same_tenant_upsert_updates(engine, case):
    model, pk, upsert, extra = CASES[case]()
    async with session_scope() as s:
        await upsert(s, _doc(pk, A, extra, "a-body"))
    changed = dict(extra)
    if "status" in changed:
        changed["status"] = "inactive"
    elif "total_open_cents" in changed:
        changed["total_open_cents"] = 999
    else:
        changed["unit_price_cents"] = 999
    async with session_scope() as s:
        assert await upsert(s, _doc(pk, A, changed, "a-v2")) is not None
    after = await _snapshot(model, pk)
    assert after["tenant_id"] == A
    assert after != {}
    if "document" in after:
        assert after["document"]["name"] == "a-v2"
    assert await _outbox_tenants() == [A, A]


async def test_unknown_current_state_row_is_adoptable(engine):
    repo = CurrentStateRepository("truck")
    async with session_scope() as s:
        await repo.upsert(s, doc={"truck_id": "T1", "status": "active"})
    async with session_scope() as s:
        row = await repo.upsert(
            s, doc={"truck_id": "T1", "tenant_id": A, "status": "inactive"}
        )
        assert row is not None
        assert row.document["status"] == "inactive"
