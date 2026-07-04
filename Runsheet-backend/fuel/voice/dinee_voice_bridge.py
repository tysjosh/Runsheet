"""
Dinee Voice Bridge — Surface A submission bridge (``POST /voice/orders``).

The bridge maps the fixed Dinee voice submission header contract onto the
**existing** :class:`~fuel.services.order_intake_pipeline.OrderIntakePipeline`.
It is deliberately *not* a parallel intake path: it adds only what the
pipeline lacks for the Dinee contract (replay-window enforcement, an
idempotency conflict / order-id-recall ledger, and bridge-boundary
schema-version + required-field validation) and then invokes the single
pipeline through the tenant's registered voice channel.

Normative validation ordering (each stage short-circuits; **no order is
persisted before the pipeline call**):

    1. **Signature** (401) — ``X-Signature`` must be present and carry the
       ``sha256=`` prefix. The prefix is stripped and the hex lower-cased.
       The *authoritative* HMAC-SHA256 verification is delegated to the
       pipeline's :meth:`OrderIntakePipeline._verify_hmac` inside
       ``ingest_webhook`` (computed over the exact raw body bytes).
    2. **Replay window** (401) — ``X-Timestamp`` must be present, parseable
       (ISO-8601 or epoch seconds), and within ``replay_window_seconds`` of
       server time. Evaluated *before* the pipeline call (Req 4.4).
    3. **Tenant / voice-channel resolution** (404 / 403) — the tenant's
       enabled voice channel is resolved via
       :class:`~fuel.intake_channel_repository.IntakeChannelRepository`
       (never client-supplied). No enabled voice channel → uniform 404. A
       payload ``tenant_id`` that disagrees with the resolved channel's
       tenant → 403.
    4. **Idempotency** (400 / 409 / replay) — a missing ``X-Idempotency-Key``
       → 400. The :class:`~fuel.voice.voice_submission_ledger.VoiceSubmissionLedger`
       pre-check: same key + *different* body → 409; same key + *same* body →
       the prior outcome is replayed (the original order id, Req 9.2) without
       re-invoking the pipeline.
    5. **Schema version** (422) — ``X-Schema-Version`` must be present and in
       ``channel.supported_schema_versions``. Checked at the bridge so an
       unsupported version returns 422 rather than a poison-queue 200.
    6. **Required fields** (422) — the body is parsed into a
       :class:`~fuel.intake.voice_intake_adapter.VoiceIntakePayload`; a
       validation error returns 422 with ``details.missing_fields``. Checked
       at the bridge so a structurally-invalid payload returns 422 rather than
       a poison-queue 200.

On a ``processed`` pipeline result the bridge records the outcome in the
ledger and maps the :class:`IntakeResponse` onto a
:class:`~fuel.voice.voice_models.VoiceSubmissionResponse` (order id +
disposition). Rejections never leak tenant data, transcript content, or
credential values (Req 9.3).

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 4.4, 5.3, 6.1, 6.2, 6.3,
7.2, 7.3, 9.1, 9.3.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from pydantic import ValidationError

from errors.exceptions import (
    idempotency_conflict,
    missing_idempotency_key,
    resource_not_found,
    unsupported_schema_version,
    voice_payload_invalid,
    voice_replay_window_exceeded,
    voice_tenant_mismatch,
    voice_unauthorized,
)
from fuel.intake.voice_intake_adapter import VoiceIntakePayload
from fuel.services.order_intake_pipeline import IntakeResponse
from fuel.voice.voice_models import VoiceDisposition, VoiceSubmissionResponse
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: Optional signature prefix required by the Dinee ``X-Signature`` contract.
_SHA256_PREFIX = "sha256="


class DineeVoiceBridge:
    """Header-contract bridge that drives the existing intake pipeline.

    Dependencies are injected so the bridge is trivially testable with
    recording fakes:

        * ``pipeline`` — the shared :class:`OrderIntakePipeline`. Its
          ``ingest_webhook`` extension (``idempotency_key_override`` /
          ``schema_version_override``) is used to map the Dinee headers onto
          the single pipeline, and its ``_verify_hmac`` performs the
          authoritative signature check inside that call.
        * ``intake_channel_repo`` — resolves the tenant's enabled voice
          channel (``get_voice_channel``).
        * ``ledger`` — the :class:`VoiceSubmissionLedger` for conflict
          detection and order-id recall.
        * ``replay_window_seconds`` — allowed clock skew for ``X-Timestamp``
          (default 300, sourced from settings by the router/bootstrap).
        * ``clock`` — injectable "now" for deterministic replay-window tests.
    """

    def __init__(
        self,
        *,
        pipeline: Any,
        intake_channel_repo: Any,
        ledger: Any,
        replay_window_seconds: int = 300,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._pipeline = pipeline
        self._intake_channel_repo = intake_channel_repo
        self._ledger = ledger
        self._replay_window_seconds = int(replay_window_seconds)
        self._clock = clock or utcnow

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def submit(
        self,
        *,
        raw_body: bytes,
        tenant_id: str,
        idempotency_key: Optional[str],
        timestamp: Optional[str],
        schema_version: Optional[str],
        signature: Optional[str],
        request_id: Optional[str] = None,
    ) -> VoiceSubmissionResponse:
        """Validate and submit a Dinee voice order.

        Args:
            raw_body: The exact raw request bytes — the canonical body over
                which the HMAC signature was computed. Never re-serialized.
            tenant_id: The value of the ``X-Runsheet-Tenant`` header.
            idempotency_key: The value of the ``X-Idempotency-Key`` header.
            timestamp: The value of the ``X-Timestamp`` header.
            schema_version: The value of the ``X-Schema-Version`` header.
            signature: The value of the ``X-Signature`` header (``sha256=`` +
                lowercase hex).
            request_id: An optional trace id; generated when absent.

        Returns:
            A :class:`VoiceSubmissionResponse` with the order id and
            disposition.

        Raises:
            AppException: With the stage-appropriate status code on any
                validation failure (see module docstring for the ordering).
        """
        request_id = request_id or str(uuid.uuid4())

        # --- (1) Signature presence / format -----------------------------
        # The authoritative HMAC verification is delegated to the pipeline's
        # _verify_hmac inside ingest_webhook. Here we only require the header
        # to be present and to carry the sha256= prefix, then strip+lowercase
        # so a forged/absent signature is rejected before we touch any state.
        stripped_signature = self._normalize_signature(signature)

        # --- (2) Replay window (before the pipeline call, Req 4.4) --------
        self._assert_replay_window(timestamp)

        # --- (3) Tenant / voice-channel resolution ------------------------
        channel = await self._intake_channel_repo.get_voice_channel(tenant_id)
        if channel is None:
            # Uniform 404 — does not leak whether the tenant exists (Req 2.4).
            raise resource_not_found(
                message="No enabled voice channel is registered for this tenant",
                details={"channel_type": "voice"},
            )

        payload_dict = self._parse_body(raw_body)

        # payload tenant_id (if present) must match the resolved channel's
        # tenant (Req 2.4). The scope is always the resolved channel — the
        # payload can only agree, never override.
        payload_tenant = payload_dict.get("tenant_id")
        if payload_tenant is not None and payload_tenant != channel.tenant_id:
            raise voice_tenant_mismatch(
                details={"reason": "payload tenant_id does not match the voice channel"},
            )

        # --- (4) Idempotency ----------------------------------------------
        if not idempotency_key:
            raise missing_idempotency_key()

        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        prior = await self._ledger.lookup(channel.tenant_id, idempotency_key)
        if prior is not None:
            if prior.body_sha256 != body_sha256:
                # Same key, different body → conflict (Req 5.4). No order.
                raise idempotency_conflict(
                    details={"idempotency_key": idempotency_key},
                )
            # Same key, same body → replay the original outcome (Req 9.2).
            return VoiceSubmissionResponse(
                orderId=prior.order_id or "",
                disposition=self._coerce_disposition(prior.disposition),
            )

        # --- (5) Schema version (bridge boundary → 422, not poison 200) ---
        supported = getattr(channel, "supported_schema_versions", []) or []
        if not schema_version or schema_version not in supported:
            raise unsupported_schema_version(
                details={"supported_schema_versions": list(supported)},
            )

        # --- (6) Required-field validation (bridge boundary → 422) --------
        parsed = self._validate_payload(payload_dict)

        # --- (7) Pipeline invocation --------------------------------------
        # The pipeline performs the authoritative HMAC verification, tenant
        # re-check, idempotency, adapter dispatch, persistence, and broadcast.
        result: IntakeResponse = await self._pipeline.ingest_webhook(
            channel_id=channel.channel_id,
            body=raw_body,
            signature=stripped_signature,
            request_id=request_id,
            idempotency_key_override=idempotency_key,
            schema_version_override=schema_version,
        )

        return await self._map_result(
            result=result,
            tenant_id=channel.tenant_id,
            idempotency_key=idempotency_key,
            body_sha256=body_sha256,
            review_required=parsed.reviewRequired,
        )

    # ------------------------------------------------------------------
    # Stage helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_signature(signature: Optional[str]) -> str:
        """Require the ``sha256=`` prefix, strip it, and lower-case the hex.

        Raises:
            voice_unauthorized: When the header is absent or does not carry
                the required ``sha256=`` prefix (Req 2.2).
        """
        if not signature or not signature.strip():
            raise voice_unauthorized(
                message="The X-Signature header is required",
                details={"reason": "missing signature"},
            )
        candidate = signature.strip()
        if not candidate.lower().startswith(_SHA256_PREFIX):
            raise voice_unauthorized(
                message="The X-Signature header must carry the 'sha256=' prefix",
                details={"reason": "malformed signature"},
            )
        return candidate[len(_SHA256_PREFIX):].lower()

    def _assert_replay_window(self, timestamp: Optional[str]) -> None:
        """Reject a missing, unparseable, or stale ``X-Timestamp`` (Req 4).

        Raises:
            voice_replay_window_exceeded: HTTP 401 when the timestamp is
                absent, cannot be parsed, or is more than
                ``replay_window_seconds`` from server time.
        """
        if not timestamp or not timestamp.strip():
            raise voice_replay_window_exceeded(
                details={"reason": "missing timestamp"},
            )

        parsed = self._parse_timestamp(timestamp.strip())
        if parsed is None:
            raise voice_replay_window_exceeded(
                details={"reason": "unparseable timestamp"},
            )

        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        skew = abs((now - parsed).total_seconds())
        if skew > self._replay_window_seconds:
            raise voice_replay_window_exceeded(
                details={"reason": "timestamp outside replay window"},
            )

    @staticmethod
    def _parse_timestamp(value: str) -> Optional[datetime]:
        """Parse an ISO-8601 or epoch-seconds timestamp into aware UTC.

        Returns ``None`` when the value cannot be interpreted as either an
        epoch-seconds number or an ISO-8601 datetime. Naive datetimes are
        assumed to be UTC.
        """
        # Try epoch seconds (int or float) first.
        try:
            epoch = float(value)
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass

        # Fall back to ISO-8601. Accept a trailing 'Z' as UTC.
        iso = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _parse_body(raw_body: bytes) -> dict:
        """Parse the raw body as a JSON object.

        A body that is not valid JSON or not a JSON object cannot carry the
        required voice fields, so it surfaces as a 422 payload-invalid error
        rather than a 500 (Req 7.2/7.3).
        """
        try:
            data = json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            raise voice_payload_invalid(
                message="The voice submission body is not valid JSON",
                details={"missing_fields": [], "reason": "invalid JSON"},
            ) from exc
        if not isinstance(data, dict):
            raise voice_payload_invalid(
                message="The voice submission body must be a JSON object",
                details={"missing_fields": [], "reason": "not a JSON object"},
            )
        return data

    @staticmethod
    def _validate_payload(payload_dict: dict) -> VoiceIntakePayload:
        """Construct the :class:`VoiceIntakePayload`, mapping errors to 422.

        Raises:
            voice_payload_invalid: HTTP 422 with ``details.missing_fields``
                naming the absent/invalid required fields (Req 7.2, 7.3).
        """
        try:
            return VoiceIntakePayload.model_validate(payload_dict)
        except ValidationError as exc:
            missing_fields = _extract_missing_fields(exc)
            raise voice_payload_invalid(missing_fields=missing_fields) from exc

    async def _map_result(
        self,
        *,
        result: IntakeResponse,
        tenant_id: str,
        idempotency_key: str,
        body_sha256: str,
        review_required: bool,
    ) -> VoiceSubmissionResponse:
        """Map an :class:`IntakeResponse` onto the acceptance response.

        On a fresh ``processed`` result the outcome is recorded in the ledger
        so a later same-key/same-body retry is recalled (Req 9.2) and a
        same-key/different-body retry conflicts (Req 5.4).
        """
        status = result.status

        if status == "processed":
            disposition: VoiceDisposition = (
                "review_hold" if review_required else "accepted"
            )
            await self._ledger.record(
                tenant_id,
                idempotency_key,
                body_sha256,
                result.order_id,
                disposition,
            )
            return VoiceSubmissionResponse(
                orderId=result.order_id or "",
                disposition=disposition,
            )

        if status == "duplicate":
            # The pipeline's own idempotency store deduped this (e.g. the
            # ledger entry expired but the pipeline marker survived). Surface
            # a duplicate disposition; the order id is unknown here.
            return VoiceSubmissionResponse(
                orderId=result.order_id or "",
                disposition="duplicate",
            )

        # Any other status (queued_for_review / legacy_passthrough) means the
        # payload could not be accepted as a valid order. Bridge pre-validation
        # should catch the common cases; surface a uniform 422 rather than a
        # misleading acceptance, leaking no internal detail.
        logger.warning(
            "DineeVoiceBridge: unexpected pipeline status %r for tenant=%s "
            "(request treated as unprocessable)",
            status,
            tenant_id,
        )
        raise voice_payload_invalid(
            message="The voice submission could not be processed",
            details={"missing_fields": []},
        )

    @staticmethod
    def _coerce_disposition(value: str) -> VoiceDisposition:
        """Coerce a stored ledger disposition to a known literal.

        A replayed entry always carries the disposition recorded at first
        sight; an unrecognized value degrades to ``duplicate`` for safety.
        """
        if value in ("accepted", "review_hold", "duplicate"):
            return value  # type: ignore[return-value]
        return "duplicate"


def _extract_missing_fields(exc: ValidationError) -> List[str]:
    """Extract the dotted field paths that failed validation.

    Prefers ``type == "missing"`` errors (absent required fields) so the
    response names exactly what the caller omitted; falls back to every
    error location when no field is strictly missing (e.g. a wrong type on a
    required field). Never includes any submitted values — only field names.
    """
    missing: List[str] = []
    others: List[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        if not loc:
            continue
        if err.get("type") == "missing":
            missing.append(loc)
        else:
            others.append(loc)
    fields = missing or others
    # De-duplicate while preserving order.
    seen: set = set()
    deduped: List[str] = []
    for f in fields:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped


__all__ = ["DineeVoiceBridge"]
