"""Scoped readers put both ids in every store call (design §2.4; ISO-C-3 /
T-SCOPE-ARGS)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from auth.test_auth import issue_test_context
from portal.api._authz import PortalScope
from portal.services.scoped_readers import (
    PortalForecastReader,
    PortalOrderReader,
    PortalTankReader,
    decode_cursor,
    encode_cursor,
)
from tests.portal.conftest import CUSTOMER_A, T1

SCOPE = PortalScope(T1, CUSTOMER_A, "st-user-1",
                    issue_test_context(T1, roles=["customer"], customer_id=CUSTOMER_A))


class _OrderRepoSpy:
    def __init__(self):
        self.calls = []

    async def search(self, tenant_id, **kwargs):
        self.calls.append(("search", tenant_id, kwargs))
        return {"orders": [], "raw_count": 0, "last_key": None}

    async def get(self, tenant_id, order_id):
        self.calls.append(("get", tenant_id, {"order_id": order_id}))
        return SimpleNamespace(order_id=order_id, customer_id="OTHER")


class _TankRepoSpy:
    def __init__(self):
        self.calls = []

    async def list_for_tenant(self, tenant_id, **kwargs):
        self.calls.append(("list_for_tenant", tenant_id, kwargs))
        return []

    async def get(self, tenant_id, tank_id):
        self.calls.append(("get", tenant_id, {"customer_tank_id": tank_id}))
        return None


class _ESSpy:
    def __init__(self):
        self.calls = []

    async def search_documents(self, index, query, size):
        self.calls.append((index, query, size))
        return {"hits": {"hits": []}}


async def test_store_calls_carry_customer_id():
    orders, tanks, es = _OrderRepoSpy(), _TankRepoSpy(), _ESSpy()
    o, t, f = PortalOrderReader(orders), PortalTankReader(tanks), PortalForecastReader(es)
    now = datetime.now(timezone.utc)

    await o.list(SCOPE, limit=10)
    await o.latest_for_tank(SCOPE, "QA-TANK-1")
    await o.next_deliveries(SCOPE, statuses=["placed"])
    await o.next_deliveries(SCOPE, statuses=["placed"], customer_tank_id="QA-TANK-1")
    await o.deliveries(SCOPE, "QA-TANK-1", limit=10, now=now)
    assert await o.get_or_none(SCOPE, "QA-ORD-1") is None  # another customer's
    await t.list(SCOPE)
    await f.latest_by_tank(SCOPE)

    searches = [c for c in orders.calls if c[0] == "search"]
    assert len(searches) == 5
    for _op, tenant_id, kwargs in searches:
        assert tenant_id == SCOPE.tenant_id
        assert kwargs["customer_id"] == SCOPE.customer_id
    (_op, tenant_id, kwargs), = tanks.calls
    assert tenant_id == SCOPE.tenant_id and kwargs["customer_id"] == SCOPE.customer_id
    assert kwargs["status"] == "active"
    (index, query, _size), = es.calls
    assert index == "mvp_tank_forecasts"
    must = query["query"]["bool"]["must"]
    assert {"term": {"tenant_id": T1}} in must
    assert {"term": {"customer_id": CUSTOMER_A}} in must


@pytest.mark.parametrize("bad", [
    SimpleNamespace(tenant_id=T1, customer_id=""),
    SimpleNamespace(tenant_id=T1, customer_id="  "),
    SimpleNamespace(tenant_id="", customer_id=CUSTOMER_A),
    SimpleNamespace(tenant_id=T1),
])
async def test_reader_refuses_scope_without_ids(bad):
    orders, tanks, es = _OrderRepoSpy(), _TankRepoSpy(), _ESSpy()
    for call in (
        lambda: PortalOrderReader(orders).list(bad, limit=5),
        lambda: PortalOrderReader(orders).get_or_none(bad, "x"),
        lambda: PortalTankReader(tanks).list(bad),
        lambda: PortalTankReader(tanks).get(bad, "x"),
        lambda: PortalForecastReader(es).latest_by_tank(bad),
    ):
        with pytest.raises(ValueError):
            await call()
    assert orders.calls == tanks.calls == es.calls == []


def test_cursor_round_trip_and_rejection():
    from errors.exceptions import AppException

    cursor = encode_cursor(("2026-10-08T00:00:00+00:00", "ord_1"))
    assert decode_cursor(cursor) == ("2026-10-08T00:00:00+00:00", "ord_1")
    assert decode_cursor(None) is None
    for bad in ("%%%", encode_cursor(["only-one"]), "e30"):
        with pytest.raises(AppException) as err:
            decode_cursor(bad)
        assert err.value.status_code == 422


async def test_repository_search_filters_on_both_paths(monkeypatch):
    """customer_tank_id / statuses / hold_reason reach the ES query and the
    Postgres read_hybrid_search call (design §2.4)."""
    import commerce.services.commerce_persistence_bridge as bridge
    from fuel.order_repository import FuelOrderRepository

    es = _ESSpy()
    repo = FuelOrderRepository(es)
    kwargs = dict(customer_id=CUSTOMER_A, customer_tank_id="QA-TANK-1",
                  statuses=["placed", "scheduled"], hold_reason="awaiting_dispatcher_confirmation")

    await repo.search(T1, **kwargs)
    (_index, query, _size), = es.calls
    text = repr(query)
    assert "{'term': {'customer_tank_id': 'QA-TANK-1'}}" in text
    assert "{'terms': {'status': ['placed', 'scheduled']}}" in text
    assert "{'term': {'hold_reason': 'awaiting_dispatcher_confirmation'}}" in text

    seen = {}

    async def _pg(aggregate_type, tenant_id, **kw):
        seen.update(kw)
        return {"items": [], "total": 0, "page": 1, "size": 20}

    monkeypatch.setattr(bridge, "read_hybrid_search", _pg)
    await repo.search(T1, **kwargs)
    assert seen["term_filters"]["customer_tank_id"] == "QA-TANK-1"
    assert seen["term_filters"]["hold_reason"] == "awaiting_dispatcher_confirmation"
    assert seen["term_filters"]["customer_id"] == CUSTOMER_A
    assert seen["in_filters"] == {"status": ["placed", "scheduled"]}

    seen.clear()
    await repo.search(T1, customer_id=CUSTOMER_A)
    assert seen["in_filters"] is None


async def test_transition_if_compares_status_and_hold_reason():
    from fuel.order_repository import FuelOrderRepository
    from tests.unit._loading_plan_fakes import ORDERS, InMemoryDocumentStore, fuel_order_doc

    store = InMemoryDocumentStore()
    store.seed(ORDERS, "o1", fuel_order_doc("o1", tenant_id=T1, status="on_hold", hold_reason="r1"))
    repo = FuelOrderRepository(store)
    upd = {"status": "placed", "hold_reason": None}
    assert await repo.transition_if("other", "o1", expected_status="on_hold", update_fields=upd) is None
    assert await repo.transition_if(T1, "o1", expected_status="placed", update_fields=upd) is None
    assert await repo.transition_if(T1, "o1", expected_status="on_hold",
                                    expected_hold_reason="r2", update_fields=upd) is None
    assert await repo.transition_if(T1, "missing", expected_status="on_hold", update_fields=upd) is None
    assert store.doc(ORDERS, "o1")["status"] == "on_hold"
    doc = await repo.transition_if(T1, "o1", expected_status="on_hold",
                                   expected_hold_reason="r1", update_fields=upd)
    assert doc["status"] == "placed" and doc["hold_reason"] is None
    assert store.doc(ORDERS, "o1")["status"] == "placed"
