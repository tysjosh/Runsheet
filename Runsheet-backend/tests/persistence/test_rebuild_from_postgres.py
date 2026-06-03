"""Tests for the rebuild-from-Postgres tool (reversibility safety net).

The rebuild tool reconstructs an ES index from the PG source-of-truth by
running each row through the SAME projector the relay uses and indexing the
result VERBATIM (no updated_at rewrite). These tests use a fake ES client that
records every index() call so we can assert the rebuilt documents match what
the projector produced — without an external ES.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from persistence.database import session_scope
from persistence.repositories import CurrentStateRepository
from persistence import rebuild_from_postgres as rfp

TENANT = "demo-tenant"


class _FakeIndices:
    def __init__(self):
        self.created = []
        self._exists = set()

    def exists(self, index):
        return index in self._exists

    def create(self, index, body=None):
        self.created.append(index)
        self._exists.add(index)

    def refresh(self, index):
        pass


class _FakeClient:
    def __init__(self):
        self.indices = _FakeIndices()
        self.indexed = []  # (index, id, body)

    def index(self, index, id, body, refresh=False):
        self.indexed.append((index, id, dict(body)))
        return {"result": "created"}


@pytest.fixture
def fake_es(monkeypatch):
    """Patch the shared elasticsearch_service with a recording fake client."""
    client = _FakeClient()
    import services.elasticsearch_service as es_mod
    monkeypatch.setattr(es_mod.elasticsearch_service, "client", client, raising=False)
    return client


async def _seed_channel(channel_id, **over):
    doc = {
        "channel_id": channel_id, "tenant_id": TENANT,
        "channel_type": "api_partner", "display_name": f"Chan {channel_id}",
        "enabled": True, "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T03:04:05+00:00",
    }
    doc.update(over)
    async with session_scope() as s:
        await CurrentStateRepository("intake_channel").upsert(s, doc=doc)


async def test_rebuild_indexes_verbatim_documents(engine, fake_es):
    await _seed_channel("CHAN-1")
    await _seed_channel("CHAN-2")

    n = await rfp.rebuild("intake_channel", TENANT)
    assert n == 2

    indexed = {doc_id: body for (_idx, doc_id, body) in fake_es.indexed}
    assert set(indexed) == {"CHAN-1", "CHAN-2"}
    # Verbatim: the stored updated_at is preserved (NOT rewritten to now()).
    assert indexed["CHAN-1"]["updated_at"] == "2026-01-02T03:04:05+00:00"
    assert indexed["CHAN-1"]["display_name"] == "Chan CHAN-1"
    # All writes target the intake_channels index.
    assert all(idx == "intake_channels" for (idx, _id, _b) in fake_es.indexed)


async def test_rebuild_recreates_missing_index_with_mapping(engine, fake_es):
    await _seed_channel("CHAN-9")
    # Index does not exist yet → rebuild should create it.
    await rfp.rebuild("intake_channel", TENANT)
    assert "intake_channels" in fake_es.indices.created


async def test_rebuild_dry_run_indexes_nothing(engine, fake_es):
    await _seed_channel("CHAN-DRY")
    n = await rfp.rebuild("intake_channel", TENANT, dry_run=True)
    assert n == 1
    assert fake_es.indexed == []


async def test_rebuild_tenant_scoped(engine, fake_es):
    await _seed_channel("MINE")
    await _seed_channel("THEIRS", tenant_id="other-tenant")
    await rfp.rebuild("intake_channel", TENANT)
    ids = {doc_id for (_i, doc_id, _b) in fake_es.indexed}
    assert ids == {"MINE"}


async def test_rebuild_rejects_unknown_aggregate(engine, fake_es):
    with pytest.raises(ValueError):
        await rfp.rebuild("not_an_aggregate", TENANT)
