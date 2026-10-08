"""Resolver tenant isolation (AC-24).

Tenants A and B share product codes, terminal ids, BOL ids, plan ids,
contract ids and rack ids. Resolving for A uses no B entry, BOL, contract or
rack row; every store call carries A's tenant filter and every entry read
is for A. A contract doc that comes back with B's ``tenant_id`` is dropped
and logged at ERROR.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from ._cost_basis_support import (
    AS_OF,
    T1,
    Harness,
    add_entry,
    bol_doc,
    contract_doc,
    days,
    plan_doc,
    rack_doc,
)
from ._margin_fakes import CountingEntries, FakeDocStore, tenant_of_query
from .conftest import TENANT_A, TENANT_B


def seed_both_tenants(store: FakeDocStore) -> None:
    lift = AS_OF - days(2)
    for tenant, contract_price, rack_price in ((TENANT_A, 2.10, 2.50), (TENANT_B, 1.10, 1.50)):
        store.add(
            "terminal_bols",
            bol_doc("BOL-1", lift, 1000.0, tenant=tenant, plan="PLAN-1"),
            bol_doc("BOL-2", lift, 1000.0, tenant=tenant, plan=None),
        )
        store.add("mvp_load_plans", plan_doc("PLAN-1", contract_id="C-1", tenant=tenant))
        store.add("supplier_contracts", contract_doc("C-1", contract_price, tenant=tenant))
        store.add("rack_prices", rack_doc("R-1", lift - timedelta(hours=1), rack_price, tenant=tenant))


@pytest.fixture
def h(repo) -> Harness:
    store = FakeDocStore()
    seed_both_tenants(store)
    return Harness(repo=repo, store=store, entries=CountingEntries(repo))


async def test_resolving_for_a_uses_only_a_data(h):
    t = AS_OF - days(1)
    await add_entry(h.repo, TENANT_B, "override", unit_cost_micros=1, effective_at=t, terminal=T1)
    await add_entry(h.repo, TENANT_B, "adder", unit_cost_micros=999_000, effective_at=t, adder_type="freight")
    await add_entry(h.repo, TENANT_B, "purchase", unit_cost_micros=1, effective_at=t, terminal=T1, gallons_milli=1_000_000)
    await add_entry(h.repo, TENANT_B, "purchase", unit_cost_micros=1, effective_at=t, terminal=T1, gallons_milli=1, bol_id="BOL-2")

    basis = await h.resolve(tenant=TENANT_A)

    assert basis.method == "wac"
    assert basis.adders_configured is False
    by_id = {lot["id"]: lot for lot in basis.lots}
    assert set(by_id) == {"BOL-1", "BOL-2"}
    assert (by_id["BOL-1"]["priced_by"], by_id["BOL-1"]["unit_cost_micros"]) == ("contract", 2_100_000)
    assert (by_id["BOL-2"]["priced_by"], by_id["BOL-2"]["unit_cost_micros"]) == ("rack", 2_500_000)
    assert basis.product_cost_micros == 2_300_000

    assert h.store.calls, "the resolver read the store"
    for index, body in h.store.calls:
        assert tenant_of_query(body) == TENANT_A, index
    assert h.entries.calls and {tenant for _, tenant in h.entries.calls} == {TENANT_A}


async def test_tenant_b_sees_its_own_values(h):
    basis = await h.resolve(tenant=TENANT_B)
    by_id = {lot["id"]: lot["unit_cost_micros"] for lot in basis.lots}
    assert by_id == {"BOL-1": 1_100_000, "BOL-2": 1_500_000}
    assert all(tenant_of_query(body) == TENANT_B for _, body in h.store.calls)


class TenantBlindContractStore(FakeDocStore):
    """Simulates a store bug: the contracts read ignores the tenant filter."""

    async def search_documents(self, index, body, size=100, request_timeout=10):
        if index != "supplier_contracts":
            return await super().search_documents(index, body, size, request_timeout)
        self.calls.append((index, body))
        wanted = set(body["query"]["bool"]["must"][0]["terms"]["contract_id"])
        hits = [
            {"_id": d["contract_id"], "_source": dict(d)}
            for d in self.indices.get(index, [])
            if d["contract_id"] in wanted and d["tenant_id"] == TENANT_B
        ]
        return {"hits": {"total": {"value": len(hits), "relation": "eq"}, "hits": hits}}


async def test_a_foreign_tenant_contract_doc_is_dropped_and_logged(repo, caplog):
    store = TenantBlindContractStore()
    seed_both_tenants(store)
    harness = Harness(repo=repo, store=store, entries=CountingEntries(repo))
    with caplog.at_level(logging.ERROR):
        basis = await harness.resolve(tenant=TENANT_A)
    lot = {lot["id"]: lot for lot in basis.lots}["BOL-1"]
    assert lot["priced_by"] == "rack"  # B's contract never prices A's lot
    assert lot["unit_cost_micros"] == 2_500_000
    assert basis.contract_ids == ()
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "another tenant" in r.getMessage()]
    assert errors and TENANT_B in errors[0].getMessage()
