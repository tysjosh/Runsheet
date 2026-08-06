"""Auto-stamped ``created_at`` / ``updated_at``, against the real store.

Moved from ``tests/unit/test_time_utils.py``, where five tests asserted this by
inspecting the body handed to a stubbed Elasticsearch client. Phase 6 deleted that
client, and the stamping had already moved into
:meth:`persistence.document_store.PostgresDocumentStore.index_document` — so the
assertions had to move with it or the behaviour would have gone untested. It was not
covered at the store layer at all: the Postgres suite tested the timestamp
COMPARISON in ``upsert_if_newer`` and never the stamping itself.

Three properties, each with a reason to exist:

* the values are timezone-aware UTC with no ``Z+00:00`` double suffix — a leak that
  produced unparseable timestamps once before;
* ``created_at`` survives, so re-indexing a document does not reset its age;
* the event-stream indices in ``TIMESTAMP_SKIP_INDICES`` are left alone, because
  they carry their own domain timestamps. On Elasticsearch a strict mapping rejected
  the auto-stamped fields outright; jsonb would accept them silently, which makes
  this the one of the three that got *easier* to break and therefore more worth
  pinning.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.elasticsearch_service import TIMESTAMP_SKIP_INDICES

TENANT = "demo-tenant"


def _assert_utc_iso(value: str) -> datetime:
    """Parse and assert the string is tz-aware UTC, without a doubled suffix."""
    assert not value.endswith("Z+00:00"), f"doubled timezone suffix: {value!r}"
    assert value.endswith("+00:00"), f"not an explicit UTC offset: {value!r}"
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    return parsed


class TestIndexDocumentStamping:
    async def test_it_stamps_tz_aware_utc_timestamps(self, store, index_name):
        await store.index_document(
            index_name, "T-1", {"truck_id": "T-1", "tenant_id": TENANT}
        )

        stored = await store.get_document(index_name, "T-1")

        _assert_utc_iso(stored["updated_at"])
        _assert_utc_iso(stored["created_at"])

    async def test_it_preserves_an_existing_created_at(self, store, index_name):
        """Re-indexing must not reset a document's age.

        ``updated_at`` is always rewritten; ``created_at`` only when absent. Getting
        this backwards makes every document look newly created on any write.
        """
        original = "2024-01-01T00:00:00+00:00"
        await store.index_document(
            index_name,
            "T-1",
            {"truck_id": "T-1", "tenant_id": TENANT, "created_at": original},
        )

        stored = await store.get_document(index_name, "T-1")

        assert stored["created_at"] == original
        assert stored["updated_at"] != original

    async def test_it_stamps_the_callers_dict_too(self, store, index_name):
        """Several callers read the timestamps back off the dict they passed in.

        The Elasticsearch path mutated the caller's document, so the store does the
        same deliberately — returning a copy instead would silently give those
        callers ``None``.
        """
        document = {"truck_id": "T-1", "tenant_id": TENANT}

        await store.index_document(index_name, "T-1", document)

        assert "updated_at" in document
        assert "created_at" in document

    @pytest.mark.parametrize("skipped_index", sorted(TIMESTAMP_SKIP_INDICES))
    async def test_it_skips_the_event_stream_indices(self, store, skipped_index):
        """``job_events`` / ``shipment_events`` / ``fuel_order_events`` carry their
        own domain timestamps.

        On Elasticsearch a strict mapping rejected the auto-stamped fields, so a
        regression here was a hard write failure. jsonb accepts them quietly, so this
        assertion is now the only thing standing between the skip list and a silent
        extra field on every event.
        """
        document = {"event_id": "e1", "tenant_id": TENANT}

        await store.index_document(skipped_index, "e1", document)

        stored = await store.get_document(skipped_index, "e1")
        assert "updated_at" not in stored
        assert "created_at" not in stored
        # Cleanup: these use the real index names, not the per-test one.
        await store.delete_document(skipped_index, "e1")


class TestUpdateDocumentStamping:
    async def test_it_stamps_updated_at_on_a_partial_update(self, store, index_name):
        await store.index_document(
            index_name, "T-1", {"truck_id": "T-1", "tenant_id": TENANT}
        )
        first = (await store.get_document(index_name, "T-1"))["updated_at"]

        await store.update_document(index_name, "T-1", {"plate_number": "KAA-001"})

        stored = await store.get_document(index_name, "T-1")
        _assert_utc_iso(stored["updated_at"])
        assert stored["plate_number"] == "KAA-001"
        assert stored["updated_at"] >= first
