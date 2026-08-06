"""
Property-based test for pipeline reuse / canonical-store persistence.

# Feature: dinee-voice-integration, Property 5: Voice orders always flow through the pipeline into the canonical store (never a parallel store)

**Validates: Requirements 1.1, 1.2, 1.4, 8.3**

Property 5 (Pipeline reuse / canonical-store persistence): For any
structurally valid, correctly-signed ``VoiceIntakePayload`` driven through
the *real* :class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge.submit`
into the *real* :class:`~fuel.services.order_intake_pipeline.OrderIntakePipeline`
— with the :class:`~fuel.intake.voice_intake_adapter.VoiceIntakeAdapter`
registered for ``channel_type="voice"`` — the submission:

* produces the resulting order + ``order_placed`` event **through the
  pipeline** (Req 1.1), and
* is persisted **only** into the canonical ``fuel_orders_current`` /
  ``fuel_order_events`` indices — the set of Elasticsearch indices written
  is a subset of ``{fuel_orders_current, fuel_order_events}`` (Req 1.2, 1.4,
  8.3 corollary). No parallel / bespoke voice order store is ever touched.

The test uses a recording in-memory Elasticsearch fake so the "only the
canonical store is written / never a parallel store" assertion is directly
checkable, exactly as the sibling ``test_voice_review_hold_disposition_property``
does. The bridge is driven with a real, valid ``sha256=``-prefixed signature,
a fresh (in-window) timestamp, a supported schema version, and a present
idempotency key so that every validation stage passes and the pipeline is
actually invoked.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from fuel.intake.adapter_base import IntakeAdapterRegistry
from fuel.intake.voice_intake_adapter import VoiceIntakeAdapter
from fuel.intake_channel_models import IntakeChannel
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from fuel.services.order_intake_pipeline import OrderIntakePipeline
from fuel.voice.dinee_voice_bridge import DineeVoiceBridge
from fuel.voice.voice_models import VoiceSubmissionResponse
from fuel.voice.voice_review_hold_hook import VoiceReviewHoldHook
from fuel.voice.voice_submission_ledger import LedgerEntry


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-voice-pipeline"
CHANNEL_ID = "voice-pipeline-chan-01"
HMAC_SECRET = "voice-pipeline-reuse-secret-2026"
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


def _sign_body(body: bytes, secret: str = HMAC_SECRET) -> str:
    """Compute the ``sha256=``-prefixed lowercase-hex HMAC-SHA256 signature."""
    digest = hmac_mod.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Strategies — structurally valid VoiceIntakePayloads (review-flag varies)
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
def _payloads(draw) -> Dict[str, Any]:
    """Generate VoiceIntakePayloads that map to a valid canonical FuelOrder.

    ``reviewRequired`` is allowed to vary so both the ``accepted`` and
    ``review_hold`` dispositions are exercised; either way the order must
    land only in the canonical store.
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
        "reviewRequired": draw(st.booleans()),
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


class RecordingLedger:
    """In-memory voice submission ledger recording every write.

    ``lookup`` always returns ``None`` (each generated submission is a fresh
    idempotency key), so the bridge always drives the pipeline. ``record``
    captures the outcome so the test can confirm the ledger is the only
    non-canonical store touched — and it is an *idempotency* store, not a
    parallel *order* store.
    """

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


def _make_channel() -> IntakeChannel:
    return IntakeChannel(
        channel_id=CHANNEL_ID,
        tenant_id=TENANT_ID,
        channel_type="voice",
        display_name="Voice Pipeline Channel",
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


def _build_bridge(pipeline) -> tuple[DineeVoiceBridge, RecordingLedger]:
    """Wrap the real pipeline in a DineeVoiceBridge with recording fakes."""
    channel_repo = AsyncMock()
    channel_repo.get_voice_channel = AsyncMock(return_value=_make_channel())
    ledger = RecordingLedger()
    bridge = DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=channel_repo,
        ledger=ledger,
        replay_window_seconds=300,
        clock=lambda: FIXED_NOW,
    )
    return bridge, ledger


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------


class TestVoicePipelineReuseCanonicalStore:
    """# Feature: dinee-voice-integration, Property 5: Voice orders always flow through the pipeline into the canonical store (never a parallel store)

    **Validates: Requirements 1.1, 1.2, 1.4, 8.3**
    """

    @given(payload=_payloads())
    @settings(max_examples=100)
    def test_submission_persists_only_to_canonical_store(
        self, payload: Dict[str, Any]
    ):
        recording_es = RecordingESService()
        recording_ws = RecordingWSManager()
        pipeline = _build_pipeline(recording_es, recording_ws)
        bridge, ledger = _build_bridge(pipeline)

        body = json.dumps(payload).encode()
        signature = _sign_body(body)
        idem_key = f"idem-{payload['callId']}-{payload['transcriptId']}"

        response = asyncio.run(
            bridge.submit(
                raw_body=body,
                tenant_id=TENANT_ID,
                idempotency_key=idem_key,
                timestamp=FIXED_NOW.isoformat(),
                schema_version=SCHEMA_VERSION,
                signature=signature,
                request_id="req-pipeline-reuse",
            )
        )

        # (Req 1.1) The submission flowed through the pipeline and produced an
        # order id; the disposition is a coarse acceptance outcome.
        assert isinstance(response, VoiceSubmissionResponse)
        assert response.orderId.startswith("ord_")
        assert response.disposition in ("accepted", "review_hold")

        # (Req 1.1/1.2) Exactly one canonical order was persisted through the
        # pipeline into fuel_orders_current.
        orders = recording_es.get_orders_written()
        assert len(orders) == 1
        order_doc = orders[0]
        assert order_doc["intake_channel"] == "voice"

        # (Req 1.1) Exactly one order_placed event was appended to the
        # canonical fuel_order_events store.
        events = recording_es.get_events_written()
        assert len(events) == 1
        assert events[0]["event_type"] == "order_placed"
        assert events[0]["order_id"] == response.orderId

        # (Req 1.2, 1.4, 8.3 corollary) The set of ES indices written is a
        # subset of the two canonical indices — no parallel / bespoke voice
        # order store is ever touched.
        assert recording_es.indices_touched() <= {
            FUEL_ORDERS_CURRENT_INDEX,
            FUEL_ORDER_EVENTS_INDEX,
        }

        # The only non-canonical write is the idempotency ledger (Req 5/9),
        # which is a dedup store keyed by (tenant, idempotency_key) — it holds
        # no order documents, so it is not a parallel order store (Req 1.4).
        assert len(ledger.records) == 1
        assert ledger.records[0]["order_id"] == response.orderId
        assert set(ledger.records[0].keys()) == {
            "tenant_id",
            "key",
            "body_sha256",
            "order_id",
            "disposition",
        }
