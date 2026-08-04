"""
Property-based test for the Dinee voice bridge acceptance response shape and
rejection hygiene.

# Feature: dinee-voice-integration, Property 12: Acceptance response shape and
rejection hygiene

**Validates: Requirements 9.1, 9.3**

Property 12 has two halves, both exercised against the real
:class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge` (with recording fakes
for the pipeline, the intake-channel repository, and the
:class:`~fuel.voice.voice_submission_ledger.VoiceSubmissionLedger`):

* **Acceptance shape (Req 9.1).** *For any* accepted voice submission the
  bridge returns a :class:`~fuel.voice.voice_models.VoiceSubmissionResponse`
  carrying the pipeline-assigned order id and a disposition drawn from the
  closed set ``{accepted, review_hold, duplicate}``. A fresh ``processed``
  result maps to ``review_hold`` when ``reviewRequired`` is true and
  ``accepted`` otherwise; a same-key/same-body replay recalls the prior order
  id with the prior disposition (Req 9.2 recall, surfaced here as a valid
  acceptance shape).

* **Rejection hygiene (Req 9.3).** *For any* rejection — driven across every
  failure stage the bridge enforces (signature 401, replay 401, channel 404,
  tenant-mismatch 403, missing-idempotency 400, idempotency-conflict 409,
  unsupported-schema 422, and required-field 422) — the raised
  :class:`~errors.exceptions.AppException`'s serialized envelope (``to_dict``),
  human ``message``, and ``repr`` never contain tenant data, transcript
  content, or credential values (HMAC secret, API key, or the raw signature).

The hygiene-assertion helper mirrors the pattern used by
``tests/property/test_voice_bearer_auth_property.py``; the failure-stage
driving mirrors ``tests/property/test_voice_validation_ordering_property.py``.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.strategies import from_regex

from errors.exceptions import AppException
from fuel.services.order_intake_pipeline import IntakeResponse
from fuel.voice.dinee_voice_bridge import DineeVoiceBridge
from fuel.voice.voice_models import VoiceSubmissionResponse
from fuel.voice.voice_submission_ledger import LedgerEntry


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0"
REPLAY_WINDOW_SECONDS = 300
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

# A well-formed signature: the required ``sha256=`` prefix plus 64 lowercase
# hex characters. Distinctive enough that a leak of the raw value is visible.
VALID_SIGNATURE = "sha256=" + ("deadbeef" * 8)
VALID_TIMESTAMP = FIXED_NOW.isoformat()
VALID_IDEM_KEY = "idem-hygiene-key"


# ---------------------------------------------------------------------------
# Recording fakes (mirroring the validation-ordering property test)
# ---------------------------------------------------------------------------


class FakeChannel:
    """Stand-in for the resolved voice ``IntakeChannel``.

    Carries a distinctive ``hmac_secret`` so the hygiene assertion can prove
    the credential value never surfaces in a rejection (the bridge must never
    read or echo it).
    """

    def __init__(
        self,
        tenant_id: str,
        channel_id: str,
        supported: List[str],
        hmac_secret: str,
    ) -> None:
        self.tenant_id = tenant_id
        self.channel_id = channel_id
        self.supported_schema_versions = supported
        self.hmac_secret = hmac_secret


class FakeChannelRepo:
    """Recording fake for ``IntakeChannelRepository.get_voice_channel``."""

    def __init__(self, channel: Optional[FakeChannel]) -> None:
        self._channel = channel
        self.lookups: List[str] = []

    async def get_voice_channel(self, tenant_id: str) -> Optional[FakeChannel]:
        self.lookups.append(tenant_id)
        return self._channel


class FakeLedger:
    """Recording fake for ``VoiceSubmissionLedger`` (lookup + record)."""

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
    """Recording fake for ``OrderIntakePipeline.ingest_webhook``.

    Returns a configurable status/order id so the acceptance-shape half can
    drive ``processed`` and ``duplicate`` outcomes.
    """

    def __init__(self, status: str = "processed", order_id: str = "ord_hygiene_1") -> None:
        self._status = status
        self._order_id = order_id
        self.calls: List[Dict[str, Any]] = []

    async def ingest_webhook(self, **kwargs: Any) -> IntakeResponse:
        self.calls.append(kwargs)
        return IntakeResponse(
            event_id=kwargs.get("idempotency_key_override") or "",
            status=self._status,
            order_id=self._order_id if self._status == "processed" else None,
        )


# ---------------------------------------------------------------------------
# Hygiene helper (mirrors test_voice_bearer_auth_property._assert_no_leak)
# ---------------------------------------------------------------------------


def _assert_no_leak(exc: AppException, secrets: List[str]) -> None:
    """Assert no tenant data / transcript / credential value appears anywhere.

    Checks the three surfaces an error could leak through: the
    JSON-serializable body (``to_dict``), the human ``message``, and ``repr``.
    """
    surfaces = [str(exc.to_dict()), str(exc.message), repr(exc)]
    for secret in secrets:
        if not secret:
            continue
        for surface in surfaces:
            assert secret not in surface, (
                f"rejection leaked sensitive value {secret!r} in {surface!r}"
            )


def _build_bridge(
    *,
    channel: Optional[FakeChannel],
    prior: Optional[LedgerEntry],
    pipeline: FakePipeline,
) -> tuple[DineeVoiceBridge, FakeLedger]:
    ledger = FakeLedger(prior)
    bridge = DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=FakeChannelRepo(channel),
        ledger=ledger,
        replay_window_seconds=REPLAY_WINDOW_SECONDS,
        clock=lambda: FIXED_NOW,
    )
    return bridge, ledger


def _valid_payload(
    *,
    transcript_marker: str,
    tenant_override: Optional[str] = None,
    drop_call_id: bool = False,
    review_required: bool = True,
) -> Dict[str, Any]:
    """Build a JSON-object voice body embedding a distinctive transcript marker."""
    payload: Dict[str, Any] = {
        "callId": "call-hygiene-1",
        "transcriptId": "transcript-hygiene-1",
        "transcript": [
            {"speaker": "caller", "text": transcript_marker},
            {"speaker": "agent", "text": f"ack {transcript_marker}"},
        ],
        "callerPhone": "+15551230000",
        "extractedSlots": {
            "customer_name": "Acme Fuels",
            "ship_to_address": "123 Depot Road",
            "ship_to_lat": 40.0,
            "ship_to_lon": -70.0,
            "product_code": "DIESEL_2",
        },
        "reviewRequired": review_required,
    }
    if tenant_override is not None:
        payload["tenant_id"] = tenant_override
    if drop_call_id:
        del payload["callId"]
    return payload


# ---------------------------------------------------------------------------
# Strategies — distinctive sensitive values that must never leak
# ---------------------------------------------------------------------------

_tenant_ids = from_regex(r"tenant-SEKRET-[a-z0-9]{8,16}", fullmatch=True)
_channel_ids = from_regex(r"chan-SEKRET-[a-z0-9]{8,16}", fullmatch=True)
_transcript_markers = from_regex(r"TRANSCRIPT-SEKRET-[a-z0-9]{8,16}", fullmatch=True)
_hmac_secrets = from_regex(r"HMACSECRET-[A-Za-z0-9]{16,32}", fullmatch=True)
_api_keys = from_regex(r"APIKEY-[A-Za-z0-9]{16,32}", fullmatch=True)


# The failure stages driven by the rejection-hygiene property.
_FAILURE_STAGES = [
    "signature",
    "replay",
    "channel",
    "tenant_mismatch",
    "missing_idempotency",
    "idempotency_conflict",
    "schema",
    "required_field",
]


# ---------------------------------------------------------------------------
# Property 12a — acceptance response shape (Req 9.1)
# ---------------------------------------------------------------------------


class TestAcceptanceResponseShape:
    """# Feature: dinee-voice-integration, Property 12 (acceptance shape)

    **Validates: Requirements 9.1**
    """

    @given(
        tenant_id=_tenant_ids,
        channel_id=_channel_ids,
        transcript_marker=_transcript_markers,
        hmac_secret=_hmac_secrets,
        review_required=st.booleans(),
        order_id=from_regex(r"ord_[a-z0-9]{8,16}", fullmatch=True),
    )
    @settings(max_examples=100)
    def test_fresh_acceptance_includes_order_id_and_disposition(
        self, tenant_id, channel_id, transcript_marker, hmac_secret, review_required, order_id
    ):
        channel = FakeChannel(tenant_id, channel_id, [SCHEMA_VERSION], hmac_secret)
        pipeline = FakePipeline(status="processed", order_id=order_id)
        bridge, ledger = _build_bridge(channel=channel, prior=None, pipeline=pipeline)

        raw_body = json.dumps(
            _valid_payload(
                transcript_marker=transcript_marker, review_required=review_required
            )
        ).encode("utf-8")

        async def scenario():
            return await bridge.submit(
                raw_body=raw_body,
                tenant_id=tenant_id,
                idempotency_key=VALID_IDEM_KEY,
                timestamp=VALID_TIMESTAMP,
                schema_version=SCHEMA_VERSION,
                signature=VALID_SIGNATURE,
                request_id="req-hygiene",
            )

        response = asyncio.run(scenario())

        # Req 9.1: the response carries the pipeline-assigned order id and a
        # disposition drawn from the closed acceptance set.
        assert isinstance(response, VoiceSubmissionResponse)
        assert response.orderId == order_id
        assert response.disposition in ("accepted", "review_hold", "duplicate")
        # reviewRequired maps deterministically to the review_hold disposition.
        assert response.disposition == ("review_hold" if review_required else "accepted")
        # The pipeline was invoked exactly once and the outcome recorded.
        assert len(pipeline.calls) == 1
        assert len(ledger.records) == 1

    @given(
        tenant_id=_tenant_ids,
        channel_id=_channel_ids,
        transcript_marker=_transcript_markers,
        hmac_secret=_hmac_secrets,
        prior_disposition=st.sampled_from(["accepted", "review_hold", "duplicate"]),
        prior_order_id=from_regex(r"ord_prior_[a-z0-9]{6,12}", fullmatch=True),
    )
    @settings(max_examples=100)
    def test_replay_recalls_prior_order_id_with_valid_disposition(
        self, tenant_id, channel_id, transcript_marker, hmac_secret, prior_disposition, prior_order_id
    ):
        channel = FakeChannel(tenant_id, channel_id, [SCHEMA_VERSION], hmac_secret)
        payload = _valid_payload(transcript_marker=transcript_marker)
        raw_body = json.dumps(payload).encode("utf-8")

        import hashlib

        prior = LedgerEntry(
            body_sha256=hashlib.sha256(raw_body).hexdigest(),
            order_id=prior_order_id,
            disposition=prior_disposition,
        )
        pipeline = FakePipeline(status="processed", order_id="ord_should_not_be_used")
        bridge, ledger = _build_bridge(channel=channel, prior=prior, pipeline=pipeline)

        async def scenario():
            return await bridge.submit(
                raw_body=raw_body,
                tenant_id=tenant_id,
                idempotency_key=VALID_IDEM_KEY,
                timestamp=VALID_TIMESTAMP,
                schema_version=SCHEMA_VERSION,
                signature=VALID_SIGNATURE,
                request_id="req-hygiene-replay",
            )

        response = asyncio.run(scenario())

        # Req 9.2 recall surfaced as a valid Req 9.1 acceptance shape: the
        # prior order id is returned and the pipeline is NOT re-invoked.
        assert response.orderId == prior_order_id
        assert response.disposition in ("accepted", "review_hold", "duplicate")
        assert pipeline.calls == []


# ---------------------------------------------------------------------------
# Property 12b — rejection hygiene (Req 9.3)
# ---------------------------------------------------------------------------


class TestRejectionHygiene:
    """# Feature: dinee-voice-integration, Property 12 (rejection hygiene)

    **Validates: Requirements 9.3**
    """

    @given(
        stage=st.sampled_from(_FAILURE_STAGES),
        tenant_id=_tenant_ids,
        channel_id=_channel_ids,
        transcript_marker=_transcript_markers,
        hmac_secret=_hmac_secrets,
        api_key=_api_keys,
        other_tenant=_tenant_ids,
    )
    @settings(max_examples=100)
    def test_rejection_never_leaks_sensitive_values(
        self, stage, tenant_id, channel_id, transcript_marker, hmac_secret, api_key, other_tenant
    ):
        import hashlib

        # Defaults: every stage passes unless this stage overrides it.
        signature: Optional[str] = VALID_SIGNATURE
        timestamp: Optional[str] = VALID_TIMESTAMP
        idempotency_key: Optional[str] = VALID_IDEM_KEY
        schema_version: Optional[str] = SCHEMA_VERSION
        channel: Optional[FakeChannel] = FakeChannel(
            tenant_id, channel_id, [SCHEMA_VERSION], hmac_secret
        )
        prior: Optional[LedgerEntry] = None
        tenant_override: Optional[str] = None
        drop_call_id = False

        # The (wrong) payload tenant used only by the mismatch stage; it is
        # tenant data too, so it must also never leak.
        payload_tenant: Optional[str] = None

        if stage == "signature":
            signature = None
        elif stage == "replay":
            timestamp = "not-a-timestamp"
        elif stage == "channel":
            channel = None
        elif stage == "tenant_mismatch":
            if other_tenant == tenant_id:
                other_tenant = tenant_id + "-x"
            tenant_override = other_tenant
            payload_tenant = other_tenant
        elif stage == "missing_idempotency":
            idempotency_key = None
        elif stage == "idempotency_conflict":
            prior = LedgerEntry(
                body_sha256="0" * 64,  # never matches the presented body
                order_id="ord_prior_conflict",
                disposition="accepted",
            )
        elif stage == "schema":
            schema_version = "9.9"
        elif stage == "required_field":
            drop_call_id = True

        payload = _valid_payload(
            transcript_marker=transcript_marker,
            tenant_override=tenant_override,
            drop_call_id=drop_call_id,
        )
        raw_body = json.dumps(payload).encode("utf-8")

        pipeline = FakePipeline(status="processed", order_id="ord_unreachable")
        bridge, ledger = _build_bridge(channel=channel, prior=prior, pipeline=pipeline)

        async def scenario():
            return await bridge.submit(
                raw_body=raw_body,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                timestamp=timestamp,
                schema_version=schema_version,
                signature=signature,
                request_id="req-hygiene-reject",
            )

        with pytest.raises(AppException) as exc_info:
            asyncio.run(scenario())
        exc = exc_info.value

        # Every rejection carries an HTTP status in the documented set.
        assert exc.status_code in (400, 401, 403, 404, 409, 422)

        # Req 9.3: no tenant data, transcript content, or credential value
        # (HMAC secret, API key, raw signature/hash) leaks through any surface.
        hashed_body = hashlib.sha256(raw_body).hexdigest()
        _assert_no_leak(
            exc,
            [
                tenant_id,
                channel_id,
                payload_tenant,
                transcript_marker,
                f"ack {transcript_marker}",
                hmac_secret,
                api_key,
                VALID_SIGNATURE,
                ("deadbeef" * 8),
                hashed_body,
            ],
        )
