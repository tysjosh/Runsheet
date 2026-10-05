"""The asset ref loader resolves a real truck row in the PG document store (C3).

Real PostgreSQL required — see ``conftest.py``.
"""
from __future__ import annotations

from services.ref_loaders import ASSETS_INDEX, make_asset_loader


class _TrucksFacade:
    """Map the ``trucks`` index onto the per-test index; anything else is empty."""

    def __init__(self, store, index_name: str) -> None:
        self._store = store
        self._index = index_name

    async def search_documents(self, index, query, size=100, **kw):
        if index != ASSETS_INDEX:
            return {"hits": {"hits": [], "total": {"value": 0}}}
        return await self._store.search_documents(self._index, query, size, **kw)


async def test_loader_resolves_a_truck_by_truck_id(store, index_name):
    await store.index_document(
        index_name,
        "QA-TRUCK-01",
        {"truck_id": "QA-TRUCK-01", "tenant_id": "demo-tenant", "name": "QA Truck", "status": "active"},
    )
    load = make_asset_loader(_TrucksFacade(store, index_name))

    summary = await load("demo-tenant", "QA-TRUCK-01")
    assert summary is not None
    assert summary["name"] == "QA Truck"

    assert await load("other-tenant", "QA-TRUCK-01") is None
