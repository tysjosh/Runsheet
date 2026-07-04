"""
Property-based test for voice-channel resolution from the tenant.

# Feature: dinee-voice-integration, Property 7: Channel is resolved from the
tenant, not the client

**Validates: Requirements 2.4**

Property 7 (Channel is resolved from the tenant, not the client): For any
voice submission arriving at :class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge`,
the voice intake channel is resolved **exclusively** from the
``X-Runsheet-Tenant`` tenant via
``IntakeChannelRepository.get_voice_channel(tenant_id)`` — never from any
client-supplied channel identifier carried in the request body. Concretely:

* the bridge calls ``get_voice_channel`` with exactly the ``X-Runsheet-Tenant``
  tenant, and the ``channel_id`` handed to the pipeline is the *resolved*
  channel's id, even when the body carries a different ``channel_id`` /
  ``channelId`` value (Req 2.4);
* when the tenant has **no enabled voice channel**, the bridge raises a
  uniform HTTP 404 and never invokes the pipeline (Req 2.4); and
* when the body's ``tenant_id`` disagrees with the resolved channel's tenant,
  the bridge raises HTTP 403 and never invokes the pipeline (Req 2.4).

The test uses recording in-memory fakes (a channel repository keyed by
tenant, a recording pipeline, and a no-op ledger) so the "resolved from the
tenant, not the client" assertion is directly checkable without a live
Elasticsearch/Redis.
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
from fuel.intake_channel_models import IntakeChannel
from fuel.services.order_intake_pipeline import IntakeResponse
from fuel.voice.dinee_voice_bridge import DineeVoiceBridge

import json


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0"
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
# A well-formed X-Signature: the bridge only checks the ``sha256=`` prefix
# (the authoritative HMAC check lives in the pipeline, which is faked here).
VALID_SIGNATURE = "sha256=" + ("a" * 64)
# X-Timestamp equal to the injected clock → always inside the replay window.
IN_WINDOW_TIMESTAMP = str(int(FIXED_NOW.timestamp()))


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


class RecordingChannelRepo:
    """Fake IntakeChannelRepository that records every ``get_voice_channel``
    tenant argument and returns a channel keyed by tenant.

    ``resolver`` maps a tenant id to the :class:`IntakeChannel` (or ``None``)
    that this tenant resolves to, so the test controls the "enabled voice
    channel exists / does not exist" cases per tenant.
    """

    def __init__(self, resolver) -> None:
        self._resolver = resolver
        self.calls: List[str] = []

    async def get_voice_channel(self, tenant_id: str) -> Optional[IntakeChannel]:
        self.calls.append(tenant_id)
        return self._resolver(tenant_id)


class RecordingPipeline:
    """Fake OrderIntakePipeline recording each ``ingest_webhook`` call.

    Always returns a ``processed`` result so the bridge reaches its success
    path; the test asserts on the ``channel_id`` the bridge passed in.
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
                "body": body,
                "signature": signature,
                "idempotency_key_override": idempotency_key_override,
                "schema_version_override": schema_version_override,
            }
        )
        return IntakeResponse(
            event_id=idempotency_key_override or "evt",
            status="processed",
            order_id="ord_channel_resolution_test",
        )


class NoopLedger:
    """Fake VoiceSubmissionLedger: every submission looks new."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    async def lookup(self, tenant_id: str, key: str):
        return None

    async def record(self, tenant_id, key, body_sha256, order_id, disposition):
        self.records.append(
            {
                "tenant_id": tenant_id,
                "key": key,
                "body_sha256": body_sha256,
                "order_id": order_id,
                "disposition": disposition,
            }
        )


def _make_channel(tenant_id: str, channel_id: str) -> IntakeChannel:
    """Construct a valid, enabled voice IntakeChannel for a tenant."""
    return IntakeChannel(
        channel_id=channel_id,
        tenant_id=tenant_id,
        channel_type="voice",
        display_name="Voice Channel",
        hmac_secret_ref=f"voice_hmac:{tenant_id}:{channel_id}",
        supported_schema_versions=[SCHEMA_VERSION],
        enabled=True,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _build_bridge(repo: RecordingChannelRepo, pipeline: RecordingPipeline) -> DineeVoiceBridge:
    return DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=repo,
        ledger=NoopLedger(),
        replay_window_seconds=300,
        clock=lambda: FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Lowercase-alphanumeric tokens usable as tenant ids.
_tenant = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20
)
# Valid channel_id fragment: middle of ^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$.
_chan_mid = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=30
)
_non_empty = st.text(min_size=1, max_size=30)


@st.composite
def _valid_slots(draw) -> Dict[str, Any]:
    """A structurally valid VoiceExtractedSlots (bridge-level validity only)."""
    return {
        "customer_id": draw(_non_empty),
        "customer_name": draw(_non_empty),
        "ship_to_address": draw(_non_empty),
        "ship_to_lat": draw(
            st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False)
        ),
        "ship_to_lon": draw(
            st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False)
        ),
        "product_code": draw(_non_empty),
    }


@st.composite
def _valid_payload(draw) -> Dict[str, Any]:
    """A structurally valid VoiceIntakePayload dict (no tenant_id field)."""
    n = draw(st.integers(min_value=0, max_value=3))
    transcript = [
        {"speaker": draw(_non_empty), "text": draw(st.text(min_size=0, max_size=40))}
        for _ in range(n)
    ]
    return {
        "callId": draw(_non_empty),
        "transcriptId": draw(_non_empty),
        "transcript": transcript,
        "extractedSlots": draw(_valid_slots()),
        "reviewRequired": draw(st.booleans()),
    }


@st.composite
def _cases(draw) -> Dict[str, Any]:
    """Draw one of three channel-resolution scenarios.

    - ``resolve``      — the tenant has an enabled voice channel; the body may
      carry an adversarial client-supplied channel id that must be ignored.
    - ``no_channel``   — the tenant has no enabled voice channel → 404.
    - ``mismatch``     — the body's ``tenant_id`` disagrees with the resolved
      channel's tenant → 403.
    """
    scenario = draw(st.sampled_from(["resolve", "no_channel", "mismatch"]))
    tenant_id = draw(_tenant)
    channel_id = "c" + draw(_chan_mid) + "0"  # valid ^[a-z0-9][a-z0-9\-]*[a-z0-9]$
    payload = draw(_valid_payload())

    # A client-supplied channel id distinct from the resolved one (Req 2.4:
    # it must never be honored).
    client_channel = "client-supplied-" + draw(_chan_mid)
    payload["channel_id"] = client_channel
    payload["channelId"] = client_channel

    if scenario == "mismatch":
        # A payload tenant that disagrees with the resolved channel's tenant.
        payload["tenant_id"] = tenant_id + "-other"

    return {
        "scenario": scenario,
        "tenant_id": tenant_id,
        "channel_id": channel_id,
        "client_channel": client_channel,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


class TestVoiceChannelResolution:
    """# Feature: dinee-voice-integration, Property 7: Channel is resolved
    from the tenant, not the client

    **Validates: Requirements 2.4**
    """

    @given(case=_cases())
    @settings(max_examples=100)
    def test_channel_resolved_from_tenant_not_client(self, case: Dict[str, Any]):
        scenario = case["scenario"]
        tenant_id = case["tenant_id"]
        channel_id = case["channel_id"]
        client_channel = case["client_channel"]
        raw_body = json.dumps(case["payload"]).encode()

        if scenario == "no_channel":
            resolver = lambda t: None
        else:
            channel = _make_channel(tenant_id, channel_id)
            resolver = lambda t, _c=channel: _c if t == tenant_id else None

        repo = RecordingChannelRepo(resolver)
        pipeline = RecordingPipeline()
        bridge = _build_bridge(repo, pipeline)

        async def _run():
            return await bridge.submit(
                raw_body=raw_body,
                tenant_id=tenant_id,
                idempotency_key="idem-" + tenant_id,
                timestamp=IN_WINDOW_TIMESTAMP,
                schema_version=SCHEMA_VERSION,
                signature=VALID_SIGNATURE,
            )

        if scenario == "resolve":
            response = asyncio.run(_run())

            # The channel was resolved using the X-Runsheet-Tenant tenant —
            # exactly once, with exactly that tenant (never a client value).
            assert repo.calls == [tenant_id]

            # The pipeline was invoked with the *resolved* channel id, not the
            # client-supplied one embedded in the body (Req 2.4).
            assert len(pipeline.calls) == 1
            assert pipeline.calls[0]["channel_id"] == channel_id
            assert pipeline.calls[0]["channel_id"] != client_channel

            # A successful acceptance carrying the pipeline order id.
            assert response.orderId == "ord_channel_resolution_test"

        elif scenario == "no_channel":
            with pytest.raises(AppException) as excinfo:
                asyncio.run(_run())

            # Uniform 404 — no enabled voice channel for the tenant (Req 2.4).
            assert excinfo.value.status_code == 404
            assert excinfo.value.error_code == ErrorCode.RESOURCE_NOT_FOUND

            # Resolution was attempted from the tenant; the pipeline was never
            # reached (no order is persisted).
            assert repo.calls == [tenant_id]
            assert pipeline.calls == []

        else:  # mismatch
            with pytest.raises(AppException) as excinfo:
                asyncio.run(_run())

            # Payload tenant disagreeing with the resolved channel → 403.
            assert excinfo.value.status_code == 403
            assert excinfo.value.error_code == ErrorCode.VOICE_TENANT_MISMATCH

            # The channel was still resolved from the tenant, and the pipeline
            # was never invoked.
            assert repo.calls == [tenant_id]
            assert pipeline.calls == []
