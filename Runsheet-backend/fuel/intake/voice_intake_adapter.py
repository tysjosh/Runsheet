"""
Voice intake adapter — transforms Dinee voice submissions into FuelOrders.

This adapter handles orders captured by the Dinee voice front-end and
submitted through the ``Dinee_Voice_Bridge`` onto the existing
``OrderIntakePipeline`` (``channel_type="voice"``). It stamps
``intake_channel="voice"``, ``intake_channel_id`` from the resolved channel,
and populates ``intake_metadata.call_id`` / ``intake_metadata.transcript`` /
``intake_metadata.agent_confidence`` from the submitted ``VoiceIntakePayload``.

Voice orders flagged for human review carry ``hold_reason="voice_review_required"``;
the ``VoiceReviewHoldHook`` (registered on the pipeline) promotes such orders
from ``status="placed"`` to ``status="on_hold"`` after the pipeline stamps the
platform-owned fields. This adapter never sets ``order_id`` / ``tenant_id`` /
``status`` / timestamps / ``trace_id`` — those are platform-assigned by the
``OrderIntakePipeline``.

Validates: Requirements 1.5, 7.1, 8.1.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from fuel.intake.adapter_base import AdapterError, IntakeContext, IntakeResult
from fuel.services.fuel_product_catalog import canonicalize


# ---------------------------------------------------------------------------
# Request-body models (VoiceIntakePayload)
# ---------------------------------------------------------------------------

#: Marker written to ``hold_reason`` when a voice order requires human review.
VOICE_REVIEW_HOLD_REASON = "voice_review_required"

#: Prefix stamped on the synthetic ``customer_id`` when a voice caller cannot
#: be resolved to an existing customer at intake time (design decision "A"):
#: the order is still accepted, forced into review-hold, and a human resolves
#: the real customer during review. The ``callId`` suffix keeps the provisional
#: id stable and traceable back to the originating call.
UNRESOLVED_CUSTOMER_PREFIX = "unresolved:"


class TranscriptTurn(BaseModel):
    """A single speaker turn in the voice call transcript.

    Accepts either ``speaker`` (canonical) or ``role`` (the Dinee runtime's
    native key) for the turn's author — the backend aligns to the fixed Dinee
    client contract (assumption A5). Any extra keys the Dinee runtime emits
    (e.g. ``at``) are ignored.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    speaker: str = Field(validation_alias=AliasChoices("speaker", "role"))
    text: str


class VoiceExtractedSlots(BaseModel):
    """The order fields the Dinee agent extracted from the call.

    Maps onto the canonical :class:`~fuel.order_models.FuelOrder` business
    fields. ``customer_name``, ``ship_to_address``, and ``product_code`` are
    required; the remainder are optional or carry defaults.
    """

    model_config = ConfigDict(extra="ignore")

    customer_id: Optional[str] = None
    customer_name: str
    ship_to_address: str
    ship_to_lat: float
    ship_to_lon: float
    product_code: str
    gallons_requested: Optional[float] = None
    fill_to_full: bool = False
    call_type: Literal["will_call", "auto_fill", "keep_full", "one_off"] = "one_off"
    customer_tank_id: Optional[str] = None
    delivery_window_start: Optional[datetime] = None
    delivery_window_end: Optional[datetime] = None


class VoiceIntakePayload(BaseModel):
    """The JSON request body submitted to the voice order endpoint.

    Tolerant of extra Dinee fields (``extra="ignore"``) so the contract can
    evolve without breaking intake. The ``X-Schema-Version`` header — not the
    advisory ``schemaVersion`` field — is authoritative for schema selection.
    """

    model_config = ConfigDict(extra="ignore")

    callId: str
    transcriptId: str
    transcript: List[TranscriptTurn]
    callerPhone: Optional[str] = None
    extractedSlots: VoiceExtractedSlots
    reviewRequired: bool = True
    schemaVersion: Optional[str] = None
    agentConfidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Voice intake adapter
# ---------------------------------------------------------------------------


class VoiceIntakeAdapter:
    """Intake adapter for the Dinee voice channel.

    Transforms a :class:`VoiceIntakePayload` into a canonical FuelOrder
    document plus a single ``order_placed`` event. Registered in the
    ``IntakeAdapterRegistry`` for ``channel_type="voice"``.

    Attributes:
        channel_type: Always ``"voice"``.
        schema_version: The schema version this adapter handles (``"1.0"``).
    """

    channel_type: str = "voice"
    schema_version: str = "1.0"

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        """Transform a Dinee voice payload into a FuelOrder + events.

        The bridge performs required-field validation before the pipeline
        call (returning HTTP 422 with the missing fields), so a structurally
        valid payload is expected here. Parsing is repeated defensively: any
        structural problem raises :class:`AdapterError` (routed to the poison
        queue by the pipeline).

        Args:
            payload: The raw JSON body of the voice submission.
            context: Per-request context carrying tenant identity, the
                     resolved voice intake channel, and trace info.

        Returns:
            An IntakeResult with the order_doc and a single ``order_placed``
            event.

        Raises:
            AdapterError: When the payload cannot be parsed into a
                :class:`VoiceIntakePayload`.
        """
        try:
            parsed = VoiceIntakePayload.model_validate(payload)
        except ValidationError as exc:
            raise AdapterError(
                error_type="adapter_validation_failed",
                message=f"Invalid VoiceIntakePayload: {exc}",
            ) from exc

        slots = parsed.extractedSlots

        # Canonicalize the product_code through the fuel product catalog.
        # The FuelOrder validator also canonicalizes, so this is
        # defense-in-depth; an unknown product surfaces as an AdapterError.
        try:
            product_code = canonicalize(slots.product_code)
        except Exception as exc:  # UnknownFuelProductError / TypeError
            raise AdapterError(
                error_type="adapter_validation_failed",
                message=f"Unknown product_code {slots.product_code!r}: {exc}",
            ) from exc

        # Customer resolution (design decision "A"): a voice caller who does
        # not resolve to an existing customer at intake time still yields an
        # accepted order — the canonical FuelOrder requires a non-empty
        # ``customer_id``, so we stamp a provisional, call-scoped reference and
        # force the order into review-hold below. A human resolves the real
        # customer during review. This keeps the platform-wide FuelOrder
        # invariant intact instead of relaxing it.
        raw_customer_id = slots.customer_id
        customer_unresolved = not (raw_customer_id and raw_customer_id.strip())
        customer_id = (
            f"{UNRESOLVED_CUSTOMER_PREFIX}{parsed.callId}"
            if customer_unresolved
            else raw_customer_id
        )

        transcript_text = self._join_transcript(parsed.transcript)

        # Build the order document — adapters own business shape only.
        order_doc: Dict[str, Any] = {
            # Customer reference
            "customer_id": customer_id,
            "customer_name": slots.customer_name,
            "customer_phone": parsed.callerPhone,
            "ship_to_address": slots.ship_to_address,
            "ship_to_lat": slots.ship_to_lat,
            "ship_to_lon": slots.ship_to_lon,
            "customer_tank_id": slots.customer_tank_id,
            # Product details
            "product_code": product_code,
            "gallons_requested": slots.gallons_requested,
            "fill_to_full": slots.fill_to_full,
            "call_type": slots.call_type,
            "delivery_window_start": slots.delivery_window_start,
            "delivery_window_end": slots.delivery_window_end,
            # Intake provenance
            "intake_channel": "voice",
            "intake_channel_id": context.channel.channel_id,
            "intake_metadata": {
                "call_id": parsed.callId,
                "transcript": transcript_text,
                "agent_confidence": parsed.agentConfidence,
            },
            "source_schema_version": self.schema_version,
        }

        # Human-review disposition (Req 8.1): stamp the hold reason so the
        # registered VoiceReviewHoldHook promotes the order to on_hold after
        # the pipeline stamps status="placed". Status itself stays
        # platform-owned and is never set by the adapter. An unresolved
        # customer always forces review-hold — even when the agent did not set
        # ``reviewRequired`` — because a provisional customer_id must be
        # reconciled by a human before the order can proceed.
        if parsed.reviewRequired or customer_unresolved:
            order_doc["hold_reason"] = VOICE_REVIEW_HOLD_REASON

        # Emit a single order_placed event.
        event_docs = [
            {
                "event_type": "order_placed",
                "event_payload": {
                    "intake_channel": "voice",
                    "intake_channel_id": context.channel.channel_id,
                    "call_id": parsed.callId,
                    "transcript_id": parsed.transcriptId,
                    "agent_confidence": parsed.agentConfidence,
                    "review_required": parsed.reviewRequired,
                },
            }
        ]

        return IntakeResult(order_doc=order_doc, event_docs=event_docs)

    @staticmethod
    def is_unresolved_customer_id(customer_id: Optional[str]) -> bool:
        """Return ``True`` when ``customer_id`` is a provisional voice reference.

        A voice order whose caller could not be resolved to an existing
        customer at intake carries a synthetic ``customer_id`` of the form
        ``unresolved:{callId}`` (design decision "A"). Review tooling uses this
        to flag the order for customer reconciliation.
        """
        return bool(customer_id) and customer_id.startswith(
            UNRESOLVED_CUSTOMER_PREFIX
        )

    @staticmethod
    def _join_transcript(transcript: List[TranscriptTurn]) -> Optional[str]:
        """Render the transcript turns into a single newline-joined string.

        Returns ``None`` for an empty transcript so ``intake_metadata.transcript``
        stays unset rather than an empty string.
        """
        if not transcript:
            return None
        return "\n".join(f"{turn.speaker}: {turn.text}" for turn in transcript)


__all__ = [
    "TranscriptTurn",
    "VoiceExtractedSlots",
    "VoiceIntakePayload",
    "VoiceIntakeAdapter",
    "VOICE_REVIEW_HOLD_REASON",
    "UNRESOLVED_CUSTOMER_PREFIX",
]
