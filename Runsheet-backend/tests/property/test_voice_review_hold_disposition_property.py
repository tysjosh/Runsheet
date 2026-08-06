"""
Property-based test for the voice review-hold disposition.

# Feature: dinee-voice-integration, Property 11: Review-hold disposition

**Validates: Requirements 8.1, 8.2, 8.4**

Property 11 (Review-hold disposition): For any structurally valid
``VoiceIntakePayload`` whose ``reviewRequired`` is ``true``, submitting it
through the *real* ``OrderIntakePipeline`` — with the ``VoiceIntakeAdapter``
registered for ``channel_type="voice"`` and the ``VoiceReviewHoldHook``
registered as a ``before_accept`` hook — persists a valid canonical
``FuelOrder`` that:

* has ``status == "on_hold"`` (Req 8.1),
* carries a non-empty ``hold_reason`` identifying the voice-review cause
  (Req 8.4),
* is excluded from dispatch — the state machine only allows
  ``on_hold → {placed, cancelled}`` (Req 8.2), and
* lives in the canonical ``fuel_orders_current`` / ``fuel_order_events``
  indices with no separate parallel draft store written (Req 8.3 corollary).

The test uses recording in-memory fakes for Elasticsearch and Redis so the
"only the canonical store is written" assertion is directly checkable.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from fuel.intake.adapter_base import IntakeAdapterRegistry
from fuel.intake.voice_intake_adapter import (
    VOICE_REVIEW_HOLD_REASON,
    VoiceIntakeAdapter,
)
from fuel.intake_channel_models import IntakeChannel
from fuel.order_models import FuelOrder
from fuel.order_state_machine import VALID_STATUS_TRANSITIONS
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from fuel.services.order_intake_pipeline import OrderIntakePipeline
from fuel.voice.voice_review_hold_hook import VoiceReviewHoldHook


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-voice-review"
CHANNEL_ID = "voice-review-chan-01"
HMAC_SECRET = "voice-review-hold-secret-2026"
SCHEMA_VERSION = "1.0"
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

# Canonical product codes accepted by fuel_product_catalog.canonicalize.
_PRODUCT_CODES = [
    "DIESEL_2",
    "HEATING_OIL",
    "GASOLINE_REG",
    "GASOLINE_PREM",
    "PROPANE",
    "KEROSENE",
    "OFF_ROAD_DIESEL",
    "DEF",
    "ETHANOL_E85",
]

# Statuses from which dispatch (or progress toward dispatch) becomes possible.
# An on_hold order must not be able to reach any of these directly.
_DISPATCH_STATUSES = {"scheduled", "dispatched", "in_transit", "delivered"}


def _sign_body(body: bytes, secret: str = HMAC_SECRET) -> str:
    """Compute the lowercase-hex HMAC-SHA256 signature for a request body."""
    return hmac_mod.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Strategies — structurally valid, review-flagged VoiceIntakePayloads
# ---------------------------------------------------------------------------

_text = st.text(min_size=1, max_size=40)


@st.composite
def _transcript_turns(draw) -> List[Dict[str, str]]:
    n = draw(st.integers(min_value=0, max_value=4))
    return [
        {"speaker": draw(_text), "text": draw(st.text(min_size=0, max_size=60))}
        for _ in range(n)
    ]


@st.composite
def _review_payloads(draw) -> Dict[str, Any]:
    """Generate review-flagged VoiceIntakePayloads that map to a valid FuelOrder.

    Constraints applied so the payload survives ``FuelOrder.model_validate``:
    - ``customer_id`` is always present (FuelOrder requires it),
    - ``gallons_requested`` is always > 0,
    - a coherent delivery window is always supplied (satisfies one_off), and
    - ``reviewRequired`` is fixed to ``True`` (this property's premise).
    """
    slots: Dict[str, Any] = {
        "customer_id": draw(_text),
        "customer_name": draw(_text),
        "ship_to_address": draw(_text),
        "ship_to_lat": draw(
            st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False)
        ),
        "ship_to_lon": draw(
            st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False)
        ),
        "product_code": draw(st.sampled_from(_PRODUCT_CODES)),
        "gallons_requested": draw(
            st.floats(min_value=1.0, max_value=50000, allow_nan=False, allow_infinity=False)
        ),
        "fill_to_full": draw(st.booleans()),
        "call_type": draw(
            st.sampled_from(["will_call", "auto_fill", "keep_full", "one_off"])
        ),
        "delivery_window_start": "2026-05-11T08:00:00Z",
        "delivery_window_end": "2026-05-11T12:00:00Z",
    }

    payload: Dict[str, Any] = {
        "callId": draw(_text),
        "transcriptId": draw(_text),
        "transcript": draw(_transcript_turns()),
        "extractedSlots": slots,
        "reviewRequired": True,
    }
    if draw(st.booleans()):
        payload["callerPhone"] = draw(_text)
    if draw(st.booleans()):
        payload["agentConfidence"] = draw(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
        )
    return payload


# ---------------------------------------------------------------------------
# Recording ES fake — captures every index/update for assertion
# ---------------------------------------------------------------------------


class RecordingESService:
    """Fake ES service recording all writes so the store can be inspected."""

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
        """Every ES index this fake saw a write against."""
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


def _make_channel() -> IntakeChannel:
    return IntakeChannel(
        channel_id=CHANNEL_ID,
        tenant_id=TENANT_ID,
        channel_type="voice",
        display_name="Voice Review Channel",
        hmac_secret_ref=f"voice_hmac:{TENANT_ID}:{CHANNEL_ID}",
        supported_schema_versions=[SCHEMA_VERSION],
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _build_pipeline(recording_es: RecordingESService, recording_ws: RecordingWSManager):
    """Construct the real pipeline with the voice adapter + review-hold hook.

    Redis (idempotency) is faked with an AsyncMock; ES and the WS manager are
    recording fakes. Feature flag is ``active_auto`` so no legacy dual-write /
    parallel store is exercised.
    """
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


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------


class TestVoiceReviewHoldDisposition:
    """# Feature: dinee-voice-integration, Property 11: Review-hold disposition

    **Validates: Requirements 8.1, 8.2, 8.4**
    """

    @given(payload=_review_payloads())
    @settings(max_examples=100)
    def test_review_required_persists_on_hold_order(
        self, payload: Dict[str, Any]
    ):
        recording_es = RecordingESService()
        recording_ws = RecordingWSManager()
        pipeline = _build_pipeline(recording_es, recording_ws)

        body = json.dumps(payload).encode()
        signature = _sign_body(body)
        idem_key = f"idem-{payload['callId']}"

        result = asyncio.run(
            pipeline.ingest_webhook(
                channel_id=CHANNEL_ID,
                body=body,
                signature=signature,
                request_id="req-review-hold",
                idempotency_key_override=idem_key,
                schema_version_override=SCHEMA_VERSION,
            )
        )

        # The submission is accepted through the pipeline.
        assert result.status == "processed"
        assert result.order_id is not None and result.order_id.startswith("ord_")

        # Exactly one order was persisted to the canonical fuel_orders_current.
        orders = recording_es.get_orders_written()
        assert len(orders) == 1
        order_doc = orders[0]

        # (Req 8.1) The review-flagged voice order is placed on_hold.
        assert order_doc["status"] == "on_hold"

        # (Req 8.4) It carries a non-empty hold_reason naming the voice cause.
        assert order_doc.get("hold_reason") == VOICE_REVIEW_HOLD_REASON
        assert order_doc["hold_reason"].strip()

        # The persisted document is a valid canonical FuelOrder (the hold
        # invariant on_hold ⇒ non-empty hold_reason holds).
        validated = FuelOrder.model_validate(order_doc)
        assert validated.status == "on_hold"
        assert validated.hold_reason == VOICE_REVIEW_HOLD_REASON
        assert validated.intake_channel == "voice"

        # (Req 8.2) on_hold is excluded from dispatch: the state machine only
        # allows on_hold → {placed, cancelled}; no dispatch-progress status is
        # reachable directly from on_hold.
        assert VALID_STATUS_TRANSITIONS["on_hold"] == {"placed", "cancelled"}
        assert not (_DISPATCH_STATUSES & VALID_STATUS_TRANSITIONS["on_hold"])

        # (Req 8.3 corollary) No separate/parallel store is written — only the
        # canonical fuel_orders_current + fuel_order_events indices are touched.
        assert recording_es.indices_touched() <= {
            FUEL_ORDERS_CURRENT_INDEX,
            FUEL_ORDER_EVENTS_INDEX,
        }
        # The order_placed event was appended to the canonical event store.
        events = recording_es.get_events_written()
        assert len(events) == 1
        assert events[0]["event_type"] == "order_placed"
        assert events[0]["order_id"] == result.order_id
