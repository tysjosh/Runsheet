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
    model_validator,
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
    fields, aligned to the Dinee runsheet-pack slot contract (assumption A5):

    * ``customer_name`` also accepts Dinee's ``customer`` key.
    * ``ship_to_address`` also accepts Dinee's ``delivery_site`` key.
    * ``quantity`` (``{gallons}`` / ``{fillToFull: true}``) is unpacked into
      the flat ``gallons_requested`` / ``fill_to_full`` fields.
    * ``delivery_window`` is a single free-text string on the Dinee side; it is
      preserved verbatim in ``delivery_window_note`` (not forced into the
      structured start/end fields).

    ``customer_name``, ``ship_to_address``, and ``product_code`` are required;
    coordinates are optional (voice captures no geocoding — reconciled during
    review-hold) and ``call_type`` defaults to ``will_call`` so no delivery
    window is required at intake.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    customer_id: Optional[str] = None
    customer_name: str = Field(
        validation_alias=AliasChoices("customer_name", "customer")
    )
    ship_to_address: str = Field(
        validation_alias=AliasChoices("ship_to_address", "delivery_site")
    )
    # Voice captures no coordinates; reconciled by a human during review-hold.
    ship_to_lat: Optional[float] = None
    ship_to_lon: Optional[float] = None
    product_code: str
    gallons_requested: Optional[float] = None
    fill_to_full: bool = False
    # Voice does not collect a call_type; default to will_call so the canonical
    # FuelOrder does not require a delivery window at intake (the dispatcher
    # attaches the window before the placed -> scheduled transition).
    call_type: Literal["will_call", "auto_fill", "keep_full", "one_off"] = "will_call"
    customer_tank_id: Optional[str] = None
    delivery_window_start: Optional[datetime] = None
    delivery_window_end: Optional[datetime] = None
    # Dinee emits a single free-text delivery_window (e.g. "tomorrow morning"),
    # not a structured start/end. Preserved verbatim for the review step.
    delivery_window_note: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _unpack_dinee_shapes(cls, data: Any) -> Any:
        """Normalize Dinee's nested/free-text slot shapes into flat fields.

        Runs before field validation so the canonical fields are populated
        from Dinee's ``quantity`` object and free-text ``delivery_window``.
        Explicit flat values (if a caller sends them) take precedence.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)  # never mutate the caller's dict

        quantity = data.get("quantity")
        if isinstance(quantity, dict):
            if "gallons" in quantity and data.get("gallons_requested") is None:
                data["gallons_requested"] = quantity.get("gallons")
            if quantity.get("fillToFull") is True and "fill_to_full" not in data:
                data["fill_to_full"] = True

        window = data.get("delivery_window")
        if (
            isinstance(window, str)
            and window.strip()
            and not data.get("delivery_window_note")
        ):
            data["delivery_window_note"] = window.strip()

        return data


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

        # Preserve Dinee's free-text delivery window for the review step. It is
        # not a structured start/end, so it lands in special_instructions rather
        # than the datetime fields (call_type=will_call means no window is
        # required at intake).
        if slots.delivery_window_note:
            order_doc["special_instructions"] = (
                f"Requested delivery window (voice): {slots.delivery_window_note}"
            )

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
