"""
End-to-end contract tests for the Dinee webhook receiver.

Sends real fixture payloads through the webhook endpoint, captures what
the adapter produces and what gets sent to ES, and verifies:
- ES documents have expected fields (shipment_id, status, tenant_id, etc.)
- HMAC signature verification accepts valid / rejects invalid
- Idempotency deduplicates repeated deliveries
- All 6 event types are handled correctly

Validates: Requirements 24.1-24.6
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

from ops.webhooks.receiver import configure_webhook_receiver, router
from ops.ingestion.adapter import (
    AdapterTransformer,
    TransformResult,
    SHIPMENTS_CURRENT_FIELDS,
    SHIPMENT_EVENTS_FIELDS,
    RIDERS_CURRENT_FIELDS,
)
from ops.ingestion.handlers.v1_0 import V1SchemaHandler
from tests.fixtures import load_fixture, load_all_webhook_fixtures

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = "contract-test-secret"
TENANT_ID = "tenant-test-1"

# Expected fields per index (subset that MUST be present)
REQUIRED_SHIPMENT_FIELDS = {"shipment_id", "status", "tenant_id", "trace_id", "ingested_at", "source_schema_version"}
REQUIRED_EVENT_FIELDS = {"event_id", "event_type", "tenant_id", "event_timestamp", "trace_id", "ingested_at", "source_schema_version"}
REQUIRED_RIDER_FIELDS = {"rider_id", "status", "tenant_id", "trace_id", "ingested_at", "source_schema_version"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sign(payload: dict, secret: str = WEBHOOK_SECRET) -> str:
    """Compute HMAC-SHA256 signature for a payload."""
    body = json.dumps(payload).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _build_contract_app():
    """
    Build a FastAPI test app with the REAL adapter (V1SchemaHandler)
    but mocked ES and Redis services so we can capture indexed documents.
    """
    app = FastAPI()
    app.include_router(router)

    # Real adapter with real V1 handler
    adapter = AdapterTransformer()
    adapter.register_handler("1.0", V1SchemaHandler())

    # Mock idempotency — tracks which event_ids have been "processed"
    _processed_ids: set[str] = set()
    idempotency = AsyncMock()

    async def _is_duplicate(event_id: str) -> bool:
        return event_id in _processed_ids

    async def _mark_processed(event_id: str) -> None:
        _processed_ids.add(event_id)

    idempotency.is_duplicate = AsyncMock(side_effect=_is_duplicate)
    idempotency.mark_processed = AsyncMock(side_effect=_mark_processed)

    # Mock ES service — capture indexed documents
    ops_es = AsyncMock()
    indexed_shipments: list[dict] = []
    indexed_events: list[dict] = []
    indexed_riders: list[dict] = []

    async def _upsert_shipment(doc):
        indexed_shipments.append(doc)
        return True

    async def _upsert_rider(doc):
        indexed_riders.append(doc)
        return True

    async def _append_event(doc):
        indexed_events.append(doc)

    ops_es.upsert_shipment_current = AsyncMock(side_effect=_upsert_shipment)
    ops_es.upsert_rider_current = AsyncMock(side_effect=_upsert_rider)
    ops_es.append_shipment_event = AsyncMock(side_effect=_append_event)

    # Mock poison queue
    poison_queue = AsyncMock()

    configure_webhook_receiver(
        adapter=adapter,
        idempotency_service=idempotency,
        poison_queue_service=poison_queue,
        ops_es_service=ops_es,
        ws_manager=None,
        feature_flag_service=None,
        webhook_secret=WEBHOOK_SECRET,
        webhook_tenant_id="",
    )

    client = TestClient(app)
    return client, {
        "idempotency": idempotency,
        "ops_es": ops_es,
        "poison_queue": poison_queue,
        "indexed_shipments": indexed_shipments,
        "indexed_events": indexed_events,
        "indexed_riders": indexed_riders,
        "_processed_ids": _processed_ids,
    }


def _post(client, payload, secret=WEBHOOK_SECRET):
    """POST a signed webhook payload."""
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


# ===========================================================================
# Contract Tests: All 6 event types produce correct ES documents
# ===========================================================================


class TestShipmentEventContracts:
    """Send each shipment fixture through the webhook and verify ES docs. Validates: Req 24.1-24.3"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.ctx = _build_contract_app()

    def test_shipment_created(self):
        payload = load_fixture("shipment_created")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"

        # Shipment current doc
        assert len(self.ctx["indexed_shipments"]) == 1
        ship = self.ctx["indexed_shipments"][0]
        assert REQUIRED_SHIPMENT_FIELDS.issubset(ship.keys()), f"Missing fields: {REQUIRED_SHIPMENT_FIELDS - ship.keys()}"
        assert ship["shipment_id"] == "SHP-20250115-0001"
        assert ship["status"] == "pending"
        assert ship["tenant_id"] == TENANT_ID
        assert ship["source_schema_version"] == "1.0"
        # All fields must be in the allowed set
        assert set(ship.keys()).issubset(SHIPMENTS_CURRENT_FIELDS)

        # Event doc
        assert len(self.ctx["indexed_events"]) == 1
        evt = self.ctx["indexed_events"][0]
        assert REQUIRED_EVENT_FIELDS.issubset(evt.keys()), f"Missing fields: {REQUIRED_EVENT_FIELDS - evt.keys()}"
        assert evt["event_type"] == "shipment_created"
        assert evt["tenant_id"] == TENANT_ID
        assert set(evt.keys()).issubset(SHIPMENT_EVENTS_FIELDS)

        # No rider doc for shipment events
        assert len(self.ctx["indexed_riders"]) == 0

    def test_shipment_updated(self):
        payload = load_fixture("shipment_updated")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        ship = self.ctx["indexed_shipments"][0]
        assert ship["shipment_id"] == "SHP-20250115-0001"
        assert ship["status"] == "in_transit"
        assert "current_location" in ship
        assert set(ship.keys()).issubset(SHIPMENTS_CURRENT_FIELDS)

    def test_shipment_delivered(self):
        payload = load_fixture("shipment_delivered")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        ship = self.ctx["indexed_shipments"][0]
        assert ship["status"] == "delivered"
        assert set(ship.keys()).issubset(SHIPMENTS_CURRENT_FIELDS)

    def test_shipment_failed(self):
        payload = load_fixture("shipment_failed")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        ship = self.ctx["indexed_shipments"][0]
        assert ship["status"] == "failed"
        assert "failure_reason" in ship
        assert ship["failure_reason"] == "Customer unavailable at delivery address"
        assert set(ship.keys()).issubset(SHIPMENTS_CURRENT_FIELDS)


class TestRiderEventContracts:
    """Send each rider fixture through the webhook and verify ES docs. Validates: Req 24.1-24.3"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.ctx = _build_contract_app()

    def test_rider_assigned(self):
        payload = load_fixture("rider_assigned")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"

        # Rider current doc
        assert len(self.ctx["indexed_riders"]) == 1
        rider = self.ctx["indexed_riders"][0]
        assert REQUIRED_RIDER_FIELDS.issubset(rider.keys()), f"Missing fields: {REQUIRED_RIDER_FIELDS - rider.keys()}"
        assert rider["rider_id"] == "RDR-101"
        assert rider["status"] == "active"
        assert rider["tenant_id"] == TENANT_ID
        assert rider["source_schema_version"] == "1.0"
        assert set(rider.keys()).issubset(RIDERS_CURRENT_FIELDS)

        # Event doc always produced
        assert len(self.ctx["indexed_events"]) == 1
        evt = self.ctx["indexed_events"][0]
        assert evt["event_type"] == "rider_assigned"
        assert set(evt.keys()).issubset(SHIPMENT_EVENTS_FIELDS)

        # No shipment doc for rider events
        assert len(self.ctx["indexed_shipments"]) == 0

    def test_rider_status_changed(self):
        payload = load_fixture("rider_status_changed")
        resp = _post(self.client, payload)

        assert resp.status_code == 200
        rider = self.ctx["indexed_riders"][0]
        assert rider["rider_id"] == "RDR-101"
        assert rider["status"] == "idle"
        assert rider["availability"] == "available"
        assert set(rider.keys()).issubset(RIDERS_CURRENT_FIELDS)


# ===========================================================================
# Contract Tests: HMAC Signature Verification
# ===========================================================================


class TestSignatureContract:
    """Verify HMAC accepts valid and rejects invalid signatures. Validates: Req 24.4"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.ctx = _build_contract_app()

    def test_valid_hmac_accepted(self):
        """A correctly signed fixture payload is accepted."""
        payload = load_fixture("shipment_created")
        resp = _post(self.client, payload, secret=WEBHOOK_SECRET)
        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"

    def test_invalid_hmac_rejected(self):
        """A payload signed with the wrong secret is rejected with 401."""
        payload = load_fixture("shipment_created")
        resp = _post(self.client, payload, secret="wrong-secret")
        assert resp.status_code == 401
        data = resp.json()
        assert data["error_code"] == "WEBHOOK_SIGNATURE_INVALID"
        # Nothing indexed
        assert len(self.ctx["indexed_shipments"]) == 0
        assert len(self.ctx["indexed_events"]) == 0

    def test_tampered_payload_rejected(self):
        """If the payload is modified after signing, HMAC fails."""
        payload = load_fixture("shipment_created")
        sig = _sign(payload)
        # Tamper with the payload
        payload["event_id"] = "evt-tampered-999"
        body = json.dumps(payload)
        resp = self.client.post(
            "/webhooks/dinee",
            content=body,
            headers={
                "X-Dinee-Signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_all_fixtures_accepted_with_valid_hmac(self):
        """Every fixture payload is accepted when correctly signed."""
        fixtures = load_all_webhook_fixtures()
        assert len(fixtures) == 6, f"Expected 6 fixtures, got {len(fixtures)}"
        for name, payload in fixtures.items():
            resp = _post(self.client, payload)
            assert resp.status_code == 200, f"Fixture '{name}' rejected: {resp.text}"


# ===========================================================================
# Contract Tests: Idempotency
# ===========================================================================


class TestIdempotencyContract:
    """Verify duplicate deliveries are deduplicated. Validates: Req 24.5"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.ctx = _build_contract_app()

    def test_duplicate_returns_duplicate_status(self):
        """Sending the same payload twice: first is processed, second is duplicate."""
        payload = load_fixture("shipment_created")

        # First delivery
        resp1 = _post(self.client, payload)
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "processed"

        # Second delivery (same event_id)
        resp2 = _post(self.client, payload)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"

        # Only one document indexed
        assert len(self.ctx["indexed_shipments"]) == 1
        assert len(self.ctx["indexed_events"]) == 1

    def test_different_event_ids_both_processed(self):
        """Two payloads with different event_ids are both processed."""
        p1 = load_fixture("shipment_created")
        p2 = load_fixture("shipment_updated")

        resp1 = _post(self.client, p1)
        resp2 = _post(self.client, p2)

        assert resp1.json()["status"] == "processed"
        assert resp2.json()["status"] == "processed"
        assert len(self.ctx["indexed_shipments"]) == 2
        assert len(self.ctx["indexed_events"]) == 2

    def test_triple_delivery_only_one_indexed(self):
        """Sending the same payload three times still results in one ES document."""
        payload = load_fixture("rider_assigned")

        for _ in range(3):
            _post(self.client, payload)

        assert len(self.ctx["indexed_riders"]) == 1
        assert len(self.ctx["indexed_events"]) == 1


# ===========================================================================
# Contract Tests: Full pipeline — all 6 event types
# ===========================================================================


class TestAllEventTypesContract:
    """Send all 6 fixture payloads and verify the full pipeline. Validates: Req 24.1-24.6"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.ctx = _build_contract_app()

    def test_all_six_events_processed(self):
        """All 6 fixture event types are accepted and produce valid ES documents."""
        fixtures = load_all_webhook_fixtures()
        assert len(fixtures) == 6

        for name, payload in fixtures.items():
            resp = _post(self.client, payload)
            assert resp.status_code == 200, f"Fixture '{name}' failed: {resp.text}"
            assert resp.json()["status"] == "processed"

        # 4 shipment events + 2 rider events
        assert len(self.ctx["indexed_shipments"]) == 4
        assert len(self.ctx["indexed_riders"]) == 2
        # All 6 produce event docs
        assert len(self.ctx["indexed_events"]) == 6

    def test_all_event_docs_have_required_fields(self):
        """Every event doc produced has the required fields."""
        fixtures = load_all_webhook_fixtures()
        for payload in fixtures.values():
            _post(self.client, payload)

        for evt in self.ctx["indexed_events"]:
            assert REQUIRED_EVENT_FIELDS.issubset(evt.keys()), (
                f"Event doc missing fields: {REQUIRED_EVENT_FIELDS - evt.keys()}"
            )
            assert set(evt.keys()).issubset(SHIPMENT_EVENTS_FIELDS), (
                f"Event doc has unmapped fields: {set(evt.keys()) - SHIPMENT_EVENTS_FIELDS}"
            )

    def test_all_shipment_docs_have_required_fields(self):
        """Every shipment current doc has the required fields."""
        fixtures = load_all_webhook_fixtures()
        for payload in fixtures.values():
            _post(self.client, payload)

        for ship in self.ctx["indexed_shipments"]:
            assert REQUIRED_SHIPMENT_FIELDS.issubset(ship.keys()), (
                f"Shipment doc missing fields: {REQUIRED_SHIPMENT_FIELDS - ship.keys()}"
            )
            assert set(ship.keys()).issubset(SHIPMENTS_CURRENT_FIELDS), (
                f"Shipment doc has unmapped fields: {set(ship.keys()) - SHIPMENTS_CURRENT_FIELDS}"
            )

    def test_all_rider_docs_have_required_fields(self):
        """Every rider current doc has the required fields."""
        fixtures = load_all_webhook_fixtures()
        for payload in fixtures.values():
            _post(self.client, payload)

        for rider in self.ctx["indexed_riders"]:
            assert REQUIRED_RIDER_FIELDS.issubset(rider.keys()), (
                f"Rider doc missing fields: {REQUIRED_RIDER_FIELDS - rider.keys()}"
            )
            assert set(rider.keys()).issubset(RIDERS_CURRENT_FIELDS), (
                f"Rider doc has unmapped fields: {set(rider.keys()) - RIDERS_CURRENT_FIELDS}"
            )

    def test_enrichment_metadata_present(self):
        """All docs are enriched with ingested_at, trace_id, source_schema_version."""
        payload = load_fixture("shipment_created")
        _post(self.client, payload)

        ship = self.ctx["indexed_shipments"][0]
        evt = self.ctx["indexed_events"][0]

        for doc in [ship, evt]:
            assert "ingested_at" in doc
            assert "trace_id" in doc
            assert "source_schema_version" in doc
            assert doc["source_schema_version"] == "1.0"
