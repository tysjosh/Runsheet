"""
Integration tests for the Ops ingestion pipeline.

Tests the full flow: Webhook → Adapter → ES upsert, verifying that
documents land in all three indices (shipments_current, shipment_events,
riders_current) with correct field mappings.

Validates: Requirements 24.1-24.3
"""

import hashlib
import hmac
import json
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

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ops.webhooks.receiver import configure_webhook_receiver, router as webhook_router
from ops.ingestion.adapter import (
    AdapterTransformer,
    SHIPMENTS_CURRENT_FIELDS,
    SHIPMENT_EVENTS_FIELDS,
    RIDERS_CURRENT_FIELDS,
)
from ops.ingestion.handlers.v1_0 import V1SchemaHandler
from tests.fixtures import load_fixture, load_all_webhook_fixtures

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = "integration-test-secret"
TENANT_ID = "tenant-test-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sign(payload: dict, secret: str = WEBHOOK_SECRET) -> str:
    body = json.dumps(payload).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, payload: dict, secret: str = WEBHOOK_SECRET):
    body = json.dumps(payload)
    sig = _sign(payload, secret)
    return client.post(
        "/webhooks/dinee",
        content=body,
        headers={
            "X-Dinee-Signature": sig,
            "Content-Type": "application/json",
        },
    )


class _InMemoryStore:
    """Lightweight in-memory store that captures indexed documents per index."""

    def __init__(self):
        self.shipments: list[dict] = []
        self.events: list[dict] = []
        self.riders: list[dict] = []
        self.poison_queue: list[dict] = []

    def reset(self):
        self.shipments.clear()
        self.events.clear()
        self.riders.clear()
        self.poison_queue.clear()


def _build_integration_app(store: _InMemoryStore, *, feature_flag_service=None):
    """
    Build a FastAPI test app with the real adapter pipeline but mocked
    ES/Redis so we can capture what gets indexed.
    """
    app = FastAPI()
    app.include_router(webhook_router)

    # Real adapter with V1 handler
    adapter = AdapterTransformer()
    adapter.register_handler("1.0", V1SchemaHandler())

    # Mock idempotency service
    _processed_ids: set[str] = set()
    idempotency = AsyncMock()

    async def _is_dup(event_id: str) -> bool:
        return event_id in _processed_ids

    async def _mark(event_id: str) -> None:
        _processed_ids.add(event_id)

    idempotency.is_duplicate = AsyncMock(side_effect=_is_dup)
    idempotency.mark_processed = AsyncMock(side_effect=_mark)

    # Mock ES service — capture indexed documents
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

    # Mock poison queue
    poison_queue = AsyncMock()

    async def _store_failed(payload, error, error_type, tenant_id="", trace_id=""):
        store.poison_queue.append({
            "payload": payload,
            "error": error,
            "error_type": error_type,
        })

    poison_queue.store_failed_event = AsyncMock(side_effect=_store_failed)

    configure_webhook_receiver(
        adapter=adapter,
        idempotency_service=idempotency,
        poison_queue_service=poison_queue,
        ops_es_service=ops_es,
        ws_manager=None,
        feature_flag_service=feature_flag_service,
        webhook_secret=WEBHOOK_SECRET,
        webhook_tenant_id="",
    )

    client = TestClient(app)
    return client, {
        "idempotency": idempotency,
        "ops_es": ops_es,
        "poison_queue": poison_queue,
        "_processed_ids": _processed_ids,
    }


# ===========================================================================
# 23.1 — Ingestion pipeline integration tests
# ===========================================================================


class TestIngestionPipelineIntegration:
    """
    End-to-end ingestion: Webhook → Adapter → ES upsert.
    Verifies documents in all three indices.

    Validates: Requirements 24.1-24.3
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.store = _InMemoryStore()
        self.client, self.ctx = _build_integration_app(self.store)

    # --- Shipment events produce shipment + event docs ---

    def test_shipment_created_indexes_shipment_and_event(self):
        """shipment_created → shipments_current + shipment_events."""
        payload = load_fixture("shipment_created")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"
        assert len(self.store.shipments) == 1
        assert len(self.store.events) == 1
        assert len(self.store.riders) == 0

    def test_shipment_updated_indexes_shipment_and_event(self):
        payload = load_fixture("shipment_updated")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        assert len(self.store.shipments) == 1
        assert self.store.shipments[0]["status"] == "in_transit"
        assert len(self.store.events) == 1

    def test_shipment_delivered_indexes_shipment_and_event(self):
        payload = load_fixture("shipment_delivered")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        assert self.store.shipments[0]["status"] == "delivered"

    def test_shipment_failed_indexes_shipment_with_failure_reason(self):
        payload = load_fixture("shipment_failed")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        ship = self.store.shipments[0]
        assert ship["status"] == "failed"
        assert "failure_reason" in ship

    # --- Rider events produce rider + event docs ---

    def test_rider_assigned_indexes_rider_and_event(self):
        """rider_assigned → riders_current + shipment_events."""
        payload = load_fixture("rider_assigned")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        assert len(self.store.riders) == 1
        assert len(self.store.events) == 1
        assert len(self.store.shipments) == 0

    def test_rider_status_changed_indexes_rider_and_event(self):
        payload = load_fixture("rider_status_changed")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        assert len(self.store.riders) == 1
        rider = self.store.riders[0]
        assert rider["status"] == "idle"

    # --- All 6 event types produce correct index distribution ---

    def test_all_six_events_produce_correct_index_distribution(self):
        """4 shipment events → 4 shipment docs, 2 rider events → 2 rider docs, 6 event docs."""
        fixtures = load_all_webhook_fixtures()
        assert len(fixtures) == 6

        for name, payload in fixtures.items():
            resp = _post(self.client, payload)
            assert resp.status_code == 200, f"Fixture '{name}' failed: {resp.text}"

        assert len(self.store.shipments) == 4
        assert len(self.store.riders) == 2
        assert len(self.store.events) == 6

    # --- Field validation: documents conform to strict mappings ---

    def test_shipment_docs_conform_to_strict_mapping(self):
        fixtures = load_all_webhook_fixtures()
        for payload in fixtures.values():
            _post(self.client, payload)

        for ship in self.store.shipments:
            assert set(ship.keys()).issubset(SHIPMENTS_CURRENT_FIELDS), (
                f"Unmapped fields: {set(ship.keys()) - SHIPMENTS_CURRENT_FIELDS}"
            )
            assert "tenant_id" in ship
            assert "shipment_id" in ship
            assert "trace_id" in ship
            assert "ingested_at" in ship
            assert "source_schema_version" in ship

    def test_event_docs_conform_to_strict_mapping(self):
        fixtures = load_all_webhook_fixtures()
        for payload in fixtures.values():
            _post(self.client, payload)

        for evt in self.store.events:
            assert set(evt.keys()).issubset(SHIPMENT_EVENTS_FIELDS), (
                f"Unmapped fields: {set(evt.keys()) - SHIPMENT_EVENTS_FIELDS}"
            )
            assert "event_id" in evt
            assert "event_type" in evt
            assert "tenant_id" in evt

    def test_rider_docs_conform_to_strict_mapping(self):
        fixtures = load_all_webhook_fixtures()
        for payload in fixtures.values():
            _post(self.client, payload)

        for rider in self.store.riders:
            assert set(rider.keys()).issubset(RIDERS_CURRENT_FIELDS), (
                f"Unmapped fields: {set(rider.keys()) - RIDERS_CURRENT_FIELDS}"
            )
            assert "rider_id" in rider
            assert "tenant_id" in rider

    # --- Enrichment metadata ---

    def test_all_docs_enriched_with_metadata(self):
        """Every doc has ingested_at, trace_id, source_schema_version."""
        payload = load_fixture("shipment_created")
        _post(self.client, payload)

        for doc in [self.store.shipments[0], self.store.events[0]]:
            assert "ingested_at" in doc
            assert "trace_id" in doc
            assert "source_schema_version" in doc
            assert doc["source_schema_version"] == "1.0"

    # --- Idempotency within the pipeline ---

    def test_duplicate_event_not_reindexed(self):
        """Same event_id sent twice → only one set of ES documents."""
        payload = load_fixture("shipment_created")
        _post(self.client, payload)
        resp2 = _post(self.client, payload)

        assert resp2.json()["status"] == "duplicate"
        assert len(self.store.shipments) == 1
        assert len(self.store.events) == 1

    # --- Unknown schema version routes to poison queue ---

    def test_unknown_schema_version_routes_to_poison_queue(self):
        payload = load_fixture("shipment_created")
        payload["schema_version"] = "99.0"
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "queued_for_review"
        assert len(self.store.shipments) == 0
        assert len(self.store.poison_queue) == 1

    # --- Invalid HMAC rejected ---

    def test_invalid_hmac_rejects_with_401(self):
        payload = load_fixture("shipment_created")
        resp = _post(self.client, payload, secret="wrong-secret")

        assert resp.status_code == 401
        assert len(self.store.shipments) == 0
        assert len(self.store.events) == 0
