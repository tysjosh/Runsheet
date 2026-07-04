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
        # call_type defaults to "one_off", which requires a delivery window on
        # the canonical FuelOrder; supply a valid one so these tests exercise
        # the customer_id behavior in isolation.
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
