"""Unit tests for the Dinee-alignment behaviors of ``VoiceIntakeAdapter``.

Covers the two changes made for the Dinee client contract (assumption A5) and
the "accept unresolved customer" decision (design option A):

1. Transcript turns accept ``role`` as an alias for ``speaker`` (and still
   accept ``speaker``), tolerating the extra ``at`` key.
2. A payload whose ``extractedSlots.customer_id`` is absent/blank still yields
   a canonical FuelOrder-valid ``order_doc`` — a provisional ``customer_id`` is
   stamped, the order is forced into review-hold, and ``intake_metadata`` flags
   the caller as unresolved. This is what previously produced an uncaught
   ``FuelOrder`` validation error (HTTP 500).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest

from fuel.intake.adapter_base import IntakeContext
from fuel.intake.voice_intake_adapter import (
    UNRESOLVED_CUSTOMER_PREFIX,
    VOICE_REVIEW_HOLD_REASON,
    TranscriptTurn,
    VoiceIntakeAdapter,
)
from fuel.order_models import FuelOrder


def _context(channel_id: str = "voice-demo-01") -> IntakeContext:
    channel = SimpleNamespace(channel_id=channel_id, tenant_id="demo-tenant")
    return IntakeContext(
        tenant_id="demo-tenant",
        channel=channel,
        trace_id="trace-1",
        request_id="req-1",
        actor_user_id=None,
    )


def _payload(**slot_overrides: Any) -> Dict[str, Any]:
    slots: Dict[str, Any] = {
        "customer_name": "Acme Co",
        "ship_to_address": "123 Depot Rd",
        "ship_to_lat": 40.0,
        "ship_to_lon": -75.0,
        "product_code": "propane",
        "gallons_requested": 500.0,
        # call_type now defaults to "will_call", so no delivery window is
        # required; keep one here for the customer_id tests either way.
        "delivery_window_start": "2026-07-04T09:00:00+00:00",
        "delivery_window_end": "2026-07-04T12:00:00+00:00",
    }
    slots.update(slot_overrides)
    return {
        "callId": "call-1",
        "transcriptId": "tr-1",
        "transcript": [{"role": "customer", "text": "I need fuel", "at": 12}],
        "callerPhone": "+15555550100",
        "extractedSlots": slots,
        "reviewRequired": True,
    }


def _stamp_platform_fields(order_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror the platform-owned fields the pipeline stamps before validation."""
    doc = dict(order_doc)
    doc.update(
        order_id="ord_test",
        tenant_id="demo-tenant",
        status="placed",
        created_at="2026-07-04T00:00:00+00:00",
        updated_at="2026-07-04T00:00:00+00:00",
        last_event_timestamp="2026-07-04T00:00:00+00:00",
        trace_id="trace-1",
    )
    return doc


# ---------------------------------------------------------------------------
# Transcript role/speaker alias
# ---------------------------------------------------------------------------


def test_transcript_turn_accepts_role_alias():
    turn = TranscriptTurn.model_validate({"role": "agent", "text": "hi", "at": 1})
    assert turn.speaker == "agent"
    assert turn.text == "hi"


def test_transcript_turn_still_accepts_speaker():
    turn = TranscriptTurn.model_validate({"speaker": "customer", "text": "hello"})
    assert turn.speaker == "customer"


def test_transcript_turn_requires_role_or_speaker():
    with pytest.raises(Exception):
        TranscriptTurn.model_validate({"text": "orphaned turn"})


def test_adapter_joins_transcript_using_aliased_speaker():
    adapter = VoiceIntakeAdapter()
    result = adapter.transform(_payload(), _context())
    transcript = result.order_doc["intake_metadata"]["transcript"]
    assert transcript == "customer: I need fuel"


# ---------------------------------------------------------------------------
# Unresolved customer (design option A)
# ---------------------------------------------------------------------------


def test_missing_customer_id_stamps_provisional_and_validates():
    adapter = VoiceIntakeAdapter()
    result = adapter.transform(_payload(), _context())  # no customer_id
    order_doc = result.order_doc

    assert order_doc["customer_id"] == f"{UNRESOLVED_CUSTOMER_PREFIX}call-1"
    assert VoiceIntakeAdapter.is_unresolved_customer_id(order_doc["customer_id"])
    assert order_doc["hold_reason"] == VOICE_REVIEW_HOLD_REASON

    # The whole point: this now survives FuelOrder validation (was a 500).
    FuelOrder.model_validate(_stamp_platform_fields(order_doc))


def test_blank_customer_id_is_treated_as_unresolved():
    adapter = VoiceIntakeAdapter()
    result = adapter.transform(_payload(customer_id="   "), _context())
    order_doc = result.order_doc
    assert order_doc["customer_id"] == f"{UNRESOLVED_CUSTOMER_PREFIX}call-1"
    assert VoiceIntakeAdapter.is_unresolved_customer_id(order_doc["customer_id"])


def test_unresolved_customer_forces_review_even_when_not_flagged():
    adapter = VoiceIntakeAdapter()
    payload = _payload()
    payload["reviewRequired"] = False  # agent did not request review
    result = adapter.transform(payload, _context())
    # Unresolved customer must still force review-hold.
    assert result.order_doc["hold_reason"] == VOICE_REVIEW_HOLD_REASON


def test_resolved_customer_id_is_preserved():
    adapter = VoiceIntakeAdapter()
    result = adapter.transform(_payload(customer_id="cust-123"), _context())
    order_doc = result.order_doc
    assert order_doc["customer_id"] == "cust-123"
    assert not VoiceIntakeAdapter.is_unresolved_customer_id(order_doc["customer_id"])
    FuelOrder.model_validate(_stamp_platform_fields(order_doc))


def test_resolved_customer_without_review_flag_is_not_held():
    adapter = VoiceIntakeAdapter()
    payload = _payload(customer_id="cust-123")
    payload["reviewRequired"] = False
    result = adapter.transform(payload, _context())
    assert "hold_reason" not in result.order_doc


# ---------------------------------------------------------------------------
# Dinee slot-contract alignment (A5)
# ---------------------------------------------------------------------------


def _dinee_payload(**slot_overrides: Any) -> Dict[str, Any]:
    """A payload shaped like Dinee's real runsheet-pack output."""
    slots: Dict[str, Any] = {
        "customer": "Acme Co",             # -> customer_name
        "delivery_site": "123 Depot Rd",   # -> ship_to_address
        "product_code": "propane",
        "quantity": {"gallons": 500},      # -> gallons_requested
        "delivery_window": "tomorrow morning",  # -> delivery_window_note
        # no coordinates, no call_type
    }
    slots.update(slot_overrides)
    return {
        "callId": "call-dinee",
        "transcriptId": "tr-dinee",
        "transcript": [{"role": "customer", "text": "need fuel", "at": 3}],
        "callerPhone": "+15555550100",
        "extractedSlots": slots,
        "reviewRequired": True,
    }


def test_dinee_shaped_payload_validates_end_to_end():
    adapter = VoiceIntakeAdapter()
    result = adapter.transform(_dinee_payload(), _context())
    doc = result.order_doc

    assert doc["customer_name"] == "Acme Co"          # customer alias
    assert doc["ship_to_address"] == "123 Depot Rd"   # delivery_site alias
    assert doc["gallons_requested"] == 500            # quantity unpacked
    assert doc["call_type"] == "will_call"            # default
    assert doc["ship_to_lat"] is None                 # no coords
    assert doc["ship_to_lon"] is None
    assert "tomorrow morning" in doc["special_instructions"]

    # Survives canonical FuelOrder validation (voice channel exempts coords,
    # will_call exempts the delivery window).
    FuelOrder.model_validate(_stamp_platform_fields(doc))


def test_quantity_fill_to_full_shape():
    adapter = VoiceIntakeAdapter()
    result = adapter.transform(
        _dinee_payload(quantity={"fillToFull": True}), _context()
    )
    doc = result.order_doc
    assert doc["fill_to_full"] is True
    assert doc["gallons_requested"] is None
    FuelOrder.model_validate(_stamp_platform_fields(doc))


def test_explicit_snake_case_still_accepted():
    # Backward compatible: the canonical snake_case keys still work.
    adapter = VoiceIntakeAdapter()
    payload = _payload(customer_id="cust-1")  # uses customer_name/ship_to_address
    result = adapter.transform(payload, _context())
    assert result.order_doc["customer_name"] == "Acme Co"
    assert result.order_doc["ship_to_address"] == "123 Depot Rd"


def test_confidence_score_alias_populates_agent_confidence():
    # Dinee sends confidenceScore; it must land on intake_metadata.agent_confidence.
    adapter = VoiceIntakeAdapter()
    payload = _dinee_payload()
    payload["confidenceScore"] = 0.87
    result = adapter.transform(payload, _context())
    assert result.order_doc["intake_metadata"]["agent_confidence"] == 0.87


def test_agent_confidence_canonical_still_accepted():
    adapter = VoiceIntakeAdapter()
    payload = _dinee_payload()
    payload["agentConfidence"] = 0.5
    result = adapter.transform(payload, _context())
    assert result.order_doc["intake_metadata"]["agent_confidence"] == 0.5
