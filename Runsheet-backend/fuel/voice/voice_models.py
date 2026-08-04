"""
Shared response models for the Dinee voice submission surface (Surface A).

The :class:`VoiceSubmissionResponse` is the acceptance-response contract the
:class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge` produces and the
``voice_submission_router`` (task 5.2) returns from ``POST /voice/orders``.
It lives in this shared module (rather than the router) so the bridge can
map an :class:`~fuel.services.order_intake_pipeline.IntakeResponse` onto it
without creating a bridge→router import cycle.

The response carries only the pipeline-assigned order id and a coarse
disposition — never tenant data, transcript content, or credential values
(Req 9.1, 9.3).

Validates: Requirements 9.1, 9.2, 9.3.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: The acceptance dispositions surfaced to the Dinee voice client.
#:
#: - ``accepted``    — a new order was placed (``status="placed"``).
#: - ``review_hold`` — a new order was placed but held for human review
#:   (``status="on_hold"``; the submission set ``reviewRequired=true``).
#: - ``duplicate``   — a replay of a prior submission (same idempotency key +
#:   same body); the original order id is returned unchanged (Req 9.2).
VoiceDisposition = Literal["accepted", "review_hold", "duplicate"]


class VoiceSubmissionResponse(BaseModel):
    """Acceptance response for a Dinee voice order submission.

    Attributes:
        orderId: The pipeline-assigned canonical order id.
        disposition: The coarse acceptance outcome (see :data:`VoiceDisposition`).
    """

    orderId: str = Field(..., description="The pipeline-assigned order id")
    disposition: VoiceDisposition = Field(
        ..., description="accepted | review_hold | duplicate"
    )


__all__ = ["VoiceDisposition", "VoiceSubmissionResponse"]
