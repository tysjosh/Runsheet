"""
Property-based test for the Dinee voice bridge replay-window enforcement.

# Feature: dinee-voice-integration, Property 4: Replay-window enforcement

**Validates: Requirements 4.1, 4.2, 4.3**

Property 4 (Replay-window enforcement): for the ``DineeVoiceBridge`` configured
with an injectable ``clock`` and ``replay_window_seconds``:

* (Req 4.1) A submission whose ``X-Timestamp`` is within ``replay_window_seconds``
  of server time passes the replay check and the bridge proceeds to invoke the
  ``OrderIntakePipeline``.
* (Req 4.2) A submission whose ``X-Timestamp`` differs from server time by more
  than ``replay_window_seconds`` is rejected with HTTP 401 and the pipeline is
  **not** invoked.
* (Req 4.3) A submission that omits ``X-Timestamp`` or presents an unparseable
  value is rejected with HTTP 401 (and the pipeline is not invoked).

A recording fake pipeline records every ``ingest_webhook`` call so the
"pipeline not invoked on a stale/missing timestamp" guarantee (Req 4.2/4.3) is
directly assertable, and the "pipeline invoked on a fresh timestamp" guarantee
(Req 4.1) is confirmed by the same recorder. All other bridge stages
(signature format, channel resolution, idempotency, schema version, required
fields) are satisfied with valid inputs so the replay-window stage is the only
gate under test.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

TENANT_ID = "tenant-voice-replay"
CHANNEL_ID = "voice-replay-chan-01"
SCHEMA_VERSION = "1.0"
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
VALID_SIGNATURE = "sha256=" + "a" * 64


def _valid_payload_bytes() -> bytes:
    """A structurally valid VoiceIntakePayload so the bridge reaches the
    pipeline once the replay-window (and every other) stage passes."""
    payload: Dict[str, Any] = {
        "callId": "call-abc",
        "transcriptId": "transcript-abc",
        "transcript": [],
        "extractedSlots": {
            "customer_name": "Acme Fuel Co",
            "ship_to_address": "123 Depot Road",
            "ship_to_lat": 40.0,
            "ship_to_lon": -74.0,
            "product_code": "DIESEL_2",
        },
        "reviewRequired": True,
    }
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# Recording / stub collaborators
# ---------------------------------------------------------------------------


@dataclass
class _FakeChannel:
    tenant_id: str = TENANT_ID
    channel_id: str = CHANNEL_ID
    supported_schema_versions: List[str] = field(
        default_factory=lambda: [SCHEMA_VERSION]
    )


class RecordingPipeline:
    """Fake pipeline recording every ``ingest_webhook`` invocation.

    Lets the property assert the pipeline is invoked exactly once for a fresh
    timestamp (Req 4.1) and never for a stale / missing / unparseable one
    (Req 4.2/4.3).
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def ingest_webhook(self, **kwargs) -> IntakeResponse:
        self.calls.append(kwargs)
        return IntakeResponse(
            event_id=kwargs.get("idempotency_key_override", ""),
            status="processed",
            order_id="ord_replaywindow_0001",
        )


class StubChannelRepo:
    """Resolves the tenant's registered enabled voice channel."""

    def __init__(self, channel: Optional[_FakeChannel]) -> None:
        self._channel = channel

    async def get_voice_channel(self, tenant_id: str) -> Optional[_FakeChannel]:
        return self._channel


class StubLedger:
    """Ledger stub: no prior entry (new submission), records ignored."""

    def __init__(self) -> None:
        self.records: List[tuple] = []

    async def lookup(self, tenant_id: str, key: str) -> Optional[LedgerEntry]:
        return None

    async def record(self, tenant_id, key, body_sha256, order_id, disposition) -> None:
        self.records.append((tenant_id, key, body_sha256, order_id, disposition))


def _build_bridge(
    pipeline: RecordingPipeline, replay_window_seconds: int
) -> DineeVoiceBridge:
    return DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=StubChannelRepo(_FakeChannel()),
        ledger=StubLedger(),
        replay_window_seconds=replay_window_seconds,
        clock=lambda: FIXED_NOW,
    )


def _submit(bridge: DineeVoiceBridge, *, timestamp: Optional[str]):
    return asyncio.run(
        bridge.submit(
            raw_body=_valid_payload_bytes(),
            tenant_id=TENANT_ID,
            idempotency_key="idem-replay-001",
            timestamp=timestamp,
            schema_version=SCHEMA_VERSION,
            signature=VALID_SIGNATURE,
        )
    )


def _format_timestamp(offset_seconds: int, *, as_iso: bool) -> str:
    """Render a timestamp ``offset_seconds`` from ``FIXED_NOW`` as epoch or ISO."""
    ts = FIXED_NOW + timedelta(seconds=offset_seconds)
    if as_iso:
        return ts.isoformat()
    return str(int(ts.timestamp()))


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


class TestReplayWindowEnforcement:
    """# Feature: dinee-voice-integration, Property 4: Replay-window enforcement

    **Validates: Requirements 4.1, 4.2, 4.3**
    """

    # -- Req 4.1: within the window passes the replay check ----------------
    @given(
        window=st.integers(min_value=1, max_value=3600),
        skew=st.integers(min_value=0, max_value=3600),
        ahead=st.booleans(),
        as_iso=st.booleans(),
    )
    @settings(max_examples=100)
    def test_within_window_passes_and_invokes_pipeline(
        self, window: int, skew: int, ahead: bool, as_iso: bool
    ):
        # Constrain the skew to the closed window so the check must pass.
        offset = min(skew, window)
        if not ahead:
            offset = -offset

        pipeline = RecordingPipeline()
        bridge = _build_bridge(pipeline, window)

        response = _submit(
            bridge, timestamp=_format_timestamp(offset, as_iso=as_iso)
        )

        # The replay check passed → the bridge invoked the pipeline exactly once.
        assert len(pipeline.calls) == 1
        assert response.orderId == "ord_replaywindow_0001"

    # -- Req 4.2: outside the window → 401, pipeline NOT invoked ------------
    @given(
        window=st.integers(min_value=1, max_value=3600),
        excess=st.integers(min_value=1, max_value=100000),
        ahead=st.booleans(),
        as_iso=st.booleans(),
    )
    @settings(max_examples=100)
    def test_outside_window_rejected_401_pipeline_not_invoked(
        self, window: int, excess: int, ahead: bool, as_iso: bool
    ):
        offset = window + excess
        if not ahead:
            offset = -offset

        pipeline = RecordingPipeline()
        bridge = _build_bridge(pipeline, window)

        with pytest.raises(AppException) as exc_info:
            _submit(bridge, timestamp=_format_timestamp(offset, as_iso=as_iso))

        exc = exc_info.value
        assert exc.status_code == 401
        assert exc.error_code == ErrorCode.VOICE_REPLAY_WINDOW_EXCEEDED
        # The pipeline must never be reached for a stale timestamp (Req 4.2).
        assert pipeline.calls == []

    # -- Req 4.3: missing / unparseable timestamp → 401 --------------------
    @given(
        window=st.integers(min_value=1, max_value=3600),
        timestamp=st.sampled_from(
            [
                None,
                "",
                "   ",
                "not-a-timestamp",
                "abcdef",
                "2026-13-45T99:99:99Z",
                "tomorrow",
                "NaN",
            ]
        ),
    )
    @settings(max_examples=100)
    def test_missing_or_unparseable_timestamp_rejected_401(
        self, window: int, timestamp: Optional[str]
    ):
        pipeline = RecordingPipeline()
        bridge = _build_bridge(pipeline, window)

        with pytest.raises(AppException) as exc_info:
            _submit(bridge, timestamp=timestamp)

        exc = exc_info.value
        assert exc.status_code == 401
        assert exc.error_code == ErrorCode.VOICE_REPLAY_WINDOW_EXCEEDED
        # A missing / unparseable timestamp must not reach the pipeline (Req 4.3).
        assert pipeline.calls == []
