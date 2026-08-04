"""
Property-based test for the Dinee voice bridge validation ordering.

# Feature: dinee-voice-integration, Property 1: Validation ordering
(signature -> replay -> tenant -> idempotency -> schema/required-field)

**Validates: Requirements 2.3, 4.4, 5.3, 6.3**

Property 1 (Validation ordering): When a voice submission would fail at more
than one validation stage, the :class:`DineeVoiceBridge` short-circuits at the
*earliest* stage in the normative order and returns that stage's error, and no
order is persisted (the pipeline is never invoked) before every stage passes.

The normative ordering enforced by ``DineeVoiceBridge.submit`` is:

    1. Signature presence/format  -> 401 VOICE_UNAUTHORIZED
    2. Replay window (X-Timestamp) -> 401 VOICE_REPLAY_WINDOW_EXCEEDED
    3. Tenant / voice-channel resolution
         a. no enabled voice channel -> 404 RESOURCE_NOT_FOUND
         b. payload tenant_id mismatch -> 403 VOICE_TENANT_MISMATCH
    4. Idempotency
         a. missing X-Idempotency-Key -> 400 MISSING_IDEMPOTENCY_KEY
         b. same key + different body -> 409 IDEMPOTENCY_CONFLICT
    5. Schema version -> 422 UNSUPPORTED_SCHEMA_VERSION
    6. Required-field validation -> 422 VOICE_PAYLOAD_INVALID

Each example independently controls whether every stage would pass or fail,
then asserts the raised ``AppException`` matches the earliest-failing stage
(and that the pipeline was invoked exactly when — and only when — all stages
pass). Recording fakes stand in for the pipeline, the intake-channel
repository (``get_voice_channel``), and the ``VoiceSubmissionLedger`` so the
ordering is observed directly.
"""
from __future__ import annotations

import asyncio
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

TENANT_ID = "tenant-ordering"
CHANNEL_ID = "voice-ordering-chan-01"
SCHEMA_VERSION = "1.0"
REPLAY_WINDOW_SECONDS = 300
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

VALID_SIGNATURE = "sha256=" + "a" * 64
VALID_TIMESTAMP = FIXED_NOW.isoformat()  # within the replay window under FIXED_NOW
VALID_IDEM_KEY = "idem-ordering-key"

# Expected (status_code, error_code) for each stage's rejection.
_SIG_FAIL = (401, ErrorCode.VOICE_UNAUTHORIZED)
_REPLAY_FAIL = (401, ErrorCode.VOICE_REPLAY_WINDOW_EXCEEDED)
_CHANNEL_FAIL = (404, ErrorCode.RESOURCE_NOT_FOUND)
_TENANT_FAIL = (403, ErrorCode.VOICE_TENANT_MISMATCH)
_IDEM_MISSING_FAIL = (400, ErrorCode.MISSING_IDEMPOTENCY_KEY)
_IDEM_CONFLICT_FAIL = (409, ErrorCode.IDEMPOTENCY_CONFLICT)
_SCHEMA_FAIL = (422, ErrorCode.UNSUPPORTED_SCHEMA_VERSION)
_FIELDS_FAIL = (422, ErrorCode.VOICE_PAYLOAD_INVALID)


# ---------------------------------------------------------------------------
# Recording fakes
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

    Always returns a ``processed`` result so the *only* reason it would not be
    called is a bridge-side validation short-circuit — which is exactly what
    the ordering property asserts.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def ingest_webhook(self, **kwargs: Any) -> IntakeResponse:
        self.calls.append(kwargs)
        return IntakeResponse(
            event_id=kwargs.get("idempotency_key_override") or "",
            status="processed",
            order_id="ord_ordering_123",
        )


# ---------------------------------------------------------------------------
# Scenario strategy — independently break/keep every stage
# ---------------------------------------------------------------------------


def _build_payload(*, fields_ok: bool, tenant_ok: bool, channel_tenant: str) -> Dict[str, Any]:
    """Build a JSON-object body, optionally incomplete / tenant-mismatched."""
    payload: Dict[str, Any] = {
        "callId": "call-ordering-1",
        "transcriptId": "transcript-ordering-1",
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
    if not fields_ok:
        # Drop a required top-level field -> VoiceIntakePayload 422.
        del payload["callId"]
    if not tenant_ok:
        # A payload tenant that disagrees with the resolved channel -> 403.
        payload["tenant_id"] = channel_tenant + "-DIFFERENT"
    return payload


@st.composite
def _scenarios(draw) -> Dict[str, Any]:
    """Draw a scenario controlling each validation stage independently.

    Returns a dict of concrete bridge inputs plus the expected
    ``(status_code, error_code)`` of the earliest-failing stage, or ``None``
    when every stage passes.
    """
    sig_ok = draw(st.booleans())
    replay_ok = draw(st.booleans())
    channel_ok = draw(st.booleans())
    tenant_ok = draw(st.booleans())
    idem_ok = draw(st.booleans())
    idem_mode = draw(st.sampled_from(["missing", "conflict"]))
    schema_ok = draw(st.booleans())
    fields_ok = draw(st.booleans())

    # --- Signature input ---------------------------------------------------
    if sig_ok:
        signature: Optional[str] = VALID_SIGNATURE
    else:
        # Missing header or a value without the required sha256= prefix.
        signature = draw(st.sampled_from([None, "", "deadbeef", "md5=abc"]))

    # --- Replay-window input ----------------------------------------------
    if replay_ok:
        timestamp: Optional[str] = VALID_TIMESTAMP
    else:
        # Missing, unparseable, or stale (epoch 0 is decades outside window).
        timestamp = draw(st.sampled_from([None, "", "not-a-timestamp", "0"]))

    # --- Channel / tenant --------------------------------------------------
    channel = (
        FakeChannel(TENANT_ID, CHANNEL_ID, [SCHEMA_VERSION]) if channel_ok else None
    )

    # --- Idempotency -------------------------------------------------------
    prior: Optional[LedgerEntry] = None
    if idem_ok:
        idempotency_key: Optional[str] = VALID_IDEM_KEY
    elif idem_mode == "missing":
        idempotency_key = draw(st.sampled_from([None, ""]))
    else:  # conflict
        idempotency_key = VALID_IDEM_KEY
        # A recorded entry whose body hash never matches the presented body.
        prior = LedgerEntry(
            body_sha256="0" * 64,
            order_id="ord_prior_999",
            disposition="accepted",
        )

    # --- Schema version ----------------------------------------------------
    if schema_ok:
        schema_version: Optional[str] = SCHEMA_VERSION
    else:
        schema_version = draw(st.sampled_from([None, "", "9.9", "2.0"]))

    payload = _build_payload(
        fields_ok=fields_ok, tenant_ok=tenant_ok, channel_tenant=TENANT_ID
    )

    # --- Expected earliest-failing stage ----------------------------------
    expected: Optional[tuple] = None
    if not sig_ok:
        expected = _SIG_FAIL
    elif not replay_ok:
        expected = _REPLAY_FAIL
    elif not channel_ok:
        expected = _CHANNEL_FAIL
    elif not tenant_ok:
        expected = _TENANT_FAIL
    elif not idem_ok:
        expected = _IDEM_MISSING_FAIL if idem_mode == "missing" else _IDEM_CONFLICT_FAIL
    elif not schema_ok:
        expected = _SCHEMA_FAIL
    elif not fields_ok:
        expected = _FIELDS_FAIL

    return {
        "signature": signature,
        "timestamp": timestamp,
        "channel": channel,
        "prior": prior,
        "idempotency_key": idempotency_key,
        "schema_version": schema_version,
        "payload": payload,
        "expected": expected,
    }


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


class TestVoiceValidationOrdering:
    """# Feature: dinee-voice-integration, Property 1: Validation ordering

    **Validates: Requirements 2.3, 4.4, 5.3, 6.3**
    """

    @given(scenario=_scenarios())
    @settings(max_examples=100)
    def test_earliest_stage_error_wins(self, scenario: Dict[str, Any]) -> None:
        import json

        channel_repo = FakeChannelRepo(scenario["channel"])
        ledger = FakeLedger(scenario["prior"])
        pipeline = FakePipeline()

        bridge = DineeVoiceBridge(
            pipeline=pipeline,
            intake_channel_repo=channel_repo,
            ledger=ledger,
            replay_window_seconds=REPLAY_WINDOW_SECONDS,
            clock=lambda: FIXED_NOW,
        )

        raw_body = json.dumps(scenario["payload"]).encode("utf-8")

        async def _run():
            return await bridge.submit(
                raw_body=raw_body,
                tenant_id=TENANT_ID,
                idempotency_key=scenario["idempotency_key"],
                timestamp=scenario["timestamp"],
                schema_version=scenario["schema_version"],
                signature=scenario["signature"],
                request_id="req-ordering",
            )

        expected = scenario["expected"]

        if expected is None:
            # Every stage passes -> the submission reaches the pipeline exactly
            # once and yields an acceptance response.
            response = asyncio.run(_run())
            assert len(pipeline.calls) == 1
            assert response.orderId == "ord_ordering_123"
            assert response.disposition in ("accepted", "review_hold", "duplicate")
        else:
            expected_status, expected_code = expected
            with pytest.raises(AppException) as exc_info:
                asyncio.run(_run())
            exc = exc_info.value
            # The earliest-failing stage's error must win, exactly.
            assert exc.status_code == expected_status
            assert exc.error_code == expected_code
            # No order is persisted before the pipeline call: the bridge
            # short-circuits before ever invoking the pipeline (Req 4.4).
            assert pipeline.calls == []
            # A short-circuit also never records a ledger outcome.
            assert ledger.records == []
