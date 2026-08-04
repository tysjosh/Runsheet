"""
Integration tests for the Ops ingestion pipeline.

Tests the full flow: Adapter → ES upsert, verifying that documents land in all
three indices (shipments_current, shipment_events, riders_current) with correct
field mappings.

Previously this suite drove the pipeline through ``POST /webhooks/dinee``. That
route and its module were removed, and with them the receiver's routing from a
:class:`~ops.ingestion.adapter.TransformResult` to the three ES calls. The
surviving implementation of that same routing is
:meth:`ops.ingestion.replay.ReplayService._process_record`, whose own docstring
describes it as processing records "through the same pipeline as live webhook
events: idempotency check → transform → upsert". These tests now bind to it, so
they exercise real production code rather than a reimplementation in the test
harness.

Two assertions did not survive the move, and are deliberately not faked here:

* **HMAC rejection.** Signature verification was the route's job. It is now
  covered directly against the shared verifier in
  ``tests/property/test_hmac_property.py``.
* **Poison-queue routing on an unsupported schema version.** The receiver
  enqueued the payload; ``_process_record`` has no poison queue and instead
  counts the record as failed. The test below asserts the behaviour that
  actually exists rather than the behaviour that used to.

Validates: Requirements 24.1-24.3, 3.3, 3.4
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Patch elasticsearch_service BEFORE any ops imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from ops.ingestion.adapter import (  # noqa: E402
    AdapterTransformer,
    SHIPMENTS_CURRENT_FIELDS,
    SHIPMENT_EVENTS_FIELDS,
    RIDERS_CURRENT_FIELDS,
)
from ops.ingestion.handlers.v1_0 import V1SchemaHandler  # noqa: E402
from ops.ingestion.replay import ReplayJobStatus, ReplayService  # noqa: E402
from tests.fixtures import load_fixture, load_all_webhook_fixtures  # noqa: E402

pytestmark = pytest.mark.integration

TENANT_ID = "tenant-test-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _InMemoryStore:
    """Lightweight in-memory store that captures indexed documents per index."""

    def __init__(self):
        self.shipments: list[dict] = []
        self.events: list[dict] = []
        self.riders: list[dict] = []

    def reset(self):
        self.shipments.clear()
        self.events.clear()
        self.riders.clear()


def _build_pipeline(store: _InMemoryStore):
    """Wire a real adapter + V1 handler into ReplayService with a capturing ES.

    Only Elasticsearch and Redis are faked. The adapter, the V1 schema handler,
    and the transform-to-upsert routing are all the real implementations.
    """
    adapter = AdapterTransformer()
    adapter.register_handler("1.0", V1SchemaHandler())

    _processed_ids: set[str] = set()
    idempotency = AsyncMock()

    async def _is_dup(event_id: str, tenant_id: str = None) -> bool:
        return event_id in _processed_ids

    async def _mark(event_id: str, tenant_id: str = None) -> None:
        _processed_ids.add(event_id)

    idempotency.is_duplicate = AsyncMock(side_effect=_is_dup)
    idempotency.mark_processed = AsyncMock(side_effect=_mark)

    ops_es = AsyncMock()

    async def _upsert_shipment(doc):
        store.shipments.append(doc)
        return True

    async def _upsert_rider(doc):
        store.riders.append(doc)
        return True

    async def _append_event(doc):
        store.events.append(doc)

    ops_es.upsert_shipment_current = AsyncMock(side_effect=_upsert_shipment)
    ops_es.upsert_rider_current = AsyncMock(side_effect=_upsert_rider)
    ops_es.append_shipment_event = AsyncMock(side_effect=_append_event)

    service = ReplayService(
        adapter=adapter,
        idempotency=idempotency,
        ops_es=ops_es,
        settings=MagicMock(),
    )
    return service, {"idempotency": idempotency, "ops_es": ops_es}


def _new_job() -> ReplayJobStatus:
    return ReplayJobStatus(job_id="job-test", tenant_id=TENANT_ID)


# ===========================================================================
# 23.1 — Ingestion pipeline integration tests
# ===========================================================================


class TestIngestionPipelineIntegration:
    """
    End-to-end ingestion: Adapter → ES upsert.
    Verifies documents in all three indices.

    Validates: Requirements 24.1-24.3
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = _InMemoryStore()
        self.service, self.ctx = _build_pipeline(self.store)
        self.job = _new_job()

    async def _ingest(self, payload: dict) -> None:
        """Push one webhook-shaped record through the real pipeline."""
        await self.service._process_record(self.job, payload, TENANT_ID)

    # --- Shipment events produce shipment + event docs ---

    async def test_shipment_created_indexes_shipment_and_event(self):
        """shipment_created → shipments_current + shipment_events."""
        await self._ingest(load_fixture("shipment_created"))

        assert self.job.failed_count == 0
        assert len(self.store.shipments) == 1
        assert len(self.store.events) == 1
        assert len(self.store.riders) == 0

    async def test_shipment_updated_indexes_shipment_and_event(self):
        await self._ingest(load_fixture("shipment_updated"))

        assert self.job.failed_count == 0
        assert len(self.store.shipments) == 1
        assert self.store.shipments[0]["status"] == "in_transit"
        assert len(self.store.events) == 1

    async def test_shipment_delivered_indexes_shipment_and_event(self):
        await self._ingest(load_fixture("shipment_delivered"))

        assert self.job.failed_count == 0
        assert self.store.shipments[0]["status"] == "delivered"

    async def test_shipment_failed_indexes_shipment_with_failure_reason(self):
        await self._ingest(load_fixture("shipment_failed"))

        assert self.job.failed_count == 0
        ship = self.store.shipments[0]
        assert ship["status"] == "failed"
        assert "failure_reason" in ship

    # --- Rider events produce rider + event docs ---

    async def test_rider_assigned_indexes_rider_and_event(self):
        """rider_assigned → riders_current + shipment_events."""
        await self._ingest(load_fixture("rider_assigned"))

        assert self.job.failed_count == 0
        assert len(self.store.riders) == 1
        assert len(self.store.events) == 1
        assert len(self.store.shipments) == 0

    async def test_rider_status_changed_indexes_rider_and_event(self):
        await self._ingest(load_fixture("rider_status_changed"))

        assert self.job.failed_count == 0
        assert len(self.store.riders) == 1
        assert self.store.riders[0]["status"] == "idle"

    # --- All 6 event types produce correct index distribution ---

    async def test_all_six_events_produce_correct_index_distribution(self):
        """4 shipment events → 4 shipment docs, 2 rider events → 2 rider docs, 6 event docs."""
        fixtures = load_all_webhook_fixtures()
        assert len(fixtures) == 6

        for name, payload in fixtures.items():
            await self._ingest(payload)
            assert self.job.failed_count == 0, f"Fixture '{name}' failed"

        assert len(self.store.shipments) == 4
        assert len(self.store.riders) == 2
        assert len(self.store.events) == 6

    # --- Field validation: documents conform to strict mappings ---

    async def test_shipment_docs_conform_to_strict_mapping(self):
        for payload in load_all_webhook_fixtures().values():
            await self._ingest(payload)

        assert self.store.shipments, "no shipment docs produced"
        for ship in self.store.shipments:
            assert set(ship.keys()).issubset(SHIPMENTS_CURRENT_FIELDS), (
                f"Unmapped fields: {set(ship.keys()) - SHIPMENTS_CURRENT_FIELDS}"
            )
            assert "tenant_id" in ship
            assert "shipment_id" in ship
            assert "trace_id" in ship
            assert "ingested_at" in ship
            assert "source_schema_version" in ship

    async def test_event_docs_conform_to_strict_mapping(self):
        for payload in load_all_webhook_fixtures().values():
            await self._ingest(payload)

        assert self.store.events, "no event docs produced"
        for evt in self.store.events:
            assert set(evt.keys()).issubset(SHIPMENT_EVENTS_FIELDS), (
                f"Unmapped fields: {set(evt.keys()) - SHIPMENT_EVENTS_FIELDS}"
            )
            assert "event_id" in evt
            assert "event_type" in evt
            assert "tenant_id" in evt

    async def test_rider_docs_conform_to_strict_mapping(self):
        for payload in load_all_webhook_fixtures().values():
            await self._ingest(payload)

        assert self.store.riders, "no rider docs produced"
        for rider in self.store.riders:
            assert set(rider.keys()).issubset(RIDERS_CURRENT_FIELDS), (
                f"Unmapped fields: {set(rider.keys()) - RIDERS_CURRENT_FIELDS}"
            )
            assert "rider_id" in rider
            assert "tenant_id" in rider

    # --- Enrichment metadata ---

    async def test_all_docs_enriched_with_metadata(self):
        """Every doc has ingested_at, trace_id, source_schema_version."""
        await self._ingest(load_fixture("shipment_created"))

        for doc in (self.store.shipments[0], self.store.events[0]):
            assert "ingested_at" in doc
            assert "trace_id" in doc
            assert "source_schema_version" in doc
            assert doc["source_schema_version"] == "1.0"

    # --- Idempotency within the pipeline ---

    async def test_duplicate_event_not_reindexed(self):
        """Same event_id processed twice → only one set of ES documents.

        The receiver answered a repeat delivery with ``status="duplicate"``;
        ``_process_record`` records it as skipped. Either way the invariant that
        matters is unchanged: the second pass writes nothing.
        """
        payload = load_fixture("shipment_created")
        await self._ingest(payload)
        await self._ingest(payload)

        assert self.job.skipped_count == 1
        assert len(self.store.shipments) == 1
        assert len(self.store.events) == 1

    # --- Unsupported schema version is refused, not written ---

    async def test_unsupported_schema_version_writes_nothing(self):
        """An unregistered schema_version must not reach any index.

        NB: the deleted receiver additionally pushed the payload onto the
        ``ops_poison_queue``. ``_process_record`` has no poison queue, so the
        surviving guarantee is narrower — the record is counted as failed and
        nothing is indexed. Asserted as-is rather than papered over.
        """
        payload = load_fixture("shipment_created")
        payload["schema_version"] = "99.0"
        await self._ingest(payload)

        assert self.job.failed_count == 1
        assert len(self.store.shipments) == 0
        assert len(self.store.events) == 0
        assert len(self.store.riders) == 0
