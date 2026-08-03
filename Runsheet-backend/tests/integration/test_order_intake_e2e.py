"""
End-to-end integration test for the Order Intake Pipeline — Task 6 checkpoint.

Verifies the full pipeline flow from HMAC-signed payload through to:
    (a) A new ``fuel_orders_current`` document persisted.
    (b) A new ``order_placed`` row in ``fuel_order_events``.
    (c) A broadcast recorded on the stubbed ``OrdersWSManager``.

Since the webhook HTTP endpoint (Task 7.1) is not yet wired, this test
exercises the pipeline directly via ``OrderIntakePipeline.ingest_webhook``
to validate the end-to-end flow.

No raw ``HTTPException`` is added — the existing ``test_http_exception_ceiling``
guard remains satisfied.

Validates: Requirements 1.1.6, 2.2.2, 2.3, 4.1, 10.2.1.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from errors.exceptions import AppException
from fuel.intake.adapter_base import (
    IntakeAdapterRegistry,
    IntakeContext,
    IntakeResult,
)
from fuel.intake_channel_models import IntakeChannel
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from fuel.services.order_intake_pipeline import (
    IntakeResponse,
    OrderIntakePipeline,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-e2e-test"
CHANNEL_ID = "e2e-partner-channel"
HMAC_SECRET = "e2e-test-hmac-secret-key-2026"
FIXED_NOW = datetime(2026, 5, 10, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign_body(body: bytes, secret: str = HMAC_SECRET) -> str:
    """Compute the HMAC-SHA256 signature for a request body."""
    return hmac_mod.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def _make_channel() -> IntakeChannel:
    """Build a registered intake channel for the e2e test."""
    return IntakeChannel(
        channel_id=CHANNEL_ID,
        tenant_id=TENANT_ID,
        channel_type="api_partner",
        display_name="E2E Test Partner Channel",
        hmac_secret_ref=f"vault-ref:{TENANT_ID}:{CHANNEL_ID}:1",
        supported_schema_versions=["1.0"],
        rate_limit_per_minute=100,
        secret_version=1,
        enabled=True,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _valid_order_payload() -> Dict[str, Any]:
    """Build a valid upstream order payload for the e2e test."""
    return {
        "schema_version": "1.0",
        "event_id": "evt-e2e-happy-001",
        "customer_id": "cust-e2e-001",
        "customer_name": "E2E Fuel Distributors",
        "customer_phone": "+15559876543",
        "ship_to_address": "456 Pipeline Ave, Houston TX 77001",
        "ship_to_lat": 29.76,
        "ship_to_lon": -95.37,
        "product_code": "DIESEL_2",
        "gallons_requested": 750.0,
        "fill_to_full": False,
        "call_type": "one_off",
        "delivery_window_start": "2026-05-11T06:00:00Z",
        "delivery_window_end": "2026-05-11T14:00:00Z",
    }


# ---------------------------------------------------------------------------
# Fake adapter that produces a valid IntakeResult
# ---------------------------------------------------------------------------


class E2EApiPartnerAdapter:
    """A realistic adapter that transforms the payload into a FuelOrder doc."""

    channel_type = "api_partner"

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        order_doc = {
            "customer_id": payload["customer_id"],
            "customer_name": payload["customer_name"],
            "customer_phone": payload.get("customer_phone"),
            "customer_email": None,
            "ship_to_address": payload["ship_to_address"],
            "ship_to_lat": payload["ship_to_lat"],
            "ship_to_lon": payload["ship_to_lon"],
            "customer_tank_id": payload.get("customer_tank_id"),
            "product_code": payload["product_code"],
            "gallons_requested": payload["gallons_requested"],
            "fill_to_full": payload.get("fill_to_full", False),
            "call_type": payload["call_type"],
            "delivery_window_start": payload.get("delivery_window_start"),
            "delivery_window_end": payload.get("delivery_window_end"),
            "hold_reason": None,
            "po_number": payload.get("po_number"),
            "special_instructions": payload.get("special_instructions"),
            "intake_channel": "api_partner",
            "intake_channel_id": context.channel.channel_id,
            "intake_metadata": {
                "partner_ref": payload.get("event_id", "unknown"),
            },
            "source_schema_version": payload.get("schema_version", "1.0"),
        }
        event_docs = [
            {
                "event_type": "order_placed",
                "event_payload": {"source": "api_partner"},
            }
        ]
        return IntakeResult(order_doc=order_doc, event_docs=event_docs)


# ---------------------------------------------------------------------------
# Recording ES service — captures all writes for assertion
# ---------------------------------------------------------------------------


class RecordingESService:
    """A fake ES service that records all index and update operations.

    Provides the same interface the pipeline and repository expect:
    - ``index_document(index, doc_id, document)``
    - ``search_documents(index, query, size)``
    - ``client.update(...)``
    """

    def __init__(self):
        self.indexed_documents: List[Dict[str, Any]] = []
        self.updated_documents: List[Dict[str, Any]] = []
        # Provide a mock client for the scripted upsert path
        self.client = MagicMock()
        self.client.update = MagicMock(
            side_effect=self._record_update
        )

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Record an indexed document."""
        self.indexed_documents.append(
            {"index": index, "doc_id": doc_id, "document": document}
        )
        return {"result": "created", "_id": doc_id}

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        """Return empty results — the pipeline doesn't read during ingest."""
        return {
            "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}
        }

    def _record_update(self, **kwargs) -> Dict[str, Any]:
        """Record a scripted update (used by upsert_with_last_event_timestamp)."""
        self.updated_documents.append(kwargs)
        return {"result": "created", "_version": 1}

    def get_orders_written(self) -> List[Dict[str, Any]]:
        """Get all documents written to fuel_orders_current."""
        # The scripted upsert goes through client.update
        orders = []
        for update in self.updated_documents:
            if update.get("index") == FUEL_ORDERS_CURRENT_INDEX:
                body = update.get("body", {})
                upsert = body.get("upsert", {})
                if upsert:
                    orders.append(upsert)
        return orders

    def get_events_written(self) -> List[Dict[str, Any]]:
        """Get all documents written to fuel_order_events."""
        events = []
        for doc in self.indexed_documents:
            if doc["index"] == FUEL_ORDER_EVENTS_INDEX:
                events.append(doc["document"])
        return events


# ---------------------------------------------------------------------------
# Recording WS Manager — captures all broadcasts
# ---------------------------------------------------------------------------


class RecordingWSManager:
    """A fake WebSocket manager that records all broadcasts."""

    def __init__(self):
        self.broadcasts: List[Dict[str, Any]] = []

    async def broadcast(
        self,
        event_type: str,
        data: Any,
        tenant_id: str,
    ) -> None:
        """Record a broadcast event."""
        self.broadcasts.append(
            {"event_type": event_type, "data": data, "tenant_id": tenant_id}
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def recording_es():
    """Provide a recording ES service."""
    return RecordingESService()


@pytest.fixture
def recording_ws():
    """Provide a recording WebSocket manager."""
    return RecordingWSManager()


@pytest.fixture
def adapter_registry():
    """Provide an adapter registry with the e2e adapter registered."""
    registry = IntakeAdapterRegistry()
    registry.register(
        E2EApiPartnerAdapter(),
        channel_type="api_partner",
        schema_version="1.0",
    )
    return registry


@pytest.fixture
def channel():
    """Provide the registered intake channel."""
    return _make_channel()


@pytest.fixture
def intake_channel_repo(channel):
    """Provide a mock channel repo that returns the e2e channel."""
    repo = AsyncMock()
    repo.get_by_channel_id = AsyncMock(return_value=channel)
    return repo


@pytest.fixture
def idempotency_service():
    """Provide a mock idempotency service (no duplicates)."""
    svc = AsyncMock()
    svc.is_duplicate = AsyncMock(return_value=False)
    svc.mark_processed = AsyncMock()
    return svc


@pytest.fixture
def feature_flag_service():
    """Provide a mock feature flag service (active_gated for pipeline processing)."""
    svc = AsyncMock()
    svc.get_overlay_state = AsyncMock(return_value="active_gated")
    return svc


@pytest.fixture
def poison_queue_service():
    """Provide a mock poison queue service."""
    svc = AsyncMock()
    svc.store_failed_event = AsyncMock()
    return svc


@pytest.fixture
def credentials_vault():
    """Provide a mock vault that returns the HMAC secret."""
    vault = AsyncMock()
    vault.get = AsyncMock(return_value={"secret": HMAC_SECRET})
    return vault


@pytest.fixture
def customer_tank_repo():
    """Provide a mock tank repo (no tank referenced in this test)."""
    repo = AsyncMock()
    repo.get = AsyncMock(return_value=None)
    return repo


# The ``legacy_dual_writer`` fixture lived here, mocking the mirror into
# ``shipments_current``. The pipeline no longer takes that dependency.


@pytest.fixture
def pipeline(
    recording_es,
    intake_channel_repo,
    adapter_registry,
    idempotency_service,
    feature_flag_service,
    poison_queue_service,
    recording_ws,
    credentials_vault,
    customer_tank_repo,
):
    """Build the full pipeline with recording dependencies."""
    return OrderIntakePipeline(
        es_service=recording_es,
        intake_channel_repo=intake_channel_repo,
        adapter_registry=adapter_registry,
        idempotency_service=idempotency_service,
        feature_flag_service=feature_flag_service,
        poison_queue_service=poison_queue_service,
        ws_manager=recording_ws,
        credentials_vault=credentials_vault,
        customer_tank_repo=customer_tank_repo,
        clock=lambda: FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# End-to-End Test — Webhook Happy Path
# ---------------------------------------------------------------------------


class TestOrderIntakeE2EHappyPath:
    """End-to-end integration test: HMAC-signed webhook → pipeline → persistence + broadcast.

    Verifies the full pipeline flow produces:
        (a) A new fuel_orders_current document.
        (b) A new order_placed event in fuel_order_events.
        (c) A broadcast recorded on the stubbed OrdersWSManager.
    """

    @pytest.mark.asyncio
    async def test_e2e_webhook_creates_order_document(
        self, pipeline, recording_es
    ):
        """(a) A valid HMAC-signed webhook creates a fuel_orders_current doc."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-e2e-001",
        )

        # Pipeline returns processed with a platform-assigned order_id
        assert result.status == "processed"
        assert result.order_id is not None
        assert result.order_id.startswith("ord_")

        # Verify the order was written to fuel_orders_current
        orders = recording_es.get_orders_written()
        assert len(orders) == 1

        order_doc = orders[0]
        assert order_doc["order_id"] == result.order_id
        assert order_doc["tenant_id"] == TENANT_ID
        assert order_doc["status"] == "placed"
        assert order_doc["customer_id"] == "cust-e2e-001"
        assert order_doc["customer_name"] == "E2E Fuel Distributors"
        assert order_doc["product_code"] == "DIESEL_2"
        assert order_doc["gallons_requested"] == 750.0
        assert order_doc["call_type"] == "one_off"
        assert order_doc["intake_channel"] == "api_partner"
        assert order_doc["intake_channel_id"] == CHANNEL_ID
        assert order_doc["trace_id"] == "req-e2e-001"
        # Both "2026-05-10T14:00:00Z" and "2026-05-10T14:00:00+00:00" are valid
        assert order_doc["created_at"] in (
            FIXED_NOW.isoformat(), "2026-05-10T14:00:00Z"
        )
        assert order_doc["updated_at"] in (
            FIXED_NOW.isoformat(), "2026-05-10T14:00:00Z"
        )

    @pytest.mark.asyncio
    async def test_e2e_webhook_creates_order_placed_event(
        self, pipeline, recording_es
    ):
        """(b) A valid HMAC-signed webhook creates an order_placed event."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-e2e-002",
        )

        assert result.status == "processed"

        # Verify the event was written to fuel_order_events
        events = recording_es.get_events_written()
        assert len(events) == 1

        event_doc = events[0]
        assert event_doc["event_type"] == "order_placed"
        assert event_doc["order_id"] == result.order_id
        assert event_doc["tenant_id"] == TENANT_ID
        assert event_doc["event_id"].startswith("evt_")
        assert event_doc["trace_id"] == "req-e2e-002"
        assert event_doc["source_schema_version"] == "1.0"
        assert event_doc["event_payload"] == {"source": "api_partner"}

    @pytest.mark.asyncio
    async def test_e2e_webhook_broadcasts_order_placed(
        self, pipeline, recording_ws
    ):
        """(c) A valid HMAC-signed webhook triggers a WebSocket broadcast."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-e2e-003",
        )

        assert result.status == "processed"

        # Verify the broadcast was recorded
        assert len(recording_ws.broadcasts) == 1

        broadcast = recording_ws.broadcasts[0]
        assert broadcast["event_type"] == "order_placed"
        assert broadcast["tenant_id"] == TENANT_ID
        assert broadcast["data"]["order_id"] == result.order_id
        assert broadcast["data"]["status"] == "placed"
        assert broadcast["data"]["customer_name"] == "E2E Fuel Distributors"

    @pytest.mark.asyncio
    async def test_e2e_webhook_full_flow_all_assertions(
        self, pipeline, recording_es, recording_ws, idempotency_service
    ):
        """Combined assertion: order doc + event + broadcast + idempotency mark."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-e2e-004",
        )

        # Pipeline result
        assert result.status == "processed"
        assert result.order_id is not None
        assert result.order_id.startswith("ord_")

        # (a) fuel_orders_current doc created
        orders = recording_es.get_orders_written()
        assert len(orders) == 1
        order_doc = orders[0]
        assert order_doc["order_id"] == result.order_id
        assert order_doc["tenant_id"] == TENANT_ID
        assert order_doc["status"] == "placed"
        assert order_doc["intake_channel"] == "api_partner"
        assert order_doc["intake_channel_id"] == CHANNEL_ID

        # (b) order_placed event in fuel_order_events
        events = recording_es.get_events_written()
        assert len(events) == 1
        event_doc = events[0]
        assert event_doc["event_type"] == "order_placed"
        assert event_doc["order_id"] == result.order_id
        assert event_doc["tenant_id"] == TENANT_ID

        # (c) WebSocket broadcast recorded
        assert len(recording_ws.broadcasts) == 1
        broadcast = recording_ws.broadcasts[0]
        assert broadcast["event_type"] == "order_placed"
        assert broadcast["tenant_id"] == TENANT_ID
        assert broadcast["data"]["order_id"] == result.order_id

        # Idempotency was marked processed
        idempotency_service.mark_processed.assert_called_once()

    @pytest.mark.asyncio
    async def test_e2e_webhook_hmac_verification_works(
        self, pipeline
    ):
        """HMAC verification rejects bad signatures in the e2e flow."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        bad_signature = "0" * 64  # wrong signature

        with pytest.raises(AppException) as exc_info:
            await pipeline.ingest_webhook(
                channel_id=CHANNEL_ID,
                body=body,
                signature=bad_signature,
                request_id="req-e2e-005",
            )

        assert exc_info.value.status_code == 401
        assert "WEBHOOK_SIGNATURE_INVALID" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_e2e_webhook_idempotency_deduplicates(
        self, pipeline, recording_es, recording_ws, idempotency_service
    ):
        """Duplicate event_id returns 'duplicate' without writing."""
        idempotency_service.is_duplicate = AsyncMock(return_value=True)

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-e2e-006",
        )

        assert result.status == "duplicate"
        assert result.order_id is None

        # No writes should have occurred
        assert len(recording_es.get_orders_written()) == 0
        assert len(recording_es.get_events_written()) == 0
        assert len(recording_ws.broadcasts) == 0

    @pytest.mark.asyncio
    async def test_e2e_pipeline_stamps_platform_fields(
        self, pipeline, recording_es
    ):
        """Platform-owned fields are stamped correctly regardless of adapter output."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-e2e-007",
        )

        orders = recording_es.get_orders_written()
        assert len(orders) == 1
        order_doc = orders[0]

        # Platform-assigned fields
        assert order_doc["order_id"].startswith("ord_")
        assert len(order_doc["order_id"]) == 36  # "ord_" + 32 hex chars
        assert order_doc["tenant_id"] == TENANT_ID
        assert order_doc["status"] == "placed"
        # Both "2026-05-10T14:00:00Z" and "2026-05-10T14:00:00+00:00" are valid
        valid_timestamps = (FIXED_NOW.isoformat(), "2026-05-10T14:00:00Z")
        assert order_doc["created_at"] in valid_timestamps
        assert order_doc["updated_at"] in valid_timestamps
        assert order_doc["last_event_timestamp"] in valid_timestamps
        assert order_doc["trace_id"] == "req-e2e-007"
