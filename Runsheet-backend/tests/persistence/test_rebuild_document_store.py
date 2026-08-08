"""Tests for the rebuild-from-Postgres tool (document-plane drift repair).

The rebuild tool reconstructs an index's documents from the relational
source-of-truth by running each row through the SAME projector the relay uses and
writing the result VERBATIM (no ``updated_at`` rewrite). These tests record every
write so they can assert the rebuilt documents match what the projector produced,
without needing both databases in one test — the relational side is the suite's
SQLite ``engine`` fixture.

Formerly ``test_rebuild_from_postgres.py``, where the recorder was a fake raw
Elasticsearch client with a fake ``indices`` namespace. Phase 6 deleted that
client, so the recorder is now the store facade's ``index_document``.

One test went with the cluster: ``test_rebuild_recreates_missing_index_with_mapping``
asserted the tool created a missing index with its declared mapping first, because a
dynamically mapped index typed ``tenant_id`` as ``text`` and every tenant-scoped
``term`` query then matched nothing. The document store is one table keyed
``(index_name, doc_id)`` — there is no index to create and no per-index typing — so
the failure mode it guarded is structurally impossible rather than merely untested.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from persistence.database import session_scope
from persistence.repositories import CurrentStateRepository
from persistence import rebuild_document_store as rds

TENANT = "demo-tenant"


class _Recorder:
    """Stands in for the store facade's ``index_document``."""

    def __init__(self) -> None:
        self.written: List[Tuple[str, str, Dict[str, Any]]] = []
        self.stamped: List[bool] = []

    async def __call__(self, index, doc_id, document, *, stamp_timestamps=True):
        self.written.append((index, doc_id, dict(document)))
        self.stamped.append(stamp_timestamps)
        return {"result": "created"}


@pytest.fixture
def recorder(monkeypatch):
    import services.elasticsearch_service as es_mod

    rec = _Recorder()
    monkeypatch.setattr(
        es_mod.elasticsearch_service, "index_document", rec, raising=True
    )
    return rec


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


async def test_rebuild_writes_verbatim_documents(engine, recorder):
    await _seed_channel("CHAN-1")
    await _seed_channel("CHAN-2")

    n = await rds.rebuild("intake_channel", TENANT)
    assert n == 2

    written = {doc_id: body for (_idx, doc_id, body) in recorder.written}
    assert set(written) == {"CHAN-1", "CHAN-2"}
    # Verbatim: the stored updated_at is preserved (NOT rewritten to now()).
    assert written["CHAN-1"]["updated_at"] == "2026-01-02T03:04:05+00:00"
    assert written["CHAN-1"]["display_name"] == "Chan CHAN-1"
    # All writes target the intake_channels index.
    assert all(idx == "intake_channels" for (idx, _id, _b) in recorder.written)


async def test_rebuild_opts_out_of_timestamp_stamping(engine, recorder):
    """The verbatim guarantee is one keyword argument, so pin it directly.

    Without ``stamp_timestamps=False`` the store overwrites ``updated_at`` with
    now() on every write, and a rebuild would silently restamp the whole index —
    the assertion above would still pass, because it inspects the document handed
    to the store rather than what the store does with it.
    """
    await _seed_channel("CHAN-STAMP")

    await rds.rebuild("intake_channel", TENANT)

    assert recorder.stamped == [False]


async def test_rebuild_dry_run_writes_nothing(engine, recorder):
    await _seed_channel("CHAN-DRY")
    n = await rds.rebuild("intake_channel", TENANT, dry_run=True)
    assert n == 1
    assert recorder.written == []


async def test_rebuild_tenant_scoped(engine, recorder):
    await _seed_channel("MINE")
    await _seed_channel("THEIRS", tenant_id="other-tenant")
    await rds.rebuild("intake_channel", TENANT)
    ids = {doc_id for (_i, doc_id, _b) in recorder.written}
    assert ids == {"MINE"}


async def test_rebuild_rejects_unknown_aggregate(engine, recorder):
    with pytest.raises(ValueError):
        await rds.rebuild("not_an_aggregate", TENANT)
