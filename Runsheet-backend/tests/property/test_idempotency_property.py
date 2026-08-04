"""
Property-based tests for Idempotent Processing Guarantee.

**Validates: Requirements 1.4, 1.5, 1.11, 3.4**

Property: For any event_id processed N times (N >= 1) by the ops ingestion
pipeline, the resulting Elasticsearch state SHALL be identical to processing the
event exactly once. The first pass produces a state change; all subsequent
passes are short-circuited with no side effects.

Sub-properties tested:
1. For any event_id processed N times, adapter.transform is called exactly once.
2. For any event_id processed N times, ES upsert operations happen exactly once.
3. After the first pass, subsequent passes are counted as skipped, not processed.

These properties used to be driven through ``POST /webhooks/dinee``. That route
was removed; the surviving implementation of the same
idempotency-check → transform → upsert sequence is
:meth:`ops.ingestion.replay.ReplayService._process_record`, so the properties
bind there now. Sub-property 3 changed shape with the target: the route answered
a repeat delivery with an HTTP body of ``status="duplicate"``, whereas
``_process_record`` increments ``skipped_count``. The invariant being asserted —
"a repeat does not re-enter the pipeline" — is the same.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis.strategies import text, integers

# ---------------------------------------------------------------------------
# Mock the elasticsearch_service module before importing ops modules
# ---------------------------------------------------------------------------
sys.modules.setdefault("services.elasticsearch_service", MagicMock())

from ops.ingestion.adapter import AdapterTransformer, TransformResult  # noqa: E402
from ops.ingestion.replay import ReplayJobStatus, ReplayService  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TENANT_ID = "tenant-idem"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Event IDs: printable non-empty strings (realistic identifiers)
_event_ids = text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
    min_size=1,
    max_size=64,
)

# Processing counts: at least 1, up to 10 (enough to exercise idempotency)
_delivery_counts = integers(min_value=1, max_value=10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(event_id: str) -> dict:
    """Build a valid webhook-shaped record with the given event_id."""
    return {
        "event_id": event_id,
        "event_type": "shipment_created",
        "schema_version": "1.0",
        "tenant_id": TENANT_ID,
        "timestamp": "2025-01-15T10:00:00Z",
        "data": {"shipment_id": "SHP-001", "status": "created"},
    }


def _build_pipeline():
    """
    Build a ReplayService with a mocked adapter and ES.

    The IdempotencyService mock simulates real behaviour: it tracks seen
    event_ids in a set so that the first call to is_duplicate returns False
    and subsequent calls return True.

    Returns (service, adapter_mock, seen_set, ops_es_mock).
    """
    adapter = MagicMock(spec=AdapterTransformer)
    adapter.is_version_supported.return_value = True
    adapter.transform.return_value = TransformResult(
        shipment_current_doc={"shipment_id": "SHP-001"},
        rider_current_doc=None,
        event_doc={"event_id": "placeholder"},
    )

    seen: set[str] = set()
    idempotency_service = AsyncMock()

    async def _is_duplicate(event_id: str, tenant_id: str = None) -> bool:
        return event_id in seen

    async def _mark_processed(event_id: str, tenant_id: str = None) -> None:
        seen.add(event_id)

    idempotency_service.is_duplicate = AsyncMock(side_effect=_is_duplicate)
    idempotency_service.mark_processed = AsyncMock(side_effect=_mark_processed)

    ops_es = AsyncMock()
    ops_es.append_shipment_event = AsyncMock()
    ops_es.upsert_shipment_current = AsyncMock()
    ops_es.upsert_rider_current = AsyncMock()

    service = ReplayService(
        adapter=adapter,
        idempotency=idempotency_service,
        ops_es=ops_es,
        settings=MagicMock(),
    )
    return service, adapter, seen, ops_es


# ---------------------------------------------------------------------------
# Property 1 – adapter.transform called exactly once per unique event_id
# ---------------------------------------------------------------------------
class TestIdempotentTransformCallCount:
    """**Validates: Requirements 1.4, 1.5, 1.11**"""

    @given(event_id=_event_ids, n_deliveries=_delivery_counts)
    @settings(max_examples=200)
    async def test_transform_called_exactly_once(
        self, event_id: str, n_deliveries: int
    ):
        """
        For any event_id processed N times (N >= 1), adapter.transform is
        called exactly once — the first pass triggers transformation, all
        subsequent passes are short-circuited by idempotency.
        """
        service, adapter, _seen, _ops_es = _build_pipeline()
        job = ReplayJobStatus(job_id="job-idem", tenant_id=TENANT_ID)

        payload = _make_payload(event_id)
        for _ in range(n_deliveries):
            await service._process_record(job, payload, TENANT_ID)

        assert adapter.transform.call_count == 1
        assert job.failed_count == 0


# ---------------------------------------------------------------------------
# Property 2 – ES upsert operations happen exactly once per unique event_id
# ---------------------------------------------------------------------------
class TestIdempotentESUpsertCount:
    """**Validates: Requirements 1.4, 1.5, 1.11**"""

    @given(event_id=_event_ids, n_deliveries=_delivery_counts)
    @settings(max_examples=200)
    async def test_es_upsert_called_exactly_once(
        self, event_id: str, n_deliveries: int
    ):
        """
        For any event_id processed N times, ES upsert (shipment current)
        and append (event) operations each happen exactly once.
        """
        service, _adapter, _seen, ops_es = _build_pipeline()
        job = ReplayJobStatus(job_id="job-idem", tenant_id=TENANT_ID)

        payload = _make_payload(event_id)
        for _ in range(n_deliveries):
            await service._process_record(job, payload, TENANT_ID)

        assert ops_es.upsert_shipment_current.call_count == 1
        assert ops_es.append_shipment_event.call_count == 1


# ---------------------------------------------------------------------------
# Property 3 – repeats are skipped, not reprocessed
# ---------------------------------------------------------------------------
class TestIdempotentDuplicateStatus:
    """**Validates: Requirements 1.4, 1.5, 1.11, 3.4**"""

    @given(event_id=_event_ids, n_deliveries=_delivery_counts)
    @settings(max_examples=200)
    async def test_repeats_are_skipped_not_reprocessed(
        self, event_id: str, n_deliveries: int
    ):
        """
        The first pass is processed; every subsequent pass for the same
        event_id is counted as skipped and never reaches the adapter.
        """
        service, adapter, seen, _ops_es = _build_pipeline()
        job = ReplayJobStatus(job_id="job-idem", tenant_id=TENANT_ID)

        payload = _make_payload(event_id)
        for _ in range(n_deliveries):
            await service._process_record(job, payload, TENANT_ID)

        assert job.skipped_count == n_deliveries - 1
        assert adapter.transform.call_count == 1
        assert event_id in seen
