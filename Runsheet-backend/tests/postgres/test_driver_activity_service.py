"""DriverActivityService over the real store (G1, D13).

The dispatcher read merges ``job_messages`` and ``driver_exceptions``. Its
queries use a ``bool.should`` (driver_id OR sender_id), a string ``range`` on
``timestamp`` and a ``sort`` on it, all inside ``inject_tenant_filter``. Those
are translator paths, so they run against real PostgreSQL — see
``conftest.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from errors.exceptions import AppException

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


class _RoutedFacade:
    """Route each activity store to its own per-test index."""

    def __init__(self, store, index_name: str) -> None:
        self._store = store
        self._index = index_name

    def route(self, index: str) -> str:
        return f"{self._index}__{index}"

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100, **kw):
        return await self._store.search_documents(self.route(index), query, size)


def _message(mid, ts, *, tenant=TENANT_A, job_id="JOB-A", sender_id="DRV-A", driver_id=None, order_id=None):
    doc = {
        "message_id": mid,
        "sender_id": sender_id,
        "sender_role": "driver",
        "body": f"body {mid}",
        "timestamp": ts,
        "tenant_id": tenant,
    }
    if job_id:
        doc["job_id"] = job_id
    if order_id:
        doc["order_id"] = order_id
    if driver_id:
        doc["driver_id"] = driver_id
    return doc


def _exception(eid, ts, *, tenant=TENANT_A, job_id="JOB-A", driver_id="DRV-A"):
    return {
        "exception_id": eid,
        "job_id": job_id,
        "order_id": None,
        "driver_id": driver_id,
        "exception_type": "delay",
        "severity": "low",
        "note": f"note {eid}",
        "timestamp": ts,
        "tenant_id": tenant,
    }


def _ts(hour: int) -> str:
    return datetime(2026, 10, 1, hour, 0, 0, 123456, tzinfo=timezone.utc).isoformat()


@pytest.fixture
async def seeded(store, index_name):
    from driver.services.driver_activity_service import DriverActivityService

    facade = _RoutedFacade(store, index_name)
    messages = facade.route("job_messages")
    exceptions = facade.route("driver_exceptions")
    rows = [
        (messages, _message("m1", _ts(8))),
        (messages, _message("m2", _ts(10))),
        # Order-keyed thread: carries driver_id, no job_id.
        (messages, _message("m3", _ts(12), job_id=None, order_id="ORD-1", driver_id="DRV-A", sender_id="DRV-A")),
        (exceptions, _exception("e1", _ts(9))),
        (exceptions, _exception("e2", _ts(11))),
        # Tenant B reuses the same job and driver ids.
        (messages, _message("mB", _ts(13), tenant=TENANT_B)),
        (exceptions, _exception("eB", _ts(14), tenant=TENANT_B)),
    ]
    for index, doc in rows:
        doc_id = doc.get("message_id") or doc.get("exception_id")
        await store.index_document(index, doc_id, doc)
    return DriverActivityService(facade)


async def test_job_timeline_is_merged_newest_first(seeded):
    result = await seeded.list_for_job(TENANT_A, "JOB-A")

    assert [row["id"] for row in result["items"]] == ["e2", "m2", "e1", "m1"]
    assert result["total"] == 4


async def test_driver_timeline_matches_driver_id_or_sender_id(seeded):
    result = await seeded.list_for_driver(TENANT_A, "DRV-A")

    assert [row["id"] for row in result["items"]] == ["m3", "e2", "m2", "e1", "m1"]
    assert result["total"] == 5


async def test_another_tenants_rows_are_never_returned(seeded):
    by_job = await seeded.list_for_job(TENANT_A, "JOB-A", size=100)
    by_driver = await seeded.list_for_driver(TENANT_A, "DRV-A", size=100)

    ids = {row["id"] for row in by_job["items"] + by_driver["items"]}
    assert "mB" not in ids and "eB" not in ids

    tenant_b = await seeded.list_for_job(TENANT_B, "JOB-A")
    assert [row["id"] for row in tenant_b["items"]] == ["eB", "mB"]


async def test_type_filter(seeded):
    only_exceptions = await seeded.list_for_job(TENANT_A, "JOB-A", types=["exception"])
    only_messages = await seeded.list_for_driver(TENANT_A, "DRV-A", types=["message"])

    assert [row["id"] for row in only_exceptions["items"]] == ["e2", "e1"]
    assert only_exceptions["total"] == 2
    assert [row["id"] for row in only_messages["items"]] == ["m3", "m2", "m1"]


async def test_date_range_is_inclusive(seeded):
    result = await seeded.list_for_job(
        TENANT_A,
        "JOB-A",
        start_date=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
        end_date=datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc),
    )

    assert [row["id"] for row in result["items"]] == ["m2", "e1"]
    assert result["total"] == 2


async def test_naive_bounds_are_read_as_utc(seeded):
    result = await seeded.list_for_job(
        TENANT_A, "JOB-A", start_date=datetime(2026, 10, 1, 10, 30)
    )

    assert [row["id"] for row in result["items"]] == ["e2"]


async def test_pagination_totals(seeded):
    page1 = await seeded.list_for_driver(TENANT_A, "DRV-A", page=1, size=2)
    page2 = await seeded.list_for_driver(TENANT_A, "DRV-A", page=2, size=2)
    page3 = await seeded.list_for_driver(TENANT_A, "DRV-A", page=3, size=2)

    assert [row["id"] for row in page1["items"]] == ["m3", "e2"]
    assert [row["id"] for row in page2["items"]] == ["m2", "e1"]
    assert [row["id"] for row in page3["items"]] == ["m1"]
    assert page1["total"] == page2["total"] == page3["total"] == 5


async def test_page_past_the_window_is_refused(seeded):
    with pytest.raises(AppException) as exc_info:
        await seeded.list_for_job(TENANT_A, "JOB-A", page=11, size=100)

    assert exc_info.value.status_code == 422
