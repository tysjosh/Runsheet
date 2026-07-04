"""
Property-based test for the Voice intake adapter's channel/metadata stamping.

# Feature: dinee-voice-integration, Property 6: Adapter sets voice channel and metadata

**Validates: Requirements 1.5**

Property 6 (Adapter sets voice channel and metadata): For any structurally
valid ``VoiceIntakePayload``, ``VoiceIntakeAdapter.transform`` stamps
``intake_channel="voice"``, ``intake_channel_id`` from the resolved context
channel, and populates ``intake_metadata.call_id`` / ``transcript`` /
``agent_confidence`` from the submitted payload; it emits exactly one
``order_placed`` event and never sets the platform-owned fields
``order_id`` / ``tenant_id`` / ``status`` / timestamps / ``trace_id``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from fuel.intake.adapter_base import IntakeContext, IntakeResult
from fuel.intake.voice_intake_adapter import VoiceIntakeAdapter
from fuel.intake_channel_models import IntakeChannel


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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

# Platform-owned fields the adapter must never set (per adapter contract).
_PLATFORM_FIELDS = ("order_id", "tenant_id", "status", "trace_id")
_TIMESTAMP_FIELDS = ("created_at", "updated_at", "placed_at", "timestamp")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_text = st.text(min_size=1, max_size=40)


@st.composite
def _transcript_turns(draw) -> List[Dict[str, str]]:
    n = draw(st.integers(min_value=0, max_value=5))
    return [
        {"speaker": draw(_text), "text": draw(st.text(min_size=0, max_size=60))}
        for _ in range(n)
    ]


@st.composite
def _voice_payloads(draw) -> Dict[str, Any]:
    """Generate structurally valid VoiceIntakePayload dicts."""
    slots: Dict[str, Any] = {
        "customer_name": draw(_text),
        "ship_to_address": draw(_text),
        "ship_to_lat": draw(
            st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False)
        ),
        "ship_to_lon": draw(
            st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False)
        ),
        "product_code": draw(st.sampled_from(_PRODUCT_CODES)),
        "fill_to_full": draw(st.booleans()),
        "call_type": draw(
            st.sampled_from(["will_call", "auto_fill", "keep_full", "one_off"])
        ),
    }
    # Optional slot fields.
    if draw(st.booleans()):
        slots["customer_id"] = draw(_text)
    if draw(st.booleans()):
        slots["gallons_requested"] = draw(
            st.floats(min_value=0, max_value=50000, allow_nan=False, allow_infinity=False)
        )
    if draw(st.booleans()):
        slots["customer_tank_id"] = draw(_text)

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
    if draw(st.booleans()):
        payload["schemaVersion"] = draw(st.sampled_from(["1.0", "0.9", "2.0"]))
    return payload


def _make_context(channel_id: str = "voice-chan-abc") -> IntakeContext:
    now = datetime.now(timezone.utc)
    channel = IntakeChannel(
        channel_id=channel_id,
        tenant_id="tenant-xyz",
        channel_type="voice",
        display_name="Voice Channel",
        hmac_secret_ref="voice_hmac:tenant-xyz",
        supported_schema_versions=["1.0"],
        created_at=now,
        updated_at=now,
    )
    return IntakeContext(
        tenant_id="tenant-xyz",
        channel=channel,
        trace_id="trace-123",
        request_id="req-456",
    )


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


class TestVoiceAdapterChannelMetadata:
    """# Feature: dinee-voice-integration, Property 6: Adapter sets voice channel and metadata

    **Validates: Requirements 1.5**
    """

    @given(payload=_voice_payloads(), channel_id=st.sampled_from(
        ["voice-chan-abc", "tenant-1-voice", "dinee-voice-99"]))
    @settings(max_examples=100)
    def test_adapter_stamps_channel_and_metadata(
        self, payload: Dict[str, Any], channel_id: str
    ):
        context = _make_context(channel_id)
        adapter = VoiceIntakeAdapter()

        result = adapter.transform(payload, context)
        assert isinstance(result, IntakeResult)
        order_doc = result.order_doc

        # intake_channel is always "voice".
        assert order_doc["intake_channel"] == "voice"
        # intake_channel_id is taken from the resolved context channel.
        assert order_doc["intake_channel_id"] == channel_id

        # intake_metadata is populated from the payload.
        metadata = order_doc["intake_metadata"]
        assert metadata["call_id"] == payload["callId"]
        assert metadata["agent_confidence"] == payload.get("agentConfidence")

        # transcript reflects the payload transcript (joined turns or None).
        turns = payload["transcript"]
        if not turns:
            assert metadata["transcript"] is None
        else:
            expected = "\n".join(f"{t['speaker']}: {t['text']}" for t in turns)
            assert metadata["transcript"] == expected

        # Exactly one order_placed event is emitted.
        assert len(result.event_docs) == 1
        assert result.event_docs[0]["event_type"] == "order_placed"

        # The adapter never sets platform-owned fields.
        for field in _PLATFORM_FIELDS:
            assert field not in order_doc, f"adapter must not set {field}"
        for field in _TIMESTAMP_FIELDS:
            assert field not in order_doc, f"adapter must not set timestamp {field}"
