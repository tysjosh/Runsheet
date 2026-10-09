"""
Property-based test for invalid-signature rejection on voice submission.

# Feature: dinee-voice-integration, Property 3: Invalid signature is rejected

**Validates: Requirements 3.2**

Property 3 (Invalid signature is rejected): For any voice submission whose
``X-Signature`` is **absent**, **malformed** (missing the ``sha256=`` prefix),
or **non-matching** (a well-formed ``sha256=<hex>`` digest that is not the
HMAC-SHA256 of the raw body under the channel's secret), the submission is
rejected with **HTTP 401** and **no order is persisted** (Req 3.2).

The test drives the *real* :class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge`
over the *real* :class:`~fuel.services.order_intake_pipeline.OrderIntakePipeline`
(with the ``VoiceIntakeAdapter`` registered for ``channel_type="voice"``):

* the **absent** and **malformed** cases are rejected by the bridge's
  signature presence/format check (``voice_unauthorized`` → 401) before the
  pipeline is ever called, and
* the **non-matching** case passes the bridge's format check and is rejected
  by the pipeline's authoritative ``_verify_hmac`` (``webhook_signature_invalid``
  → 401) — which is why the test exercises the bridge *through* the pipeline
  rather than the format check alone.

Recording in-memory fakes for Elasticsearch and Redis let the "no order was
persisted" assertion be checked directly: on any rejection, nothing is written
to ``fuel_orders_current`` / ``fuel_order_events`` and no ledger entry is
recorded.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from errors.exceptions import AppException
from fuel.intake.adapter_base import IntakeAdapterRegistry
from fuel.intake.voice_intake_adapter import VoiceIntakeAdapter
from fuel.intake_channel_models import IntakeChannel
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from fuel.services.order_intake_pipeline import OrderIntakePipeline
from fuel.voice.dinee_voice_bridge import DineeVoiceBridge


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-voice-sig"
CHANNEL_ID = "voice-sig-chan-01"
HMAC_SECRET = "voice-invalid-signature-secret-2026"
SCHEMA_VERSION = "1.0"
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
# A timestamp inside the default replay window so the signature stage is what
# actually rejects the request (not the replay-window stage).
FRESH_TIMESTAMP = FIXED_NOW.isoformat().replace("+00:00", "Z")

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


def _correct_signature(body: bytes, secret: str = HMAC_SECRET) -> str:
    """The correct lowercase-hex HMAC-SHA256 digest for ``body``."""
    return hmac_mod.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_text = st.text(min_size=1, max_size=40)
_hex_char = st.sampled_from("0123456789abcdef")


@st.composite
def _valid_payloads(draw) -> Dict[str, Any]:
    """A structurally valid VoiceIntakePayload that maps to a valid FuelOrder.

    Used for the non-matching case so the request reaches the pipeline's HMAC
    check rather than being short-circuited earlier by required-field
    validation.
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
        "transcript": [],
        "extractedSlots": slots,
        "reviewRequired": draw(st.booleans()),
    }
    return payload


@st.composite
def _malformed_signatures(draw) -> str:
    """A non-empty signature string that lacks the ``sha256=`` prefix.

    Restricted to hex characters so it looks like a bare digest a client might
    send without the required prefix.
    """
    n = draw(st.integers(min_value=1, max_value=80))
    candidate = "".join(draw(_hex_char) for _ in range(n))
    # Guard against the (astronomically unlikely) accidental prefix match.
    assume(not candidate.lower().startswith("sha256="))
    return candidate


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


class RecordingESService:
    """Fake ES service recording every write so persistence can be inspected."""

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

    def indices_touched(self) -> set:
        """Every ES index this fake saw any write against."""
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
    """Fake VoiceSubmissionLedger recording writes; no prior entries exist."""

    def __init__(self) -> None:
        self.records: List[tuple] = []

    async def lookup(self, tenant_id: str, key: str):
        return None

    async def record(self, tenant_id, key, body_sha256, order_id, disposition):
        self.records.append((tenant_id, key, body_sha256, order_id, disposition))


def _make_channel() -> IntakeChannel:
    return IntakeChannel(
        channel_id=CHANNEL_ID,
        tenant_id=TENANT_ID,
        channel_type="voice",
        display_name="Voice Signature Channel",
        hmac_secret_ref=f"voice_hmac:{TENANT_ID}:{CHANNEL_ID}",
        supported_schema_versions=[SCHEMA_VERSION],
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _build_bridge(recording_es: RecordingESService, ledger: RecordingLedger):
    """Real DineeVoiceBridge over the real OrderIntakePipeline with fakes."""
    registry = IntakeAdapterRegistry()
    registry.register(
        VoiceIntakeAdapter(), channel_type="voice", schema_version=SCHEMA_VERSION
    )

    channel = _make_channel()
    channel_repo = AsyncMock()
    # The pipeline resolves by channel_id; the bridge resolves the tenant's
    # voice channel. Both return the same enabled voice channel.
    channel_repo.get_by_channel_id = AsyncMock(return_value=channel)
    channel_repo.get_voice_channel = AsyncMock(return_value=channel)

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
        ws_manager=RecordingWSManager(),
        credentials_vault=credentials_vault,
        customer_tank_repo=customer_tank_repo,
        clock=lambda: FIXED_NOW,
    )

    bridge = DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=channel_repo,
        ledger=ledger,
        replay_window_seconds=300,
        clock=lambda: FIXED_NOW,
    )
    return bridge


def _submit(
    bridge: DineeVoiceBridge,
    *,
    raw_body: bytes,
    signature: Optional[str],
    idempotency_key: str = "idem-sig-test",
) -> Any:
    return asyncio.run(
        bridge.submit(
            raw_body=raw_body,
            tenant_id=TENANT_ID,
            idempotency_key=idempotency_key,
            timestamp=FRESH_TIMESTAMP,
            schema_version=SCHEMA_VERSION,
            signature=signature,
            request_id="req-invalid-sig",
        )
    )


def _assert_rejected_401_no_persistence(
    exc_info, recording_es: RecordingESService, ledger: RecordingLedger
) -> None:
    """A rejection must be HTTP 401 with no order persisted and no ledger write."""
    assert exc_info.value.status_code == 401
    assert recording_es.indices_touched() == set()
    assert recording_es.updated_documents == []
    assert recording_es.indexed_documents == []
    assert ledger.records == []
    # Defensive: the canonical stores specifically must be untouched.
    assert FUEL_ORDERS_CURRENT_INDEX not in recording_es.indices_touched()
    assert FUEL_ORDER_EVENTS_INDEX not in recording_es.indices_touched()


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


class TestInvalidSignatureRejection:
    """# Feature: dinee-voice-integration, Property 3: Invalid signature is rejected

    **Validates: Requirements 3.2**
    """

    @given(payload=_valid_payloads())
    @settings(max_examples=100)
    def test_absent_signature_rejected_401(self, payload: Dict[str, Any]):
        recording_es = RecordingESService()
        ledger = RecordingLedger()
        bridge = _build_bridge(recording_es, ledger)
        raw_body = json.dumps(payload).encode()

        with pytest.raises(AppException) as exc_info:
            _submit(bridge, raw_body=raw_body, signature=None)

        _assert_rejected_401_no_persistence(exc_info, recording_es, ledger)

    @given(payload=_valid_payloads(), bad_sig=_malformed_signatures())
    @settings(max_examples=100)
    def test_malformed_signature_rejected_401(
        self, payload: Dict[str, Any], bad_sig: str
    ):
        recording_es = RecordingESService()
        ledger = RecordingLedger()
        bridge = _build_bridge(recording_es, ledger)
        raw_body = json.dumps(payload).encode()

        with pytest.raises(AppException) as exc_info:
            _submit(bridge, raw_body=raw_body, signature=bad_sig)

        _assert_rejected_401_no_persistence(exc_info, recording_es, ledger)

    @given(payload=_valid_payloads())
    @settings(max_examples=100)
    def test_nonmatching_signature_rejected_401(self, payload: Dict[str, Any]):
        recording_es = RecordingESService()
        ledger = RecordingLedger()
        bridge = _build_bridge(recording_es, ledger)
        raw_body = json.dumps(payload).encode()

        # A well-formed sha256=<hex> digest that is NOT the HMAC of the body:
        # flip the first hex nibble of the correct digest so it can never match.
        correct = _correct_signature(raw_body)
        first = correct[0]
        wrong_hex = ("1" if first != "1" else "0") + correct[1:]
        assert wrong_hex != correct
        presented = f"sha256={wrong_hex}"

        with pytest.raises(AppException) as exc_info:
            _submit(bridge, raw_body=raw_body, signature=presented)

        _assert_rejected_401_no_persistence(exc_info, recording_es, ledger)
