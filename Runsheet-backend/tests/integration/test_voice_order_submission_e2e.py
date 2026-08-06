"""
End-to-end integration test for Dinee voice order submission — Task 10.3.

Exercises the full Surface A path over the **real**
:class:`~fuel.services.order_intake_pipeline.OrderIntakePipeline` with the
:class:`~fuel.intake.voice_intake_adapter.VoiceIntakeAdapter` registered for
``channel_type="voice"`` and the
:class:`~fuel.voice.voice_review_hold_hook.VoiceReviewHoldHook` registered on
the pipeline. A signed voice body is submitted through the HTTP endpoint
``POST /voice-intake`` (served by the ``voice_submission_router`` with a wired
:class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge`) against recording
in-memory Elasticsearch / ``/ws/orders`` fakes.

Assertions (submit → persist → broadcast):
    (a) the order persists to ``fuel_orders_current``,
    (b) an ``order_placed`` event persists to ``fuel_order_events``,
    (c) a broadcast is emitted on ``/ws/orders``, and
    (d) a ``reviewRequired=true`` submission persists an order with
        ``status == "on_hold"`` (the review-hold disposition).

Validates: Requirements 1.1, 1.2, 1.3, 8.1.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.handlers import register_exception_handlers
from fuel.intake.adapter_base import IntakeAdapterRegistry
from fuel.intake.voice_intake_adapter import VoiceIntakeAdapter
from fuel.intake_channel_models import IntakeChannel
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from fuel.services.order_intake_pipeline import OrderIntakePipeline
from fuel.voice import voice_submission_router as router_module
from fuel.voice.dinee_voice_bridge import DineeVoiceBridge
from fuel.voice.voice_review_hold_hook import VoiceReviewHoldHook
from fuel.voice.voice_submission_ledger import LedgerEntry


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-voice-e2e"
CHANNEL_ID = "voice-e2e-chan-01"
HMAC_SECRET = "voice-e2e-hmac-secret-2026"
SCHEMA_VERSION = "1.0"
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign_body(body: bytes, secret: str = HMAC_SECRET) -> str:
    """Compute the ``sha256=``-prefixed lowercase-hex HMAC-SHA256 signature."""
    digest = hmac_mod.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _voice_payload(*, review_required: bool) -> Dict[str, Any]:
    """Build a structurally valid VoiceIntakePayload for the e2e test."""
    return {
        "callId": "call-e2e-001",
        "transcriptId": "transcript-e2e-001",
        "transcript": [
            {"speaker": "agent", "text": "How many gallons today?"},
            {"speaker": "caller", "text": "Fill it up, about 500 gallons."},
        ],
        "callerPhone": "+15551234567",
        "extractedSlots": {
            "customer_id": "cust-e2e-001",
            "customer_name": "E2E Voice Fuel Co",
            "ship_to_address": "789 Depot Rd, Dallas TX 75001",
            "ship_to_lat": 32.78,
            "ship_to_lon": -96.80,
            "product_code": "DIESEL_2",
            "gallons_requested": 500.0,
            "fill_to_full": True,
            "call_type": "will_call",
            "delivery_window_start": "2026-05-11T08:00:00Z",
            "delivery_window_end": "2026-05-11T12:00:00Z",
        },
        "reviewRequired": review_required,
        "agentConfidence": 0.92,
    }


def _make_channel() -> IntakeChannel:
    return IntakeChannel(
        channel_id=CHANNEL_ID,
        tenant_id=TENANT_ID,
        channel_type="voice",
        display_name="Voice E2E Channel",
        hmac_secret_ref=f"voice_hmac:{TENANT_ID}:{CHANNEL_ID}",
        supported_schema_versions=[SCHEMA_VERSION],
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# Recording fakes — capture every ES write / broadcast / ledger write
# ---------------------------------------------------------------------------


class RecordingESService:
    """Fake ES service recording all writes for canonical-store assertions."""

    def __init__(self) -> None:
        self.indexed_documents: List[Dict[str, Any]] = []
        self.updated_documents: List[Dict[str, Any]] = []
        self.client = MagicMock()
        self.client.update = MagicMock(side_effect=self._record_update)

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.indexed_documents.append(
            {"index": index, "doc_id": doc_id, "document": document}
        )
        return {"result": "created", "_id": doc_id}

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        return {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}}

    def _record_update(self, **kwargs) -> Dict[str, Any]:
        self.updated_documents.append(kwargs)
        return {"result": "created", "_version": 1}

    async def upsert_if_newer(
        self,
        index: str,
        doc_id: str,
        document: Dict[str, Any],
        *,
        timestamp_field: str = "last_event_timestamp",
    ) -> bool:
        """Record a timestamp-guarded upsert.

        The order repository used to build the painless ``scripted_upsert`` itself
        and call ``client.update``, so this fake mocked the raw client. That write
        would have kept going to Elasticsearch after the document plane moved to
        Postgres, so it now goes through the facade — and the fake models the
        facade, which is what the code under test actually depends on. Recorded in
        the same shape ``get_orders_written`` already reads.
        """
        self.updated_documents.append(
            {"index": index, "id": doc_id, "body": {"upsert": document}}
        )
        return True

    def get_orders_written(self) -> List[Dict[str, Any]]:
        orders = []
        for update in self.updated_documents:
            if update.get("index") == FUEL_ORDERS_CURRENT_INDEX:
                upsert = update.get("body", {}).get("upsert", {})
                if upsert:
                    orders.append(upsert)
        return orders

    def get_events_written(self) -> List[Dict[str, Any]]:
        return [
            doc["document"]
            for doc in self.indexed_documents
            if doc["index"] == FUEL_ORDER_EVENTS_INDEX
        ]

    def indices_touched(self) -> set:
        touched = {doc["index"] for doc in self.indexed_documents}
        touched |= {
            up.get("index") for up in self.updated_documents if up.get("index")
        }
        return touched


class RecordingWSManager:
    """Fake /ws/orders manager recording broadcasts."""

    def __init__(self) -> None:
        self.broadcasts: List[Dict[str, Any]] = []

    async def broadcast(self, event_type: str, data: Any, tenant_id: str) -> None:
        self.broadcasts.append(
            {"event_type": event_type, "data": data, "tenant_id": tenant_id}
        )


class RecordingLedger:
    """In-memory voice submission ledger; every submission is a fresh key."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    async def lookup(self, tenant_id: str, key: str) -> Optional[LedgerEntry]:
        return None

    async def record(
        self,
        tenant_id: str,
        key: str,
        body_sha256: str,
        order_id: Optional[str],
        disposition: str,
    ) -> None:
        self.records.append(
            {
                "tenant_id": tenant_id,
                "key": key,
                "body_sha256": body_sha256,
                "order_id": order_id,
                "disposition": disposition,
            }
        )


# ---------------------------------------------------------------------------
# Harness construction — real pipeline + real bridge behind the HTTP router
# ---------------------------------------------------------------------------


def _build_pipeline(
    recording_es: RecordingESService, recording_ws: RecordingWSManager
) -> OrderIntakePipeline:
    """Build the real pipeline with the voice adapter + review-hold hook."""
    registry = IntakeAdapterRegistry()
    registry.register(
        VoiceIntakeAdapter(), channel_type="voice", schema_version=SCHEMA_VERSION
    )

    channel_repo = AsyncMock()
    channel_repo.get_by_channel_id = AsyncMock(return_value=_make_channel())

    idempotency_service = AsyncMock()
    idempotency_service.is_duplicate = AsyncMock(return_value=False)
    idempotency_service.mark_processed = AsyncMock()

    feature_flag_service = AsyncMock()
    feature_flag_service.get_overlay_state = AsyncMock(return_value="active_auto")

    poison_queue_service = AsyncMock()
    poison_queue_service.store_failed_event = AsyncMock()

    credentials_vault = AsyncMock()
    credentials_vault.get = AsyncMock(return_value={"secret": HMAC_SECRET})

    customer_tank_repo = AsyncMock()
    customer_tank_repo.get = AsyncMock(return_value=None)

    pipeline = OrderIntakePipeline(
        es_service=recording_es,
        intake_channel_repo=channel_repo,
        adapter_registry=registry,
        idempotency_service=idempotency_service,
        feature_flag_service=feature_flag_service,
        poison_queue_service=poison_queue_service,
        ws_manager=recording_ws,
        credentials_vault=credentials_vault,
        customer_tank_repo=customer_tank_repo,
        clock=lambda: FIXED_NOW,
    )
    pipeline.register_hook(VoiceReviewHoldHook())
    return pipeline


@pytest.fixture
def recording_es() -> RecordingESService:
    return RecordingESService()


@pytest.fixture
def recording_ws() -> RecordingWSManager:
    return RecordingWSManager()


@pytest.fixture
def ledger() -> RecordingLedger:
    return RecordingLedger()


@pytest.fixture
def client(recording_es, recording_ws, ledger):
    """A FastAPI TestClient serving POST /voice-intake through the real bridge."""
    pipeline = _build_pipeline(recording_es, recording_ws)

    channel_repo = AsyncMock()
    channel_repo.get_voice_channel = AsyncMock(return_value=_make_channel())

    bridge = DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=channel_repo,
        ledger=ledger,
        replay_window_seconds=300,
        clock=lambda: FIXED_NOW,
    )

    app = FastAPI()
    register_exception_handlers(app)
    router_module.configure_voice_submission_router(bridge=bridge)
    app.include_router(router_module.router)

    with TestClient(app) as test_client:
        yield test_client

    # Reset the module-level bridge so this test never leaks into others.
    router_module.configure_voice_submission_router(bridge=None)


def _headers(signature: str) -> Dict[str, str]:
    return {
        "X-Runsheet-Tenant": TENANT_ID,
        "X-Idempotency-Key": "idem-e2e-001",
        "X-Timestamp": FIXED_NOW.isoformat(),
        "X-Schema-Version": SCHEMA_VERSION,
        "X-Signature": signature,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# End-to-end tests
# ---------------------------------------------------------------------------


class TestVoiceOrderSubmissionE2E:
    """End-to-end: POST /voice-intake → pipeline → persist + broadcast.

    Validates: Requirements 1.1, 1.2, 1.3, 8.1.
    """

    def test_signed_submission_persists_and_broadcasts(
        self, client, recording_es, recording_ws, ledger
    ):
        """(a)/(b)/(c) An accepted voice order persists + broadcasts."""
        payload = _voice_payload(review_required=False)
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        resp = client.post("/voice-intake", content=body, headers=_headers(signature))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["orderId"].startswith("ord_")
        assert data["disposition"] == "accepted"
        order_id = data["orderId"]

        # (a) Persisted to fuel_orders_current through the pipeline.
        orders = recording_es.get_orders_written()
        assert len(orders) == 1
        order_doc = orders[0]
        assert order_doc["order_id"] == order_id
        assert order_doc["tenant_id"] == TENANT_ID
        assert order_doc["status"] == "placed"
        assert order_doc["intake_channel"] == "voice"
        assert order_doc["intake_channel_id"] == CHANNEL_ID
        assert order_doc["intake_metadata"]["call_id"] == payload["callId"]

        # (b) order_placed event persisted to fuel_order_events.
        events = recording_es.get_events_written()
        assert len(events) == 1
        assert events[0]["event_type"] == "order_placed"
        assert events[0]["order_id"] == order_id
        assert events[0]["tenant_id"] == TENANT_ID

        # (c) Broadcast emitted on /ws/orders.
        assert len(recording_ws.broadcasts) == 1
        broadcast = recording_ws.broadcasts[0]
        assert broadcast["event_type"] == "order_placed"
        assert broadcast["tenant_id"] == TENANT_ID
        assert broadcast["data"]["order_id"] == order_id
        assert broadcast["data"]["status"] == "placed"

        # Only the canonical indices were written — no parallel store.
        assert recording_es.indices_touched() <= {
            FUEL_ORDERS_CURRENT_INDEX,
            FUEL_ORDER_EVENTS_INDEX,
        }

        # The ledger recorded the outcome for idempotency recall.
        assert len(ledger.records) == 1
        assert ledger.records[0]["order_id"] == order_id
        assert ledger.records[0]["disposition"] == "accepted"

    def test_review_hold_submission_persists_on_hold(
        self, client, recording_es, recording_ws
    ):
        """(d) A reviewRequired=true submission persists status == 'on_hold'."""
        payload = _voice_payload(review_required=True)
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        resp = client.post("/voice-intake", content=body, headers=_headers(signature))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["orderId"].startswith("ord_")
        assert data["disposition"] == "review_hold"
        order_id = data["orderId"]

        # (d) Persisted with the review-hold disposition (Req 8.1).
        orders = recording_es.get_orders_written()
        assert len(orders) == 1
        order_doc = orders[0]
        assert order_doc["order_id"] == order_id
        assert order_doc["status"] == "on_hold"
        assert order_doc["hold_reason"] == "voice_review_required"
        assert order_doc["intake_channel"] == "voice"

        # The order_placed event still persists and broadcasts.
        events = recording_es.get_events_written()
        assert len(events) == 1
        assert events[0]["event_type"] == "order_placed"
        assert events[0]["order_id"] == order_id

        assert len(recording_ws.broadcasts) == 1
        broadcast = recording_ws.broadcasts[0]
        assert broadcast["event_type"] == "order_placed"
        assert broadcast["data"]["order_id"] == order_id
        assert broadcast["data"]["status"] == "on_hold"
