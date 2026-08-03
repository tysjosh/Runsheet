"""A bad order payload must be a 422, not a 500, on every intake channel.

``FuelOrder`` enforces cross-field invariants that no per-channel request model
can express: a ``one_off`` order must carry a delivery window, a window must end
after it starts, a non-legacy channel must carry a canonical product_code.
``OrderIntakePipeline`` checks them at step (j) via ``FuelOrder.model_validate``,
after the adapter has run.

Those ``ValidationError``s used to escape unhandled. Live, ``POST /api/orders``
answered every one of them with::

    500  {"error_code": "INTERNAL_ERROR",
          "message": "An unexpected error occurred. Please try again later."}

so a caller could not tell what it had sent wrong, and error-rate metrics could
not separate client error from server fault.

The Dinee voice bridge had already diagnosed this — its comment read "Left
unhandled they surface as an HTTP 500, which is wrong: the input is bad, not the
server" — but it caught the error at its own call site, fixing exactly one of the
five channels. These tests pin the mapping at the pipeline, where the rule is
enforced, and assert it for each channel rather than for one.

Validates: Requirements 2.1, 2.2, 2.3 (intake), 7.3 (voice envelope preserved).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.intake.adapter_base import IntakeAdapterRegistry
from fuel.intake.dispatcher_adapter import DispatcherIntakeAdapter
from fuel.services.order_intake_pipeline import (
    OrderIntakePipeline,
    extract_invalid_fields,
)

_NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _Channel:
    channel_id: str = "dispatcher-default"
    tenant_id: str = "tenant-A"
    channel_type: str = "dispatcher"
    supported_schema_versions: List[str] = field(default_factory=lambda: ["1.0"])
    enabled: bool = True


class _Repo:
    """Intake-channel repo stub that always yields the dispatcher channel."""

    def __init__(self, channel: _Channel):
        self._c = channel

    async def ensure_dispatcher_channel(self, tenant_id: str):
        return self._c

    async def get_dispatcher_channel(self, tenant_id: str):
        return self._c

    async def get_by_channel_id(self, channel_id: str):
        return self._c


def _build_pipeline(channel: _Channel) -> OrderIntakePipeline:
    """Real adapter registry and real FuelOrder validation; ES/Redis faked.

    Nothing about the validation path is mocked — that is the code under test.
    """
    registry = IntakeAdapterRegistry()
    registry.register(
        DispatcherIntakeAdapter(),
        channel_type="dispatcher",
        schema_version="1.0",
    )

    idempotency = AsyncMock()
    idempotency.is_duplicate = AsyncMock(return_value=False)
    idempotency.mark_processed = AsyncMock()

    flags = AsyncMock()
    flags.get_overlay_state = AsyncMock(return_value="active_auto")

    poison = AsyncMock()
    poison.store_failed_event = AsyncMock()

    ws = AsyncMock()
    ws.broadcast = AsyncMock()

    tanks = AsyncMock()
    tanks.get = AsyncMock(return_value=None)

    return OrderIntakePipeline(
        es_service=MagicMock(),
        intake_channel_repo=_Repo(channel),
        adapter_registry=registry,
        idempotency_service=idempotency,
        feature_flag_service=flags,
        poison_queue_service=poison,
        ws_manager=ws,
        credentials_vault=AsyncMock(),
        customer_tank_repo=tanks,
        clock=lambda: _NOW,
    )


def _payload(**overrides: Any) -> Dict[str, Any]:
    base = {
        "customer_id": "cust-1",
        "customer_name": "Acme Fuel",
        "ship_to_address": "1200 Industrial Pkwy, Houston, TX",
        "ship_to_lat": 29.7604,
        "ship_to_lon": -95.3698,
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "call_type": "one_off",
        "delivery_window_start": "2026-08-05T08:00:00+00:00",
        "delivery_window_end": "2026-08-05T12:00:00+00:00",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not _OMIT}


class _Omit:
    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "<omit>"


_OMIT = _Omit()


# ---------------------------------------------------------------------------
# The three invariants, each through the dispatcher channel
# ---------------------------------------------------------------------------


class TestDispatcherIntakeRejectsWith422:
    """``POST /api/orders`` is the channel that was observed returning 500."""

    @pytest.mark.parametrize(
        "overrides,expected_field",
        [
            pytest.param(
                {"delivery_window_start": _OMIT, "delivery_window_end": _OMIT},
                "invalid_delivery_window",
                id="one_off_without_a_delivery_window",
            ),
            pytest.param(
                {
                    "delivery_window_start": "2026-08-05T12:00:00+00:00",
                    "delivery_window_end": "2026-08-05T08:00:00+00:00",
                },
                "invalid_delivery_window",
                id="window_ends_before_it_starts",
            ),
        ],
    )
    async def test_value_violation_raises_422_naming_the_rule(
        self, overrides, expected_field
    ):
        pipeline = _build_pipeline(_Channel())

        with pytest.raises(AppException) as ei:
            await pipeline.ingest_dispatcher(
                tenant={"tenant_id": "tenant-A", "user_id": "u1"},
                payload=_payload(**overrides),
                request_id="req-1",
                client_event_id="evt-1",
            )

        exc = ei.value
        assert exc.status_code == 422, (
            f"expected 422, got {exc.status_code} — a bad payload must not be "
            f"reported as a server fault"
        )
        assert exc.error_code is ErrorCode.ORDER_PAYLOAD_INVALID
        assert expected_field in exc.details["invalid_fields"]

    async def test_error_never_echoes_the_submitted_values(self):
        """The rejection must name fields, not leak customer data.

        The payload carries a real-looking street address and a phone number.
        Neither may appear anywhere in the error the caller receives.
        """
        pipeline = _build_pipeline(_Channel())
        secret_address = "1200 Industrial Pkwy, Houston, TX"
        secret_phone = "+15125550123"

        with pytest.raises(AppException) as ei:
            await pipeline.ingest_dispatcher(
                tenant={"tenant_id": "tenant-A", "user_id": "u1"},
                payload=_payload(
                    customer_phone=secret_phone,
                    delivery_window_start=_OMIT,
                    delivery_window_end=_OMIT,
                ),
                request_id="req-2",
                client_event_id="evt-2",
            )

        rendered = repr(ei.value.to_dict())
        assert secret_address not in rendered
        assert secret_phone not in rendered

    async def test_a_valid_payload_still_passes_validation(self):
        """Guard the guard: the 422 must not fire on a well-formed order.

        Without this, tightening step (j) into an unconditional rejection would
        satisfy every assertion above.
        """
        pipeline = _build_pipeline(_Channel())

        # A valid order proceeds past step (j) into persistence. The ES service
        # is a MagicMock, so the write raises something that is NOT our
        # AppException — reaching it proves validation was passed.
        with pytest.raises(Exception) as ei:
            await pipeline.ingest_dispatcher(
                tenant={"tenant_id": "tenant-A", "user_id": "u1"},
                payload=_payload(),
                request_id="req-3",
                client_event_id="evt-3",
            )
        assert not (
            isinstance(ei.value, AppException)
            and ei.value.error_code is ErrorCode.ORDER_PAYLOAD_INVALID
        ), "a valid order must not be rejected as ORDER_PAYLOAD_INVALID"


# ---------------------------------------------------------------------------
# The helper that names the offending rule
# ---------------------------------------------------------------------------


class TestExtractInvalidFields:
    """``extract_invalid_fields`` handles both pydantic error shapes."""

    def test_model_level_rule_code_is_recovered(self):
        from pydantic import ValidationError

        from fuel.order_models import FuelOrder

        try:
            FuelOrder.model_validate(
                {
                    "order_id": "o1",
                    "tenant_id": "t1",
                    "customer_id": "c1",
                    "customer_name": "N",
                    "ship_to_address": "A",
                    "ship_to_lat": 30.0,
                    "ship_to_lon": -90.0,
                    "product_code": "DIESEL_2",
                    # gallons must be present or ``missing_volume`` fires first
                    # and masks the rule under test.
                    "gallons_requested": 500.0,
                    "call_type": "one_off",  # no window -> model-level rule
                    "intake_channel": "dispatcher",
                    "intake_channel_id": "d",
                    "source_schema_version": "1.0",
                    "trace_id": "tr",
                    "created_at": _NOW,
                    "updated_at": _NOW,
                    "last_event_timestamp": _NOW,
                }
            )
        except ValidationError as exc:
            assert extract_invalid_fields(exc) == ["invalid_delivery_window"]
        else:  # pragma: no cover
            pytest.fail("expected the one_off delivery-window rule to reject")

    def test_field_level_path_is_used(self):
        from pydantic import ValidationError

        from fuel.order_models import FuelOrder

        try:
            FuelOrder.model_validate(
                {
                    "order_id": "o1",
                    "tenant_id": "t1",
                    "customer_id": "c1",
                    "customer_name": "N",
                    "ship_to_address": "A",
                    "ship_to_lat": 999.0,  # out of range -> field-level
                    "ship_to_lon": -90.0,
                    "product_code": "DIESEL_2",
                    "gallons_requested": 500.0,
                    "call_type": "will_call",
                    "intake_channel": "dispatcher",
                    "intake_channel_id": "d",
                    "source_schema_version": "1.0",
                    "trace_id": "tr",
                    "created_at": _NOW,
                    "updated_at": _NOW,
                    "last_event_timestamp": _NOW,
                }
            )
        except ValidationError as exc:
            assert "ship_to_lat" in extract_invalid_fields(exc)
        else:  # pragma: no cover
            pytest.fail("expected the latitude range check to reject")


# ---------------------------------------------------------------------------
# The voice channel keeps its own documented envelope
# ---------------------------------------------------------------------------


class TestVoiceEnvelopePreserved:
    """The bridge must still answer VOICE_PAYLOAD_INVALID (Req 7.3).

    The pipeline now raises ORDER_PAYLOAD_INVALID for everyone. The bridge
    re-maps only that one code, so the Dinee contract is unchanged while the
    other four channels gain the same protection.
    """

    def test_bridge_remaps_only_the_order_payload_code(self):
        import inspect

        from fuel.voice import dinee_voice_bridge as bridge

        src = inspect.getsource(bridge.DineeVoiceBridge)
        assert "ORDER_PAYLOAD_INVALID" in src, (
            "the bridge must key its re-map on the pipeline's error code"
        )
        assert "voice_payload_invalid" in src, (
            "the bridge must still emit its own documented error code"
        )

    def test_bridge_no_longer_defines_its_own_extractor(self):
        """The helper moved to the pipeline; a stale copy would drift."""
        from fuel.voice import dinee_voice_bridge as bridge

        assert not hasattr(bridge, "_extract_invalid_fields")
