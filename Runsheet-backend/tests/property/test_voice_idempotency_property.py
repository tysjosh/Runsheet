"""
Property-based test for Dinee voice submission idempotency.

# Feature: dinee-voice-integration, Property 8: Idempotency — dedup, tenant
scoping, conflict, and order-id recall

**Validates: Requirements 5.1, 5.2, 5.4, 9.2**

Property 8 (Idempotency): For voice submissions driven through the
:class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge` against the
tenant-scoped :class:`~fuel.voice.voice_submission_ledger.VoiceSubmissionLedger`:

* **Dedup + order-id recall (Req 5.1, 9.2)** — a second submission for the
  *same* tenant with the *same* ``X-Idempotency-Key`` and an *identical* body
  returns the **original** order id, its disposition, and does **not**
  re-invoke the pipeline (no second order / review-workflow entry).
* **Conflict (Req 5.4)** — the *same* key reused for the *same* tenant with a
  *different* body is rejected with **HTTP 409** and never re-invokes the
  pipeline.
* **Tenant scoping (Req 5.2)** — the *same* key presented for two *different*
  tenants is treated independently: both submissions reach the pipeline and
  mint distinct orders.

The test drives the real ``DineeVoiceBridge`` against an in-memory,
dict-backed ledger fake (faithfully implementing the tenant-scoped
lookup/record contract of the real Redis ledger) and a recording pipeline
that mints a fresh order id per call. This lets the "single order on replay",
"409 on conflict", and "independent per tenant" behaviours be observed
directly without a live Elasticsearch/Redis.
"""
from __future__ import annotations

import asyncio
import hashlib
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
from fuel.voice.voice_submission_ledger import LedgerEntry


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
# Fakes
# ---------------------------------------------------------------------------


class DictLedger:
    """In-memory, dict-backed :class:`VoiceSubmissionLedger` stand-in.

    Faithfully implements the tenant-scoped contract of the real Redis ledger:
    entries are keyed by ``(tenant_id, key)`` so the same idempotency key under
    two tenants is independent (Req 5.2). ``lookup`` returns the recorded
    :class:`LedgerEntry` (or ``None``); ``record`` persists a first-seen
    outcome. Every call is recorded so the test can assert what the bridge did.
    """

    def __init__(self) -> None:
        self._store: Dict[tuple, LedgerEntry] = {}
        self.lookups: List[tuple] = []
        self.records: List[Dict[str, Any]] = []

    async def lookup(self, tenant_id: str, key: str) -> Optional[LedgerEntry]:
        self.lookups.append((tenant_id, key))
        return self._store.get((tenant_id, key))

    async def record(
        self,
        tenant_id: str,
        key: str,
        body_sha256: str,
        order_id: Optional[str],
        disposition: str,
    ) -> None:
        self.records.append(
            {
                "tenant_id": tenant_id,
                "key": key,
                "body_sha256": body_sha256,
                "order_id": order_id,
                "disposition": disposition,
            }
        )
        self._store[(tenant_id, key)] = LedgerEntry(
            body_sha256=body_sha256,
            order_id=order_id,
            disposition=disposition,
        )


class RecordingPipeline:
    """Fake OrderIntakePipeline minting a fresh order id on each call.

    Always returns a ``processed`` result; the monotonically increasing order
    id lets the test prove that a replay recalls the *original* id (the
    pipeline is not re-invoked) while independent tenants get distinct orders.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._counter = 0

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
        self._counter += 1
        order_id = f"ord_idem_{self._counter:04d}"
        self.calls.append(
            {
                "channel_id": channel_id,
                "body": body,
                "signature": signature,
                "idempotency_key_override": idempotency_key_override,
                "schema_version_override": schema_version_override,
                "order_id": order_id,
            }
        )
        return IntakeResponse(
            event_id=idempotency_key_override or "evt",
            status="processed",
            order_id=order_id,
        )


class TenantChannelRepo:
    """Fake IntakeChannelRepository resolving an enabled voice channel per tenant."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def get_voice_channel(self, tenant_id: str) -> IntakeChannel:
        self.calls.append(tenant_id)
        return _make_channel(tenant_id)


def _make_channel(tenant_id: str) -> IntakeChannel:
    channel_id = f"c{hashlib.sha256(tenant_id.encode()).hexdigest()[:20]}0"
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


def _build_bridge(repo: TenantChannelRepo, pipeline: RecordingPipeline, ledger: DictLedger) -> DineeVoiceBridge:
    return DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=repo,
        ledger=ledger,
        replay_window_seconds=300,
        clock=lambda: FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# Strategies — structurally valid VoiceIntakePayloads (no tenant_id field)
# ---------------------------------------------------------------------------

_non_empty = st.text(min_size=1, max_size=30)
# Lowercase-alphanumeric tokens usable as tenant ids / idempotency keys.
_token = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20
)


@st.composite
def _valid_slots(draw) -> Dict[str, Any]:
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
        # A real catalog code: the bridge validates product_code against the
        # fuel product catalog before invoking the pipeline.
        "product_code": "DIESEL_2",
    }


@st.composite
def _valid_payload(draw) -> Dict[str, Any]:
    """A structurally valid VoiceIntakePayload dict (never carries tenant_id)."""
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


def _submit(bridge: DineeVoiceBridge, *, raw_body: bytes, tenant_id: str, key: str):
    async def _run():
        return await bridge.submit(
            raw_body=raw_body,
            tenant_id=tenant_id,
            idempotency_key=key,
            timestamp=IN_WINDOW_TIMESTAMP,
            schema_version=SCHEMA_VERSION,
            signature=VALID_SIGNATURE,
        )

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------


class TestVoiceIdempotency:
    """# Feature: dinee-voice-integration, Property 8: Idempotency — dedup,
    tenant scoping, conflict, and order-id recall

    **Validates: Requirements 5.1, 5.2, 5.4, 9.2**
    """

    @given(payload=_valid_payload(), tenant_id=_token, key=_token)
    @settings(max_examples=100)
    def test_same_tenant_same_key_same_body_dedups_and_recalls_order_id(
        self, payload: Dict[str, Any], tenant_id: str, key: str
    ) -> None:
        """Req 5.1 / 9.2 — replay returns the original order id, pipeline once."""
        ledger = DictLedger()
        pipeline = RecordingPipeline()
        bridge = _build_bridge(TenantChannelRepo(), pipeline, ledger)

        raw_body = json.dumps(payload).encode("utf-8")

        first = _submit(bridge, raw_body=raw_body, tenant_id=tenant_id, key=key)
        second = _submit(bridge, raw_body=raw_body, tenant_id=tenant_id, key=key)

        # (Req 5.1) The pipeline was invoked exactly once — no second order or
        # review-workflow entry was created for the duplicate.
        assert len(pipeline.calls) == 1
        # Only the first submission recorded a ledger outcome.
        assert len(ledger.records) == 1

        # (Req 9.2) The replay returns the *original* minted order id verbatim.
        assert first.orderId == "ord_idem_0001"
        assert second.orderId == first.orderId
        assert second.disposition == first.disposition

    @given(payload=_valid_payload(), tenant_id=_token, key=_token)
    @settings(max_examples=100)
    def test_same_tenant_same_key_different_body_conflicts(
        self, payload: Dict[str, Any], tenant_id: str, key: str
    ) -> None:
        """Req 5.4 — same key + different body → 409, pipeline not re-invoked."""
        ledger = DictLedger()
        pipeline = RecordingPipeline()
        bridge = _build_bridge(TenantChannelRepo(), pipeline, ledger)

        first_body = json.dumps(payload).encode("utf-8")
        # A materially different body under the same key: mutate a required
        # field so the sha256 differs while the payload stays structurally
        # valid. (Prefixing guarantees a distinct value and distinct hash.)
        conflicting = dict(payload)
        conflicting["callId"] = "CONFLICT-" + str(payload["callId"])
        second_body = json.dumps(conflicting).encode("utf-8")
        assert hashlib.sha256(first_body).hexdigest() != hashlib.sha256(
            second_body
        ).hexdigest()

        first = _submit(bridge, raw_body=first_body, tenant_id=tenant_id, key=key)
        assert first.orderId == "ord_idem_0001"

        with pytest.raises(AppException) as exc_info:
            _submit(bridge, raw_body=second_body, tenant_id=tenant_id, key=key)

        exc = exc_info.value
        assert exc.status_code == 409
        assert exc.error_code == ErrorCode.IDEMPOTENCY_CONFLICT

        # The conflicting submission never reached the pipeline (no order
        # persisted) and recorded no ledger outcome.
        assert len(pipeline.calls) == 1
        assert len(ledger.records) == 1

    @given(payload=_valid_payload(), tenant_a=_token, tenant_b=_token, key=_token)
    @settings(max_examples=100)
    def test_same_key_different_tenants_are_independent(
        self,
        payload: Dict[str, Any],
        tenant_a: str,
        tenant_b: str,
        key: str,
    ) -> None:
        """Req 5.2 — the same key under two tenants is treated independently."""
        # Constrain to two genuinely distinct tenants.
        if tenant_a == tenant_b:
            tenant_b = tenant_b + "-b"

        ledger = DictLedger()
        pipeline = RecordingPipeline()
        bridge = _build_bridge(TenantChannelRepo(), pipeline, ledger)

        raw_body = json.dumps(payload).encode("utf-8")

        resp_a = _submit(bridge, raw_body=raw_body, tenant_id=tenant_a, key=key)
        resp_b = _submit(bridge, raw_body=raw_body, tenant_id=tenant_b, key=key)

        # Both submissions were processed independently through the pipeline.
        assert len(pipeline.calls) == 2
        assert len(ledger.records) == 2

        # Each tenant minted a distinct order — the shared key did not dedup
        # across tenants (Req 5.2).
        assert resp_a.orderId == "ord_idem_0001"
        assert resp_b.orderId == "ord_idem_0002"
        assert resp_a.orderId != resp_b.orderId

        # The ledger recorded one tenant-scoped entry per tenant.
        recorded_tenants = {r["tenant_id"] for r in ledger.records}
        assert recorded_tenants == {tenant_a, tenant_b}
