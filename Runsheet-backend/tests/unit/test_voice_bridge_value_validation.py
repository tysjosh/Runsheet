"""Unit tests for the DineeVoiceBridge value-validation hardening.

The bridge validates that required *fields* are present, but the canonical
``FuelOrder`` enforces additional *value* invariants inside the pipeline
(e.g. a ``one_off`` order must carry a delivery window). Those raise
``pydantic.ValidationError`` from ``FuelOrder.model_validate`` after the
adapter runs. Previously that surfaced as an HTTP 500; the bridge now maps it
to a uniform 422 ``VOICE_PAYLOAD_INVALID`` naming the offending field(s)/rule(s)
and records no order.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.order_models import FuelOrder
from fuel.services.order_intake_pipeline import IntakeResponse
from fuel.voice.dinee_voice_bridge import DineeVoiceBridge, _extract_invalid_fields

TENANT_ID = "tenant-value-validation"
CHANNEL_ID = "voice-value-chan-01"
SCHEMA_VERSION = "1.0"
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
VALID_SIGNATURE = "sha256=" + "a" * 64
VALID_TIMESTAMP = FIXED_NOW.isoformat()
VALID_IDEM_KEY = "idem-value-key"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeChannel:
    def __init__(self) -> None:
        self.tenant_id = TENANT_ID
        self.channel_id = CHANNEL_ID
        self.supported_schema_versions = [SCHEMA_VERSION]


class FakeChannelRepo:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel

    async def get_voice_channel(self, tenant_id: str) -> Optional[FakeChannel]:
        return self._channel


class FakeLedger:
    def __init__(self) -> None:
        self.records: List[Any] = []

    async def lookup(self, tenant_id: str, key: str):
        return None

    async def record(self, *args: Any) -> None:
        self.records.append(args)


class RaisingPipeline:
    """Pipeline whose ingest_webhook raises a real FuelOrder ValidationError."""

    def __init__(self, exc: ValidationError) -> None:
        self._exc = exc
        self.calls = 0

    async def ingest_webhook(self, **kwargs: Any) -> IntakeResponse:
        self.calls += 1
        raise self._exc


class ProcessingPipeline:
    """Pipeline that accepts the order (control case)."""

    def __init__(self) -> None:
        self.calls = 0

    async def ingest_webhook(self, **kwargs: Any) -> IntakeResponse:
        self.calls += 1
        return IntakeResponse(
            event_id=kwargs.get("idempotency_key_override") or "",
            status="processed",
            order_id="ord_ok_1",
        )


def _bridge(pipeline: Any, ledger: FakeLedger) -> DineeVoiceBridge:
    return DineeVoiceBridge(
        pipeline=pipeline,
        intake_channel_repo=FakeChannelRepo(FakeChannel()),
        ledger=ledger,
        replay_window_seconds=300,
        clock=lambda: FIXED_NOW,
    )


def _body() -> bytes:
    payload: Dict[str, Any] = {
        "callId": "call-1",
        "transcriptId": "tr-1",
        "transcript": [{"speaker": "customer", "text": "fuel please"}],
        "callerPhone": "+15555550100",
        "extractedSlots": {
            "customer_id": "cust-1",
            "customer_name": "Acme",
            "ship_to_address": "1 Depot Rd",
            "ship_to_lat": 40.0,
            "ship_to_lon": -75.0,
            "product_code": "propane",
        },
        "reviewRequired": True,
    }
    return json.dumps(payload).encode("utf-8")


async def _submit(bridge: DineeVoiceBridge) -> Any:
    return await bridge.submit(
        raw_body=_body(),
        tenant_id=TENANT_ID,
        idempotency_key=VALID_IDEM_KEY,
        timestamp=VALID_TIMESTAMP,
        schema_version=SCHEMA_VERSION,
        signature=VALID_SIGNATURE,
        request_id="req-1",
    )


def _window_validation_error() -> ValidationError:
    """A real FuelOrder ValidationError: one_off order without a window."""
    doc = {
        "order_id": "o1",
        "tenant_id": TENANT_ID,
        "customer_id": "c1",
        "customer_name": "Acme",
        "ship_to_address": "1 Depot Rd",
        "ship_to_lat": 40.0,
        "ship_to_lon": -75.0,
        "product_code": "propane",
        "gallons_requested": 500.0,
        "call_type": "one_off",  # requires a delivery window -> raises
        "intake_channel": "voice",
        "intake_channel_id": CHANNEL_ID,
        "status": "placed",
        "source_schema_version": "1.0",
        "created_at": FIXED_NOW.isoformat(),
        "updated_at": FIXED_NOW.isoformat(),
        "last_event_timestamp": FIXED_NOW.isoformat(),
        "trace_id": "t",
    }
    try:
        FuelOrder.model_validate(doc)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected FuelOrder validation to fail")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pipeline_validation_error_maps_to_422(anyio_backend=None):
    import asyncio

    exc = _window_validation_error()
    ledger = FakeLedger()
    bridge = _bridge(RaisingPipeline(exc), ledger)

    with pytest.raises(AppException) as ei:
        asyncio.run(_submit(bridge))

    app_exc = ei.value
    assert app_exc.error_code == ErrorCode.VOICE_PAYLOAD_INVALID
    # The offending rule is named (invalid_delivery_window) in missing_fields.
    assert "invalid_delivery_window" in app_exc.details["missing_fields"]
    # Nothing was recorded in the ledger (no order persisted outcome).
    assert ledger.records == []


def test_control_case_still_processes():
    import asyncio

    ledger = FakeLedger()
    pipeline = ProcessingPipeline()
    bridge = _bridge(pipeline, ledger)

    result = asyncio.run(_submit(bridge))
    assert pipeline.calls == 1
    assert result.orderId == "ord_ok_1"


def test_unknown_product_code_named_422_without_pipeline():
    import asyncio

    ledger = FakeLedger()
    pipeline = ProcessingPipeline()
    bridge = _bridge(pipeline, ledger)

    body = json.dumps(
        {
            "callId": "call-1",
            "transcriptId": "tr-1",
            "transcript": [{"speaker": "customer", "text": "fuel"}],
            "callerPhone": "+15555550100",
            "extractedSlots": {
                "customer_id": "cust-1",
                "customer_name": "Acme",
                "ship_to_address": "1 Depot Rd",
                "ship_to_lat": 40.0,
                "ship_to_lon": -75.0,
                "product_code": "ZZZ-not-a-product",
            },
            "reviewRequired": True,
        }
    ).encode("utf-8")

    async def _run():
        return await bridge.submit(
            raw_body=body,
            tenant_id=TENANT_ID,
            idempotency_key="idem-bad-product",
            timestamp=VALID_TIMESTAMP,
            schema_version=SCHEMA_VERSION,
            signature=VALID_SIGNATURE,
            request_id="req-bad-product",
        )

    with pytest.raises(AppException) as ei:
        asyncio.run(_run())

    app_exc = ei.value
    assert app_exc.error_code == ErrorCode.VOICE_PAYLOAD_INVALID
    assert app_exc.details["missing_fields"] == ["extractedSlots.product_code"]
    # Rejected at the bridge boundary — the pipeline is never invoked and no
    # order outcome is recorded (nothing hits the poison queue).
    assert pipeline.calls == 0
    assert ledger.records == []


def _signed_body(**overrides: Any) -> bytes:
    """A body that also carries the signed tenant/idempotency/timestamp/schema."""
    payload: Dict[str, Any] = {
        "callId": "call-1",
        "transcriptId": "tr-1",
        "transcript": [{"speaker": "customer", "text": "fuel"}],
        "callerPhone": "+15555550100",
        "extractedSlots": {
            "customer_id": "cust-1",
            "customer_name": "Acme",
            "ship_to_address": "1 Depot Rd",
            "product_code": "propane",
            "quantity": {"gallons": 500},
        },
        "reviewRequired": True,
        # Signed copies of the header values.
        "tenantId": TENANT_ID,
        "idempotencyKey": VALID_IDEM_KEY,
        "timestamp": VALID_TIMESTAMP,
        "schemaVersion": SCHEMA_VERSION,
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


async def _submit_body(bridge: DineeVoiceBridge, body: bytes, **hdr: Any) -> Any:
    return await bridge.submit(
        raw_body=body,
        tenant_id=hdr.get("tenant_id", TENANT_ID),
        idempotency_key=hdr.get("idempotency_key", VALID_IDEM_KEY),
        timestamp=hdr.get("timestamp", VALID_TIMESTAMP),
        schema_version=hdr.get("schema_version", SCHEMA_VERSION),
        signature=VALID_SIGNATURE,
        request_id="req-signed",
    )


def test_signed_body_matching_headers_processes():
    import asyncio

    pipeline = ProcessingPipeline()
    bridge = _bridge(pipeline, FakeLedger())
    result = asyncio.run(_submit_body(bridge, _signed_body()))
    assert pipeline.calls == 1
    assert result.orderId == "ord_ok_1"


def test_signed_body_idempotency_key_mismatch_rejected():
    import asyncio

    pipeline = ProcessingPipeline()
    bridge = _bridge(pipeline, FakeLedger())
    # Header idempotency key differs from the signed body's key -> replay attempt.
    body = _signed_body(idempotencyKey="ORIGINAL-KEY")
    with pytest.raises(AppException) as ei:
        asyncio.run(_submit_body(bridge, body, idempotency_key="FRESH-KEY"))
    assert ei.value.error_code == ErrorCode.VOICE_UNAUTHORIZED
    assert pipeline.calls == 0


def test_signed_body_timestamp_mismatch_rejected():
    import asyncio

    pipeline = ProcessingPipeline()
    bridge = _bridge(pipeline, FakeLedger())
    # Header timestamp differs from the signed body's timestamp -> replay attempt.
    body = _signed_body(timestamp="2020-01-01T00:00:00+00:00")
    with pytest.raises(AppException) as ei:
        asyncio.run(_submit_body(bridge, body))  # header uses VALID_TIMESTAMP
    assert ei.value.error_code == ErrorCode.VOICE_UNAUTHORIZED
    assert pipeline.calls == 0


def test_signed_body_timestamp_z_suffix_equivalent_ok():
    import asyncio

    # 'Z' vs '+00:00' for the same instant must NOT be rejected.
    pipeline = ProcessingPipeline()
    bridge = _bridge(pipeline, FakeLedger())
    z_ts = VALID_TIMESTAMP.replace("+00:00", "Z")
    body = _signed_body(timestamp=z_ts)
    result = asyncio.run(_submit_body(bridge, body))  # header keeps +00:00 form
    assert pipeline.calls == 1
    assert result.orderId == "ord_ok_1"


def test_extract_invalid_fields_field_level():
    # gallons must be > 0 and lat in range — field-level errors carry loc.
    doc = {
        "order_id": "o1",
        "tenant_id": TENANT_ID,
        "customer_id": "c1",
        "customer_name": "Acme",
        "ship_to_address": "1 Depot Rd",
        "ship_to_lat": 999.0,  # out of range -> field-level error at ship_to_lat
        "ship_to_lon": -75.0,
        "product_code": "propane",
        "gallons_requested": -5.0,  # gt=0 -> field-level error at gallons_requested
        "call_type": "will_call",
        "intake_channel": "voice",
        "intake_channel_id": CHANNEL_ID,
        "status": "placed",
        "created_at": FIXED_NOW.isoformat(),
        "updated_at": FIXED_NOW.isoformat(),
        "last_event_timestamp": FIXED_NOW.isoformat(),
        "trace_id": "t",
    }
    with pytest.raises(ValidationError) as ei:
        FuelOrder.model_validate(doc)
    fields = _extract_invalid_fields(ei.value)
    assert "ship_to_lat" in fields
    assert "gallons_requested" in fields
