"""
Property-based tests for Event Payload Round-Trip.

# Feature: logistics-scheduling, Property 7: Event Payload Round-Trip

**Validates: Requirement 15.5**

For all valid job event sequences, serializing then deserializing the
event_payload SHALL produce an equivalent object (round-trip property
for event serialization).
"""

import json
import pytest

from hypothesis import given, settings
from hypothesis.strategies import (
    booleans,
    dictionaries,
    floats,
    integers,
    lists,
    none,
    recursive,
    text,
    uuids,
    sampled_from,
)

from scheduling.models import JobEvent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EVENT_TYPES = [
    "job_created",
    "asset_assigned",
    "asset_reassigned",
    "status_changed",
    "cargo_updated",
    "cargo_status_changed",
]

TENANT_ID = "tenant_test"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# JSON-compatible leaf values (no NaN/Inf which aren't valid JSON)
_json_leaves = (
    none()
    | booleans()
    | integers(min_value=-(2**53), max_value=2**53)
    | floats(allow_nan=False, allow_infinity=False)
    | text(max_size=50)
)

# Recursive JSON-compatible structures (dicts and lists of leaves)
_json_values = recursive(
    _json_leaves,
    lambda children: (
        lists(children, max_size=5)
        | dictionaries(text(max_size=20), children, max_size=5)
    ),
    max_leaves=20,
)

# Strategy for arbitrary event_payload dicts
_event_payloads = dictionaries(
    text(min_size=1, max_size=30),
    _json_values,
    min_size=0,
    max_size=10,
)

_event_types = sampled_from(EVENT_TYPES)


# ---------------------------------------------------------------------------
# Property 7 – Event Payload Round-Trip
# ---------------------------------------------------------------------------
class TestEventPayloadRoundTrip:
    """**Validates: Requirement 15.5**"""

    @given(event_payload=_event_payloads, event_type=_event_types, event_uuid=uuids())
    @settings(max_examples=150)
    @pytest.mark.asyncio
    async def test_pydantic_model_roundtrip(
        self, event_payload: dict, event_type: str, event_uuid
    ):
        """
        Serializing a JobEvent to JSON and deserializing it back SHALL
        produce an equivalent JobEvent object.

        **Validates: Requirement 15.5**
        """
        event = JobEvent(
            event_id=str(event_uuid),
            job_id="JOB_1",
            event_type=event_type,
            tenant_id=TENANT_ID,
            actor_id="operator_1",
            event_timestamp="2026-03-12T10:00:00Z",
            event_payload=event_payload,
        )

        # Serialize → deserialize via Pydantic JSON
        json_str = event.model_dump_json()
        restored = JobEvent.model_validate_json(json_str)

        assert restored == event, (
            f"Round-trip mismatch.\n"
            f"Original payload: {event.event_payload}\n"
            f"Restored payload: {restored.event_payload}"
        )

    @given(event_payload=_event_payloads, event_type=_event_types, event_uuid=uuids())
    @settings(max_examples=150)
    @pytest.mark.asyncio
    async def test_dict_json_roundtrip(
        self, event_payload: dict, event_type: str, event_uuid
    ):
        """
        Serializing a JobEvent to a dict, then to a JSON string, then back
        to a dict, then constructing a JobEvent SHALL produce an equivalent
        object. This mirrors the Elasticsearch index → retrieve path.

        **Validates: Requirement 15.5**
        """
        event = JobEvent(
            event_id=str(event_uuid),
            job_id="JOB_42",
            event_type=event_type,
            tenant_id=TENANT_ID,
            actor_id=None,
            event_timestamp="2026-03-12T14:30:00Z",
            event_payload=event_payload,
        )

        # Simulate ES storage: model → dict → JSON string → dict → model
        event_dict = event.model_dump()
        json_str = json.dumps(event_dict)
        raw_dict = json.loads(json_str)
        restored = JobEvent(**raw_dict)

        assert restored == event, (
            f"Dict-JSON round-trip mismatch.\n"
            f"Original payload: {event.event_payload}\n"
            f"Restored payload: {restored.event_payload}"
        )

    @given(event_payload=_event_payloads)
    @settings(max_examples=150)
    @pytest.mark.asyncio
    async def test_payload_preserved_through_json_stdlib(self, event_payload: dict):
        """
        The event_payload dict alone SHALL survive a json.dumps → json.loads
        round-trip with full equivalence.

        **Validates: Requirement 15.5**
        """
        serialized = json.dumps(event_payload)
        deserialized = json.loads(serialized)

        assert deserialized == event_payload, (
            f"Payload round-trip mismatch.\n"
            f"Original: {event_payload}\n"
            f"Restored: {deserialized}"
        )
