"""
Property-based test for the Dinee voice bridge required-field validation.

# Feature: dinee-voice-integration, Property 10: Required-field validation

**Validates: Requirements 7.1, 7.2, 7.3**

Property 10 (Required-field validation): For a voice submission that has
already passed signature, replay-window, tenant, idempotency, and
schema-version checks, :meth:`DineeVoiceBridge.submit` proceeds to the
pipeline **iff** the body carries every required ``VoiceIntakePayload``
field, and otherwise rejects with HTTP 422 ``VOICE_PAYLOAD_INVALID``, names
every absent required field in ``details.missing_fields``, and persists no
order (the pipeline is never invoked and nothing is recorded in the ledger).

Concretely, for each drawn scenario:

    * all required fields present  -> the submission reaches the pipeline
      exactly once and yields an acceptance response (Req 7.1);
    * one or more required fields dropped -> a 422 ``VOICE_PAYLOAD_INVALID``
      is raised whose ``details.missing_fields`` equals the set of dropped
      required fields (Req 7.3), and the pipeline / ledger are never
      touched (Req 7.2).

Required ``VoiceIntakePayload`` fields exercised:

    top-level      : callId, transcriptId, transcript, extractedSlots
    extractedSlots : customer_name, ship_to_address, ship_to_lat,
                     ship_to_lon, product_code

Recording fakes stand in for the pipeline, the intake-channel repository
(``get_voice_channel``), and the ``VoiceSubmissionLedger`` so that every
earlier validation stage passes and the required-field decision is observed
directly on :class:`DineeVoiceBridge`.
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services.order_intake_pipeline import IntakeResponse
from fuel.voice.dinee_voice_bridge import DineeVoiceBridge
from fuel.voice.voice_submission_ledger import LedgerEntry


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-required-fields"
CHANNEL_ID = "voice-required-chan-01"
SCHEMA_VERSION = "1.0"
REPLAY_WINDOW_SECONDS = 300
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

# Inputs that make every stage *before* required-field validation pass.
VALID_SIGNATURE = "sha256=" + "a" * 64
VALID_TIMESTAMP = FIXED_NOW.isoformat()
VALID_IDEM_KEY = "idem-required-key"

# Required VoiceIntakePayload fields (dotted paths, matching pydantic loc).
REQUIRED_TOP: List[str] = ["callId", "transcriptId", "transcript", "extractedSlots"]
# ship_to_lat / ship_to_lon are intentionally NOT required: voice orders
# capture no coordinates and are reconciled during review-hold (design A).
REQUIRED_SLOTS: List[str] = [
    "customer_name",
    "ship_to_address",
    "product_code",
]


# ---------------------------------------------------------------------------
# Recording fakes (mirrors test_voice_validation_ordering_property.py)
# ---------------------------------------------------------------------------


class FakeChannel:
    """Minimal stand-in for the resolved voice IntakeChannel."""

    def __init__(self, tenant_id: str, channel_id: str, supported: List[str]) -> None:
        self.tenant_id = tenant_id
        self.channel_id = channel_id
        self.supported_schema_versions = supported


class FakeChannelRepo:
    """Recording fake for IntakeChannelRepository.get_voice_channel."""

    def __init__(self, channel: Optional[FakeChannel]) -> None:
        self._channel = channel
        self.lookups: List[str] = []

    async def get_voice_channel(self, tenant_id: str) -> Optional[FakeChannel]:
        self.lookups.append(tenant_id)
        return self._channel


class FakeLedger:
    """Recording fake for VoiceSubmissionLedger (lookup + record)."""

    def __init__(self, prior: Optional[LedgerEntry] = None) -> None:
        self._prior = prior
        self.lookups: List[Any] = []
        self.records: List[Any] = []

    async def lookup(self, tenant_id: str, key: str) -> Optional[LedgerEntry]:
        self.lookups.append((tenant_id, key))
        return self._prior

    async def record(self, *args: Any) -> None:
        self.records.append(args)


class FakePipeline:
    """Recording fake for OrderIntakePipeline.ingest_webhook.

    Always returns a ``processed`` result, so the only reason it would not be
    invoked is a bridge-side validation short-circuit — exactly what the
    required-field property asserts.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def ingest_webhook(self, **kwargs: Any) -> IntakeResponse:
        self.calls.append(kwargs)
        return IntakeResponse(
            event_id=kwargs.get("idempotency_key_override") or "",
            status="processed",
            order_id="ord_required_123",
        )


# ---------------------------------------------------------------------------
# Payload construction + drop strategy
# ---------------------------------------------------------------------------


def _complete_payload() -> Dict[str, Any]:
    """A structurally complete, valid VoiceIntakePayload body."""
    return {
        "callId": "call-required-1",
        "transcriptId": "transcript-required-1",
        "transcript": [],
        "extractedSlots": {
            "customer_name": "Acme Fuels",
            "ship_to_address": "123 Depot Road",
            "ship_to_lat": 40.0,
            "ship_to_lon": -70.0,
            "product_code": "DIESEL_2",
        },
        "reviewRequired": True,
    }


@st.composite
def _drop_scenarios(draw) -> Dict[str, Any]:
    """Draw a set of required fields to drop (possibly none).

    When ``extractedSlots`` is dropped wholesale, its subfields are moot
    (they cannot be missing individually), so no subfield drops are drawn.
    Returns the concrete payload plus the expected set of missing field
    names (empty set => a fully valid payload).
    """
    drop_top = set(
        draw(st.lists(st.sampled_from(REQUIRED_TOP), unique=True))
    )

    if "extractedSlots" in drop_top:
        drop_slots: set = set()
    else:
        drop_slots = set(
            draw(st.lists(st.sampled_from(REQUIRED_SLOTS), unique=True))
        )

    payload = _complete_payload()
    for field in drop_top:
        del payload[field]
    if "extractedSlots" not in drop_top:
        for sub in drop_slots:
            del payload["extractedSlots"][sub]

    expected_missing = set(drop_top) | {
        f"extractedSlots.{sub}" for sub in drop_slots
    }

    return {"payload": payload, "expected_missing": expected_missing}


def _make_bridge(pipeline: FakePipeline, ledger: FakeLedger) -> DineeVoiceBridge:
    channel_repo = FakeChannelRepo(
        FakeChannel(TENANT_ID, CHANNEL_ID, [SCHEMA_VERSION])
    )
    return DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=channel_repo,
        ledger=ledger,
        replay_window_seconds=REPLAY_WINDOW_SECONDS,
        clock=lambda: FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


class TestVoiceRequiredFieldValidation:
    """# Feature: dinee-voice-integration, Property 10: Required-field validation

    **Validates: Requirements 7.1, 7.2, 7.3**
    """

    @given(scenario=_drop_scenarios())
    @settings(max_examples=100)
    def test_required_fields_gate_the_pipeline(self, scenario: Dict[str, Any]) -> None:
        pipeline = FakePipeline()
        ledger = FakeLedger()
        bridge = _make_bridge(pipeline, ledger)

        raw_body = json.dumps(scenario["payload"]).encode("utf-8")
        expected_missing: set = scenario["expected_missing"]

        async def _run():
            return await bridge.submit(
                raw_body=raw_body,
                tenant_id=TENANT_ID,
                idempotency_key=VALID_IDEM_KEY,
                timestamp=VALID_TIMESTAMP,
                schema_version=SCHEMA_VERSION,
                signature=VALID_SIGNATURE,
                request_id="req-required",
            )

        if not expected_missing:
            # Req 7.1: a complete payload proceeds to the pipeline exactly once.
            response = asyncio.run(_run())
            assert len(pipeline.calls) == 1
            assert response.orderId == "ord_required_123"
            assert response.disposition in ("accepted", "review_hold", "duplicate")
        else:
            # Req 7.2: a missing required field -> 422 VOICE_PAYLOAD_INVALID,
            # and no order is persisted (pipeline + ledger never touched).
            with pytest.raises(AppException) as exc_info:
                asyncio.run(_run())
            exc = exc_info.value
            assert exc.status_code == 422
            assert exc.error_code == ErrorCode.VOICE_PAYLOAD_INVALID
            assert pipeline.calls == []
            assert ledger.records == []

            # Req 7.3: details.missing_fields names exactly the absent fields.
            reported = set(exc.details.get("missing_fields", []))
            assert reported == expected_missing
