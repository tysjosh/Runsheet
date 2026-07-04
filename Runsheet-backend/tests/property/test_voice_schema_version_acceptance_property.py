"""
Property-based test for voice submission schema-version acceptance.

# Feature: dinee-voice-integration, Property 9: Schema-version acceptance

**Validates: Requirements 6.1, 6.2, 6.3**

Property 9 (Schema-version acceptance): For any voice submission that has
already passed the earlier validation stages (signature, replay window,
tenant/voice-channel resolution, and idempotency), the
:class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge` decides the
submission's fate solely on the ``X-Schema-Version`` header against the
resolved channel's ``supported_schema_versions``:

* a **supported** version (present in ``channel.supported_schema_versions``)
  proceeds to the pipeline, and the exact header value is forwarded as the
  pipeline's ``schema_version_override`` (Req 6.1);
* an **unsupported** version (present but not in the supported set) is
  rejected with HTTP 422 ``UNSUPPORTED_SCHEMA_VERSION`` and the pipeline is
  never invoked (Req 6.2); and
* a **missing** ``X-Schema-Version`` header (``None`` / empty) is rejected
  with HTTP 422 ``UNSUPPORTED_SCHEMA_VERSION`` and the pipeline is never
  invoked (Req 6.3).

The test uses recording in-memory fakes (a channel repository returning a
channel with a known supported set, a recording pipeline, and a no-op ledger)
so the schema-version decision is observed directly without a live
Elasticsearch/Redis. Every other validation stage is held valid so the
submission always reaches — and is decided by — the schema-version check.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.intake_channel_models import IntakeChannel
from fuel.services.order_intake_pipeline import IntakeResponse
from fuel.voice.dinee_voice_bridge import DineeVoiceBridge


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-schema"
CHANNEL_ID = "voice-schema-chan-01"
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
# A well-formed X-Signature: the bridge only checks the ``sha256=`` prefix
# (the authoritative HMAC check lives in the pipeline, which is faked here).
VALID_SIGNATURE = "sha256=" + ("a" * 64)
# X-Timestamp equal to the injected clock → always inside the replay window.
IN_WINDOW_TIMESTAMP = str(int(FIXED_NOW.timestamp()))
VALID_IDEM_KEY = "idem-schema-key"


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


class RecordingChannelRepo:
    """Fake IntakeChannelRepository returning a fixed enabled voice channel."""

    def __init__(self, channel: Optional[IntakeChannel]) -> None:
        self._channel = channel
        self.calls: List[str] = []

    async def get_voice_channel(self, tenant_id: str) -> Optional[IntakeChannel]:
        self.calls.append(tenant_id)
        return self._channel


class RecordingPipeline:
    """Fake OrderIntakePipeline recording each ``ingest_webhook`` call.

    Always returns a ``processed`` result so the *only* reason it would not be
    called is a bridge-side schema-version short-circuit.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def ingest_webhook(
        self,
        *,
        channel_id: str,
        body: bytes,
        signature: str,
        request_id: str,
        idempotency_key_override: Optional[str] = None,
        schema_version_override: Optional[str] = None,
    ) -> IntakeResponse:
        self.calls.append(
            {
                "channel_id": channel_id,
                "schema_version_override": schema_version_override,
                "idempotency_key_override": idempotency_key_override,
            }
        )
        return IntakeResponse(
            event_id=idempotency_key_override or "evt",
            status="processed",
            order_id="ord_schema_version_test",
        )


class NoopLedger:
    """Fake VoiceSubmissionLedger: every submission looks new."""

    def __init__(self) -> None:
        self.records: List[Any] = []

    async def lookup(self, tenant_id: str, key: str):
        return None

    async def record(self, *args: Any) -> None:
        self.records.append(args)


def _make_channel(supported: List[str]) -> IntakeChannel:
    """Construct a valid, enabled voice IntakeChannel with a supported set."""
    return IntakeChannel(
        channel_id=CHANNEL_ID,
        tenant_id=TENANT_ID,
        channel_type="voice",
        display_name="Voice Channel",
        hmac_secret_ref=f"voice_hmac:{TENANT_ID}:{CHANNEL_ID}",
        supported_schema_versions=supported,
        enabled=True,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _valid_payload() -> Dict[str, Any]:
    """A structurally valid VoiceIntakePayload dict (no tenant_id field).

    Kept structurally valid so a supported version reaches the pipeline rather
    than tripping the *later* required-field stage.
    """
    return {
        "callId": "call-schema-1",
        "transcriptId": "transcript-schema-1",
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


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Version-like tokens, e.g. "1.0", "2", "3.5" — a small space so "supported"
# and "unsupported" partitions are easy to construct disjointly.
_version = st.builds(
    lambda major, minor: f"{major}.{minor}" if minor is not None else str(major),
    st.integers(min_value=0, max_value=9),
    st.one_of(st.none(), st.integers(min_value=0, max_value=9)),
)


@st.composite
def _cases(draw) -> Dict[str, Any]:
    """Draw a schema-version scenario: supported / unsupported / missing.

    Returns the resolved channel's supported set plus the ``X-Schema-Version``
    header value to present and the expected outcome.
    """
    scenario = draw(st.sampled_from(["supported", "unsupported", "missing"]))

    # A non-empty supported set of distinct versions.
    supported = draw(
        st.lists(_version, min_size=1, max_size=5, unique=True)
    )

    if scenario == "supported":
        header_version: Optional[str] = draw(st.sampled_from(supported))
    elif scenario == "unsupported":
        # A version guaranteed not to be in the supported set.
        candidate = draw(_version.filter(lambda v: v not in supported))
        header_version = candidate
    else:  # missing
        header_version = draw(st.sampled_from([None, ""]))

    return {
        "scenario": scenario,
        "supported": supported,
        "header_version": header_version,
    }


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------


class TestVoiceSchemaVersionAcceptance:
    """# Feature: dinee-voice-integration, Property 9: Schema-version acceptance

    **Validates: Requirements 6.1, 6.2, 6.3**
    """

    @given(case=_cases())
    @settings(max_examples=100)
    def test_schema_version_decides_submission(self, case: Dict[str, Any]) -> None:
        scenario = case["scenario"]
        supported = case["supported"]
        header_version = case["header_version"]

        channel = _make_channel(supported)
        repo = RecordingChannelRepo(channel)
        pipeline = RecordingPipeline()
        ledger = NoopLedger()

        bridge = DineeVoiceBridge(
            pipeline=pipeline,
            intake_channel_repo=repo,
            ledger=ledger,
            replay_window_seconds=300,
            clock=lambda: FIXED_NOW,
        )

        raw_body = json.dumps(_valid_payload()).encode("utf-8")

        async def _run():
            return await bridge.submit(
                raw_body=raw_body,
                tenant_id=TENANT_ID,
                idempotency_key=VALID_IDEM_KEY,
                timestamp=IN_WINDOW_TIMESTAMP,
                schema_version=header_version,
                signature=VALID_SIGNATURE,
                request_id="req-schema",
            )

        if scenario == "supported":
            # Req 6.1 — a supported version proceeds to the pipeline, and the
            # exact header value is forwarded as schema_version_override.
            response = asyncio.run(_run())
            assert len(pipeline.calls) == 1
            assert pipeline.calls[0]["schema_version_override"] == header_version
            assert pipeline.calls[0]["channel_id"] == CHANNEL_ID
            assert response.orderId == "ord_schema_version_test"
        else:
            # Req 6.2 (unsupported) / Req 6.3 (missing) — HTTP 422 and the
            # pipeline is never invoked (no order persisted).
            with pytest.raises(AppException) as exc_info:
                asyncio.run(_run())
            exc = exc_info.value
            assert exc.status_code == 422
            assert exc.error_code == ErrorCode.UNSUPPORTED_SCHEMA_VERSION
            assert pipeline.calls == []
            assert ledger.records == []
