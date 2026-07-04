"""
Surface A — Voice Order Submission router (``POST /voice/orders``).

The Dinee ``ws-server`` posts signed, voice-captured orders to this endpoint on
behalf of a tenant. The router is a thin FastAPI shell: it reads the fixed Dinee
header contract (``X-Runsheet-Tenant`` / ``X-Idempotency-Key`` / ``X-Timestamp``
/ ``X-Schema-Version`` / ``X-Signature``) and the exact raw request body, then
delegates every validation and persistence decision to the injected
:class:`~fuel.voice.dinee_voice_bridge.DineeVoiceBridge`.

The bridge maps the contract onto the **existing** ``OrderIntakePipeline``
(no parallel path) and enforces the normative validation ordering
(signature → replay window → tenant/channel → idempotency → schema/required
fields). It raises :class:`~errors.exceptions.AppException` with the
stage-appropriate status code on any failure; the app-wide exception handlers
(``errors.handlers.register_exception_handlers``) render those into the
structured JSON envelope, so this router never translates errors by hand.

The path (``/voice/orders``) follows open assumption A5: the bridge is mounted
where the Dinee client actually posts. Per the design's A1 snippet the router
carries no prefix and groups under the ``voice-intake`` tag.

Wiring: the constructed :class:`DineeVoiceBridge` is injected at bootstrap
(task 10.1) via :func:`configure_voice_submission_router` so the module-level
handler can reach it without an import cycle — mirroring the sibling
``voice_read_driver_router.configure_voice_read_driver_router`` pattern.

Requirements: 1.1, 1.2, 1.3, 1.4, 9.1, 9.2
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, Request

from errors.exceptions import internal_error
from fuel.voice.voice_models import VoiceSubmissionResponse

logger = logging.getLogger(__name__)

__all__ = [
    "router",
    "configure_voice_submission_router",
]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
#
# No prefix: the Dinee client posts to the bare ``/voice/orders`` path (A5). The
# ``voice-intake`` tag groups the submission endpoint in the OpenAPI schema.
router = APIRouter(tags=["voice-intake"])


# ---------------------------------------------------------------------------
# Module-level bridge reference, wired via configure_voice_submission_router()
# at bootstrap (task 10.1). Left as None until wired so importing this module
# never requires a constructed pipeline/ledger.
# ---------------------------------------------------------------------------

_bridge: Any = None


def configure_voice_submission_router(*, bridge: Any) -> None:
    """Wire the constructed :class:`DineeVoiceBridge` into the router.

    Called from the bootstrap layer (task 10.1) once the shared
    ``OrderIntakePipeline``, the tenant intake-channel repository, and the
    :class:`~fuel.voice.voice_submission_ledger.VoiceSubmissionLedger` are
    available and the bridge has been constructed around them.

    Args:
        bridge: The :class:`DineeVoiceBridge` that validates and submits voice
            orders through the existing intake pipeline.
    """
    global _bridge
    _bridge = bridge


# ---------------------------------------------------------------------------
# Requirement 1 / 2 / 9 — voice order submission endpoint
# ---------------------------------------------------------------------------


@router.post("/voice/orders", response_model=VoiceSubmissionResponse)
async def submit_voice_order(
    request: Request,
    x_runsheet_tenant: str = Header(..., alias="X-Runsheet-Tenant"),
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
    x_timestamp: Optional[str] = Header(None, alias="X-Timestamp"),
    x_schema_version: Optional[str] = Header(None, alias="X-Schema-Version"),
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
) -> VoiceSubmissionResponse:
    """Accept a signed Dinee voice order and drive it through the pipeline.

    Reads the fixed Dinee header contract (Req 2.1) and the exact raw body —
    the canonical body over which the HMAC signature was computed (Req 2.5) —
    and delegates to the bridge. The bridge enforces the full validation
    ordering and, on acceptance, persists the order through the existing
    ``OrderIntakePipeline`` and returns the pipeline-assigned order id plus a
    disposition (Req 1.1, 1.2, 1.3, 9.1). A same-key/same-body replay returns
    the original order id unchanged (Req 9.2).

    All validation and persistence failures are raised by the bridge as
    :class:`AppException` and rendered by the app-wide exception handlers with
    the stage-appropriate status code; none leak tenant data or credentials.

    Validates: Requirements 1.1, 1.2, 1.3, 1.4, 9.1, 9.2
    """
    if _bridge is None:
        # The submission endpoint must not be served without a wired bridge
        # (bootstrap fail-closed). Surface a uniform internal error rather than
        # a bare AttributeError.
        logger.error(
            "voice_submission_router received a request before the "
            "DineeVoiceBridge was configured"
        )
        raise internal_error(message="Voice submission is not available")

    raw_body = await request.body()
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())

    return await _bridge.submit(
        raw_body=raw_body,
        tenant_id=x_runsheet_tenant,
        idempotency_key=x_idempotency_key,
        timestamp=x_timestamp,
        schema_version=x_schema_version,
        signature=x_signature,
        request_id=request_id,
    )
