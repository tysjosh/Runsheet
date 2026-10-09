"""OI-31: ``update_job_fields`` mirrors the merged job, not the caller's snapshot.

A caller reads the job, another writer changes ``cargo_manifest``, then the
caller writes its own fields. The store merges both; the Postgres mirror used
to receive the caller's stale snapshot plus its fields, which reverted the
concurrent cargo change in the row that ``GET /jobs/{id}`` reads.
"""
from unittest.mock import AsyncMock, patch

import pytest

from scheduling.services import job_writes
from scheduling.services.scheduling_es_mappings import JOBS_CURRENT_INDEX

pytestmark = pytest.mark.asyncio


class _Store:
    def __init__(self, docs):
        self.docs = {key: dict(value) for key, value in docs.items()}

    async def update_document(self, index, doc_id, partial):
        self.docs[(index, doc_id)].update(partial)
        return {"result": "updated"}

    async def get_document(self, index, doc_id):
        doc = self.docs.get((index, doc_id))
        return dict(doc) if doc is not None else None


def _job(**overrides):
    doc = {
        "job_id": "JOB_1",
        "tenant_id": "tenant-a",
        "status": "scheduled",
        "cargo_manifest": [{"item_id": "c1", "item_status": "pending"}],
    }
    doc.update(overrides)
    return doc


async def test_mirror_carries_a_concurrent_cargo_change():
    store = _Store({(JOBS_CURRENT_INDEX, "JOB_1"): _job()})
    snapshot = await store.get_document(JOBS_CURRENT_INDEX, "JOB_1")
    # Another writer updates the cargo after the caller's read.
    concurrent = [{"item_id": "c1", "item_status": "loaded"}]
    store.docs[(JOBS_CURRENT_INDEX, "JOB_1")]["cargo_manifest"] = concurrent

    mirror = AsyncMock()
    with patch.object(job_writes, "mirror_job", mirror):
        merged = await job_writes.update_job_fields(
            store, "JOB_1", {"status": "assigned"}, job_doc=snapshot
        )

    mirrored, job_id = mirror.await_args.args
    assert job_id == "JOB_1"
    assert mirrored["cargo_manifest"] == concurrent
    assert mirrored["status"] == "assigned"
    # The caller's dict is updated in place; callers read it afterwards.
    assert snapshot is merged
    assert snapshot["cargo_manifest"] == concurrent
    assert snapshot["status"] == "assigned"


async def test_empty_read_back_falls_back_to_snapshot_plus_fields():
    store = _Store({(JOBS_CURRENT_INDEX, "JOB_1"): _job()})
    store.get_document = AsyncMock(return_value=None)
    snapshot = _job()

    mirror = AsyncMock()
    with patch.object(job_writes, "mirror_job", mirror):
        merged = await job_writes.update_job_fields(
            store, "JOB_1", {"status": "assigned"}, job_doc=snapshot
        )

    assert merged["status"] == "assigned"
    assert mirror.await_args.args[0]["status"] == "assigned"


async def test_no_snapshot_and_no_read_back_mirrors_nothing():
    store = _Store({(JOBS_CURRENT_INDEX, "JOB_1"): _job()})
    store.get_document = AsyncMock(return_value=None)

    mirror = AsyncMock()
    with patch.object(job_writes, "mirror_job", mirror):
        merged = await job_writes.update_job_fields(store, "JOB_1", {"status": "x"})

    assert merged == {}
    mirror.assert_not_awaited()
