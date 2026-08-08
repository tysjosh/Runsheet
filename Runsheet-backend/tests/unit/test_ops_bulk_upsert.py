"""``OpsElasticsearchService.bulk_upsert``, which had no test and no caller.

It built ``elasticsearch.helpers.bulk`` actions carrying a fourth transcription of
the out-of-order painless script and handed ``self.client`` straight to the helper.
That made it the last application write bypassing the document-store backend
switch, and it was invisible to the raw-client inventory because the client was
passed as an argument rather than called as ``.client.bulk(...)``. So the inventory
reported the application data plane as clean while every batched shipment and rider
would still have gone to Elasticsearch after the cutover.

It is now N facade calls. Tests exist because the method is uncalled: nothing else
would notice it breaking, and the reason it drifted to a fourth copy of the script
in the first place is that nothing exercised it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from ops.services.ops_es_service import OpsElasticsearchService


class _RecordingFacade:
    """Records facade calls and answers ``upsert_if_newer`` from a script."""

    def __init__(self, *, applied: bool = True, fail_on: Tuple[str, ...] = ()) -> None:
        self.upserts: List[Tuple[str, str, Dict[str, Any]]] = []
        self.indexed: List[Tuple[str, str, Dict[str, Any]]] = []
        self._applied = applied
        self._fail_on = fail_on
        self.client = None

    async def upsert_if_newer(self, index, doc_id, document, **_):
        if doc_id in self._fail_on:
            raise RuntimeError(f"boom for {doc_id}")
        self.upserts.append((index, doc_id, document))
        return self._applied

    async def index_document(self, index, doc_id, document):
        if doc_id in self._fail_on:
            raise RuntimeError(f"boom for {doc_id}")
        self.indexed.append((index, doc_id, document))
        return {"result": "created"}


@pytest.fixture
def facade() -> _RecordingFacade:
    return _RecordingFacade()


@pytest.fixture
def ops(facade: _RecordingFacade) -> OpsElasticsearchService:
    return OpsElasticsearchService(facade)


class TestBulkUpsertRoutesThroughTheFacade:
    async def test_a_shipment_goes_through_upsert_if_newer(self, ops, facade):
        """Not ``index_document``: the out-of-order guard has to be kept.

        At-least-once delivery means an older event can arrive after a newer one,
        and a plain index would move the shipment backwards.
        """
        result = await ops.bulk_upsert(
            [{"action": "upsert_shipment", "doc": {"shipment_id": "s1"}}]
        )

        assert result["successful"] == 1
        assert facade.upserts == [(ops.SHIPMENTS_CURRENT, "s1", {"shipment_id": "s1"})]
        assert facade.indexed == []

    async def test_a_rider_goes_to_the_riders_index(self, ops, facade):
        await ops.bulk_upsert([{"action": "upsert_rider", "doc": {"rider_id": "r1"}}])

        assert facade.upserts[0][0] == ops.RIDERS_CURRENT
        assert facade.upserts[0][1] == "r1"

    async def test_an_event_is_indexed_rather_than_reconciled(self, ops, facade):
        """Event documents are immutable and append-only; there is nothing to
        compare a timestamp against."""
        await ops.bulk_upsert([{"action": "append_event", "doc": {"event_id": "e1"}}])

        assert facade.indexed == [(ops.SHIPMENT_EVENTS, "e1", {"event_id": "e1"})]
        assert facade.upserts == []

    async def test_it_never_touches_the_raw_client(self, ops, facade):
        """``client`` is ``None`` here, so any raw call raises AttributeError.

        The assertion is the absence of an exception: this is what "follows
        DOCUMENT_STORE_BACKEND" means in practice.
        """
        result = await ops.bulk_upsert(
            [
                {"action": "upsert_shipment", "doc": {"shipment_id": "s1"}},
                {"action": "append_event", "doc": {"event_id": "e1"}},
            ]
        )

        assert result["failed"] == 0


class TestBulkUpsertAccounting:
    async def test_a_stale_document_is_reported_as_discarded_not_successful(self):
        """The bulk helper counted a scripted no-op as a success.

        Which made an ingestion run that discarded every event as stale look
        identical to one that applied every event — the exact condition an
        operator investigating "why is nothing updating" needs to see.
        """
        facade = _RecordingFacade(applied=False)
        ops = OpsElasticsearchService(facade)

        result = await ops.bulk_upsert(
            [{"action": "upsert_shipment", "doc": {"shipment_id": "s1"}}]
        )

        assert result["discarded"] == 1
        assert result["successful"] == 0
        assert result["failed"] == 0

    async def test_an_unknown_action_fails_that_operation_only(self, ops, facade):
        result = await ops.bulk_upsert(
            [
                {"action": "explode", "doc": {}},
                {"action": "upsert_rider", "doc": {"rider_id": "r1"}},
            ]
        )

        assert result["failed"] == 1
        assert result["successful"] == 1
        assert result["errors"][0]["action"] == "explode"

    async def test_one_failing_document_does_not_abandon_the_batch(self):
        """``raise_on_error=False`` is what the bulk helper bought, and it is worth
        keeping: an ingestion batch that stops at the first bad document silently
        drops everything after it."""
        facade = _RecordingFacade(fail_on=("s2",))
        ops = OpsElasticsearchService(facade)

        result = await ops.bulk_upsert(
            [
                {"action": "upsert_shipment", "doc": {"shipment_id": "s1"}},
                {"action": "upsert_shipment", "doc": {"shipment_id": "s2"}},
                {"action": "upsert_shipment", "doc": {"shipment_id": "s3"}},
            ]
        )

        assert result["successful"] == 2
        assert result["failed"] == 1
        assert [doc_id for _index, doc_id, _doc in facade.upserts] == ["s1", "s3"]

    async def test_the_error_entry_names_the_document(self, ops_error_result):
        """An error that does not say which document failed cannot be acted on."""
        error = ops_error_result["errors"][0]
        assert error["doc_id"] == "s2"
        assert error["error_type"] == "RuntimeError"
        assert "boom" in error["reason"]

    async def test_the_totals_add_up(self, ops, facade):
        result = await ops.bulk_upsert(
            [
                {"action": "upsert_shipment", "doc": {"shipment_id": "s1"}},
                {"action": "append_event", "doc": {"event_id": "e1"}},
                {"action": "nonsense", "doc": {}},
            ]
        )

        assert result["total"] == 3
        assert (
            result["successful"] + result["discarded"] + result["failed"]
            == result["total"]
        )

    async def test_an_empty_batch_is_a_no_op(self, ops):
        assert await ops.bulk_upsert([]) == {
            "total": 0,
            "successful": 0,
            "discarded": 0,
            "failed": 0,
            "errors": [],
        }


@pytest.fixture
async def ops_error_result() -> Dict[str, Any]:
    facade = _RecordingFacade(fail_on=("s2",))
    ops = OpsElasticsearchService(facade)
    return await ops.bulk_upsert(
        [{"action": "upsert_shipment", "doc": {"shipment_id": "s2"}}]
    )
