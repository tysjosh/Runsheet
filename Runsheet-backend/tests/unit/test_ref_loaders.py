"""Unit tests for the asset loader in ``services/ref_loaders.py`` (finding C3).

The loader read an ``assets`` index that does not exist in the document store,
so every asset certification failed with ``asset_not_found``. It must read the
``trucks`` index, where real trucks live.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.ref_loaders import ASSETS_INDEX, make_asset_loader


def _es_with(hit_source):
    es = MagicMock()
    hits = [{"_source": hit_source}] if hit_source is not None else []
    es.search_documents = AsyncMock(return_value={"hits": {"hits": hits}})
    return es


def test_assets_index_is_trucks():
    assert ASSETS_INDEX == "trucks"


@pytest.mark.asyncio
async def test_loader_reads_trucks_and_returns_summary():
    es = _es_with(
        {"truck_id": "QA-TRUCK-01", "tenant_id": "demo-tenant", "name": "Truck 1", "status": "active"}
    )
    summary = await make_asset_loader(es)("demo-tenant", "QA-TRUCK-01")

    assert es.search_documents.await_args.args[0] == "trucks"
    assert summary == {"asset_id": "QA-TRUCK-01", "name": "Truck 1", "status": "active"}


@pytest.mark.asyncio
async def test_loader_rejects_a_hit_from_another_tenant():
    es = _es_with({"truck_id": "QA-TRUCK-01", "tenant_id": "other-tenant", "name": "x"})
    assert await make_asset_loader(es)("demo-tenant", "QA-TRUCK-01") is None


@pytest.mark.asyncio
async def test_loader_returns_none_without_a_hit():
    assert await make_asset_loader(_es_with(None))("demo-tenant", "QA-NOPE") is None
