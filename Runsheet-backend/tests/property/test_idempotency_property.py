"""
Property-based tests for Idempotent Processing Guarantee.

**Validates: Requirements 1.4, 1.5, 1.11**

Property 2: For any event_id delivered N times (N >= 1) to the Webhook_Receiver,
the resulting Elasticsearch state SHALL be identical to processing the event
exactly once. The first delivery produces a state change; all subsequent
deliveries return 200 without side effects.

Sub-properties tested:
1. For any event_id delivered N times, adapter.transform is called exactly once.
2. For any event_id delivered N times, ES upsert operations happen exactly once.
3. After the first delivery, subsequent deliveries return status "duplicate".
"""

import hashlib
import hmac as hmac_mod
import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis.strategies import text, integers, composite

# ---------------------------------------------------------------------------
# Mock the elasticsearch_service module before importing ops modules
# ---------------------------------------------------------------------------
sys.modules.setdefault("services.elasticsearch_service", MagicMock())

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ops.webhooks.receiver import configure_webhook_receiver, router
from ops.ingestion.adapter import AdapterTransformer, TransformResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = "test-secret-for-idempotency"
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

# Delivery counts: at least 1, up to 10 (enough to exercise idempotency)
_delivery_counts = integers(min_value=1, max_value=10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(event_id: str) -> dict:
    """Build a valid Dinee webhook payload with the given event_id."""
    return {
        "event_id": event_id,
        "event_type": "shipment_created",
        "schema_version": "1.0",
        "tenant_id": TENANT_ID,
        "timestamp": "2025-01-15T10:00:00Z",
        "data": {"shipment_id": "SHP-001", "status": "created"},
    }


def _sign(payload: dict) -> str:
    """Compute HMAC-SHA256 hex digest for the payload."""
    body = json.dumps(payload).encode("utf-8")
    return hmac_mod.new(
        WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def _post_webhook(client: TestClient, payload: dict) -> "Response":
    """POST a signed webhook payload to the receiver."""
    body = json.dumps(payload)
    sig = _sign(payload)
    return client.post(
        "/webhooks/dinee",
        content=body,
        headers={
            "X-Dinee-Signature": sig,
            "Content-Type": "application/json",
        },
    )


def _build_test_app():
    """
    Build a minimal FastAPI app with the webhook receiver wired up.

    The IdempotencyService mock simulates real behaviour: it tracks seen
    event_ids in a set so that the first call to is_duplicate returns False
    and subsequent calls return True.

    Returns (client, adapter_mock, idempotency_seen_set, ops_es_mock).
    """
    app = FastAPI()
    app.include_router(router)

    # --- Adapter mock ---
    adapter = MagicMock(spec=AdapterTransformer)
    adapter.is_version_supported.return_value = True
    adapter.transform.return_value = TransformResult(
        shipment_current_doc={"shipment_id": "SHP-001"},
        rider_current_doc=None,
        event_doc={"event_id": "placeholder"},
    )

    # --- Idempotency mock with real set-based tracking ---
    seen: set[str] = set()
    idempotency_service = AsyncMock()

    async def _is_duplicate(event_id: str) -> bool:
        return event_id in seen

    async def _mark_processed(event_id: str) -> None:
        seen.add(event_id)

    idempotency_service.is_duplicate = AsyncMock(side_effect=_is_duplicate)
    idempotency_service.mark_processed = AsyncMock(side_effect=_mark_processed)

    # --- ES mock ---
    ops_es = AsyncMock()
    ops_es.append_shipment_event = AsyncMock()
    ops_es.upsert_shipment_current = AsyncMock()
    ops_es.upsert_rider_current = AsyncMock()

    # --- Poison queue mock ---
    poison_queue = AsyncMock()

    configure_webhook_receiver(
        adapter=adapter,
        idempotency_service=idempotency_service,
        poison_queue_service=poison_queue,
        ops_es_service=ops_es,
        ws_manager=None,
        feature_flag_service=None,
        webhook_secret=WEBHOOK_SECRET,
        webhook_tenant_id="",
    )

    client = TestClient(app)
    return client, adapter, seen, ops_es


# ---------------------------------------------------------------------------
# Property 1 – adapter.transform called exactly once per unique event_id
# ---------------------------------------------------------------------------
class TestIdempotentTransformCallCount:
    """**Validates: Requirements 1.4, 1.5, 1.11**"""

    @given(event_id=_event_ids, n_deliveries=_delivery_counts)
    @settings(max_examples=200)
    def test_transform_called_exactly_once(self, event_id: str, n_deliveries: int):
        """
        For any event_id delivered N times (N >= 1), the adapter.transform
        is called exactly once — the first delivery triggers transformation,
        all subsequent deliveries are short-circuited by idempotency.
        """
        client, adapter, seen, ops_es = _build_test_app()

        payload = _make_payload(event_id)
        for _ in range(n_deliveries):
            resp = _post_webhook(client, payload)
            assert resp.status_code == 200

        assert adapter.transform.call_count == 1


# ---------------------------------------------------------------------------
# Property 2 – ES upsert operations happen exactly once per unique event_id
# ---------------------------------------------------------------------------
class TestIdempotentESUpsertCount:
    """**Validates: Requirements 1.4, 1.5, 1.11**"""

    @given(event_id=_event_ids, n_deliveries=_delivery_counts)
    @settings(max_examples=200)
    def test_es_upsert_called_exactly_once(self, event_id: str, n_deliveries: int):
        """
        For any event_id delivered N times, ES upsert (shipment current)
        and append (event) operations each happen exactly once.
        """
        client, adapter, seen, ops_es = _build_test_app()

        payload = _make_payload(event_id)
        for _ in range(n_deliveries):
            resp = _post_webhook(client, payload)
            assert resp.status_code == 200

        assert ops_es.upsert_shipment_current.call_count == 1
        assert ops_es.append_shipment_event.call_count == 1


# ---------------------------------------------------------------------------
# Property 3 – subsequent deliveries return status "duplicate"
# ---------------------------------------------------------------------------
class TestIdempotentDuplicateStatus:
    """**Validates: Requirements 1.4, 1.5, 1.11**"""

    @given(event_id=_event_ids, n_deliveries=_delivery_counts)
    @settings(max_examples=200)
    def test_subsequent_deliveries_return_duplicate(
        self, event_id: str, n_deliveries: int
    ):
        """
        The first delivery returns status "processed"; all subsequent
        deliveries for the same event_id return status "duplicate".
        """
        client, adapter, seen, ops_es = _build_test_app()

        payload = _make_payload(event_id)
        statuses = []
        for _ in range(n_deliveries):
            resp = _post_webhook(client, payload)
            assert resp.status_code == 200
            statuses.append(resp.json()["status"])

        # First delivery must be "processed"
        assert statuses[0] == "processed"

        # All subsequent deliveries must be "duplicate"
        for status in statuses[1:]:
            assert status == "duplicate"
