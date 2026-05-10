"""
Meter-Ticket OCR Service — AWS Textract-backed extraction of delivered gallons
from driver-uploaded meter-ticket images.

Capability 4 (Requirement 4.2) of the fuel-ops hardening spec requires the
driver POD flow to auto-extract ``delivered_gallons`` from a photographed
meter ticket so drivers no longer hand-type numbers on their phones. This
module is that service. It is deliberately small and single-purpose:

    1. Fetch the meter-ticket image bytes from S3 via
       :class:`services.file_storage_service.FileStorageService` so the
       tenant-prefixed access control and audit log are honored before a
       Textract call is ever made.
    2. Call AWS Textract ``AnalyzeDocument`` with the ``FORMS`` feature to
       obtain key/value pair blocks.
    3. Parse ``extracted_gallons`` by scanning the KV pairs for keys whose
       normalized text contains ``GAL``, ``GALLONS``, or ``GROSS`` (the
       three most common fields on US meter tickets) and picking the first
       value that parses as a positive float.
    4. Compute ``confidence`` as the average confidence across every block
       Textract returned (scaled from Textract's 0–100 range into the
       [0.0, 1.0] range required by Requirement 4.2.1).
    5. Set ``requires_manual_review`` when ``confidence`` is below the
       tenant-configurable threshold (default 0.85, overridable per tenant
       via Redis key ``ocr_confidence_threshold:{tenant_id}`` — see
       :meth:`MeterTicketOCRService._resolve_confidence_threshold`).
    6. Persist the :class:`OCRResult` to the ``meter_ticket_ocr_results``
       ES index (mapping defined in
       :mod:`fuel.services.fuel_ops_es_mappings`).

The service is callable from the POD submission flow (task 8.4) and from
background re-processing jobs. On any Textract error or parse failure the
service still returns a well-formed ``OCRResult`` with
``extracted_gallons=None``, ``confidence=0.0``, ``requires_manual_review=True``,
and an ``error_details`` string so the caller can log the failure and fall
back to manual entry (Requirement 4.2.6). This separation — never raising
Textract errors outward — lets the POD endpoint treat the service as an
always-responsive enrichment step.

Tenant isolation is enforced at two layers:

* The injected :class:`FileStorageService` rejects cross-tenant ``file_ref``
  values with ``PermissionError`` before the Textract call.
* The persisted OCR result document carries ``tenant_id`` as a keyword so
  every downstream query in the POD / reconciliation paths can filter on
  the caller's tenant.

Validates: Requirements 4.2.1, 4.2.2, 4.2.3.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from fuel.services.fuel_ops_es_mappings import METER_TICKET_OCR_RESULTS_INDEX
from services.external_call_tracing import (
    CircuitBreaker,
    CircuitOpenError,
    default_circuit_breaker,
    trace_external_call,
)
from services.metrics import fuelops_ocr_calls_total

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default confidence threshold below which we flag ``requires_manual_review``
#: (Requirement 4.2.3). Overridable per tenant via Redis key
#: ``ocr_confidence_threshold:{tenant_id}``.
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.85

#: Default Textract call timeout (seconds). Matches Requirement 4.2.6 which
#: caps OCR latency at 15s before the POD flow falls back to manual entry.
DEFAULT_TEXTRACT_TIMEOUT_SECONDS: float = 15.0

#: Provider label persisted on each ``OCRResult`` so downstream analytics can
#: distinguish between engines if a non-Textract adapter is introduced later.
PROVIDER_NAME: str = "aws_textract"

#: Redis key pattern for the tenant-configurable threshold override.
_CONFIDENCE_THRESHOLD_KEY_PATTERN: str = "ocr_confidence_threshold:{tenant_id}"

#: Textract FeatureTypes we request on every ``AnalyzeDocument`` call.
_TEXTRACT_FEATURES: Tuple[str, ...] = ("FORMS",)

#: KV-pair key tokens considered a match for "delivered gallons" on US meter
#: tickets, ordered by preference. ``GROSS`` is scanned last because some
#: tickets print ``GROSS GAL`` and ``NET GAL`` and we prefer to match the
#: more specific ``GAL`` token when both are present.
_GALLON_KEY_TOKENS: Tuple[str, ...] = ("GALLONS", "GAL", "GROSS")

#: KV-pair key tokens for meter number extraction (Requirement 8.1).
#: US meter tickets typically label this field as "METER NO", "METER #",
#: "METER NUMBER", or just "METER" followed by a serial-like value.
_METER_NUMBER_KEY_TOKENS: Tuple[str, ...] = ("METER NUMBER", "METER NO", "METER #", "METER")

#: KV-pair key tokens for ticket number extraction (Requirement 8.1).
#: US meter tickets label this as "TICKET NO", "TICKET #", "TICKET NUMBER",
#: or "TICKET" followed by a sequential number.
_TICKET_NUMBER_KEY_TOKENS: Tuple[str, ...] = ("TICKET NUMBER", "TICKET NO", "TICKET #", "TICKET")

#: Regex extracting the first positive float from a free-text value cell.
#: Handles e.g. ``"GALLONS: 1,234.56"`` → ``1234.56`` and ``"GAL 780"`` → 780.
_NUMERIC_RE: re.Pattern[str] = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")

#: Regex extracting an alphanumeric identifier (meter number or ticket number).
#: Handles values like "M-12345", "SN 98765", "12345678", "MT-2024-001".
_IDENTIFIER_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9][\w\-./]*[A-Za-z0-9]|[A-Za-z0-9]+")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class OCRResult(BaseModel):
    """Strict result model for a single meter-ticket OCR pass.

    Persisted 1:1 to the ``meter_ticket_ocr_results`` ES index, matching the
    mapping in :mod:`fuel.services.fuel_ops_es_mappings` so
    ``model_dump(mode="json")`` is a valid indexing payload.
    """

    model_config = ConfigDict(extra="forbid")

    ocr_result_id: str = Field(
        ...,
        min_length=1,
        description="Unique id (UUID4) assigned by the service for this OCR pass.",
    )
    tenant_id: str = Field(..., min_length=1)
    pod_id: Optional[str] = Field(
        default=None,
        description=(
            "Associated POD id when the OCR is triggered inline from the POD "
            "submission flow. ``None`` for pre-POD preview calls."
        ),
    )
    file_ref: str = Field(
        ...,
        min_length=1,
        description=(
            "Tenant-prefixed S3 key of the meter-ticket image this result was "
            "derived from. Must pass FileStorageService.validate_ref."
        ),
    )
    meter_number: Optional[str] = Field(
        default=None,
        description=(
            "Physical meter serial/identification number extracted from the "
            "ticket. ``None`` when no KV pair matching the meter number key "
            "tokens was found. (Requirement 8.1)"
        ),
    )
    ticket_number: Optional[str] = Field(
        default=None,
        description=(
            "Sequential meter ticket number extracted from the ticket. "
            "``None`` when no KV pair matching the ticket number key tokens "
            "was found. (Requirement 8.1)"
        ),
    )
    extracted_gallons: Optional[float] = Field(
        default=None,
        description=(
            "Parsed gallon count. ``None`` when Textract returned no KV pair "
            "matching the gallon key tokens or when every match failed to "
            "parse as a positive number."
        ),
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Average block confidence in [0.0, 1.0].",
    )
    raw_text: str = Field(
        default="",
        description=(
            "Concatenated ``LINE`` block text joined by newlines, suitable "
            "for dispatcher review when ``requires_manual_review`` is True."
        ),
    )
    requires_manual_review: bool = Field(
        default=True,
        description=(
            "True when ``confidence`` is below the tenant-configured "
            "threshold, when parsing failed, or when the provider errored."
        ),
    )
    provider: str = Field(default=PROVIDER_NAME)
    processed_at: datetime = Field(...)
    error_details: Optional[str] = Field(
        default=None,
        description=(
            "Free-text provider error on failure (timeout, throttling, "
            "unsupported format). ``None`` on a clean run."
        ),
    )
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedGallons:
    """Internal container for a parsed gallon value and its provenance."""

    gallons: float
    key_text: str
    value_text: str


class MeterTicketOCRService:
    """Extract ``delivered_gallons`` from meter-ticket images via AWS Textract.

    The service is deliberately dependency-injectable: the Textract client,
    the :class:`FileStorageService`, the ES service, and the Redis client for
    tenant-threshold lookups are all passed in. This lets the unit tests in
    ``tests/unit/test_meter_ticket_ocr_service.py`` exercise every code path
    without ever calling AWS.

    Args:
        file_storage: :class:`FileStorageService` instance used to fetch the
            meter-ticket bytes. The service's tenant-prefix check is the
            first line of defense against cross-tenant OCR requests.
        es_service: Any object with an async ``index_document(index, id, doc)``
            coroutine (``ElasticsearchService`` satisfies this).
        textract_client: Optional pre-built boto3 Textract client. When
            omitted, boto3 is imported lazily on first call so the module
            can be imported in environments without AWS credentials (unit
            tests, CI).
        redis_client: Optional Redis client used to resolve per-tenant
            overrides of ``ocr_confidence_threshold``. When ``None`` or
            unreachable, the default threshold is used.
        region: AWS region for the lazily-constructed Textract client.
            Ignored when ``textract_client`` is injected.
        default_confidence_threshold: Platform-wide default threshold used
            when no tenant override is configured (Requirement 4.2.3).
        timeout_seconds: Hard per-call timeout applied to the Textract RPC.
            Defaults to 15s to match the POD flow's OCR budget.
    """

    INDEX = METER_TICKET_OCR_RESULTS_INDEX

    def __init__(
        self,
        file_storage: Any,
        es_service: Any,
        textract_client: Any = None,
        redis_client: Optional[Any] = None,
        region: str = "us-east-1",
        default_confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        timeout_seconds: float = DEFAULT_TEXTRACT_TIMEOUT_SECONDS,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        if file_storage is None:
            raise ValueError("file_storage is required")
        if es_service is None:
            raise ValueError("es_service is required")
        if not 0.0 <= default_confidence_threshold <= 1.0:
            raise ValueError("default_confidence_threshold must be in [0.0, 1.0]")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._file_storage = file_storage
        self._es = es_service
        self._textract_client = textract_client
        self._redis = redis_client
        self._region = region
        self._default_threshold = float(default_confidence_threshold)
        self._timeout = float(timeout_seconds)
        self._circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else default_circuit_breaker
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(
        self,
        tenant_id: str,
        file_ref: str,
        pod_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> OCRResult:
        """Run OCR on the meter-ticket at ``file_ref`` and persist the result.

        The method never raises on provider errors — every failure path
        returns an :class:`OCRResult` with ``extracted_gallons=None``,
        ``confidence=0.0``, ``requires_manual_review=True``, and an
        ``error_details`` string so the caller can fall back to manual entry
        without wrapping this method in a ``try``/``except``.

        It *does* raise ``PermissionError`` when the tenant does not own
        ``file_ref`` — that check is performed by the injected
        :class:`FileStorageService` before Textract is ever called, and the
        POD endpoint translates it into HTTP 403.

        Args:
            tenant_id: Owning tenant; non-empty.
            file_ref: Tenant-prefixed S3 key of the meter-ticket image.
            pod_id: Optional associated POD id for traceability.
            actor: Optional actor id forwarded to FileStorageService audit
                events (driver user id, dispatcher user id, system id).
        """
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not file_ref:
            raise ValueError("file_ref must be non-empty")

        # 1) Tenant-scoped S3 read. ``FileStorageService.get`` raises
        #    ``PermissionError`` on cross-tenant refs, which we let
        #    propagate so the POD endpoint can map it to HTTP 403.
        image_bytes = await self._load_document_bytes(
            tenant_id=tenant_id, file_ref=file_ref, actor=actor
        )

        # 2) Resolve the tenant's confidence threshold once per call.
        threshold = await self._resolve_confidence_threshold(tenant_id)

        # 3) Call Textract. On any error/timeout, build a failure result
        #    and persist it so the POD flow has a durable record of the
        #    attempt (Requirement 4.2.6).
        now = _utcnow()
        error_details: Optional[str] = None
        extracted_gallons: Optional[float] = None
        confidence: float = 0.0
        raw_text: str = ""
        meter_number: Optional[str] = None
        ticket_number: Optional[str] = None

        try:
            response, extracted_gallons, confidence, raw_text, meter_number, ticket_number = (
                await self._wrapped_call_textract(
                    tenant_id=tenant_id, image_bytes=image_bytes, threshold=threshold
                )
            )
        except CircuitOpenError:
            # Breaker is open for this tenant's Textract — skip the call
            # and emit a failure result so POD submission falls back to
            # manual entry. The wrapper already emitted the
            # ``external_call_rejected`` log event and incremented the
            # ``fuelops_ocr_calls_total{status="circuit_open"}`` counter.
            error_details = "textract_circuit_open"
        except asyncio.TimeoutError:
            error_details = "textract_timeout"
            logger.warning(
                "Textract timed out after %.1fs for tenant=%s file_ref=%s",
                self._timeout,
                tenant_id,
                file_ref,
            )
        except Exception as exc:  # pragma: no cover - caught to emit failure record
            error_details = f"textract_error:{type(exc).__name__}:{exc}"
            logger.warning(
                "Textract call failed for tenant=%s file_ref=%s: %s",
                tenant_id,
                file_ref,
                exc,
            )

        requires_manual_review = (
            extracted_gallons is None
            or error_details is not None
            or confidence < threshold
        )

        result = OCRResult(
            ocr_result_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            pod_id=pod_id,
            file_ref=file_ref,
            meter_number=meter_number,
            ticket_number=ticket_number,
            extracted_gallons=extracted_gallons,
            confidence=confidence,
            raw_text=raw_text,
            requires_manual_review=requires_manual_review,
            provider=PROVIDER_NAME,
            processed_at=now,
            error_details=error_details,
            created_at=now,
            updated_at=now,
        )

        # 4) Persist. A persistence failure must not hide the OCR result
        #    from the caller — we log and return so the POD endpoint can
        #    still consume the in-memory result and fall back to manual.
        try:
            await self._persist(result)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Failed to persist OCR result ocr_result_id=%s tenant=%s: %s",
                result.ocr_result_id,
                tenant_id,
                exc,
            )

        return result

    # ------------------------------------------------------------------
    # Internals: document fetch
    # ------------------------------------------------------------------

    async def _load_document_bytes(
        self, tenant_id: str, file_ref: str, actor: Optional[str]
    ) -> bytes:
        """Fetch the meter-ticket image bytes via the file storage service.

        ``FileStorageService.get`` is typically synchronous, but this method
        awaits it when the caller has injected an async implementation
        (tests occasionally do). This keeps the outer coroutine signature
        consistent regardless of the underlying S3 client style.
        """
        getter = self._file_storage.get
        result = getter(tenant_id, file_ref, actor=actor) if _accepts_actor(getter) \
            else getter(tenant_id, file_ref)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, (bytes, bytearray)):
            raise TypeError(
                f"FileStorageService.get must return bytes, got {type(result).__name__}"
            )
        return bytes(result)

    # ------------------------------------------------------------------
    # Internals: threshold resolution
    # ------------------------------------------------------------------

    async def _resolve_confidence_threshold(self, tenant_id: str) -> float:
        """Return the tenant's configured threshold or the platform default.

        Reads Redis key ``ocr_confidence_threshold:{tenant_id}``. Malformed
        values (non-float, out of [0.0, 1.0]) fall through to the default so
        a bad override never blocks a driver's POD submission.
        """
        if self._redis is None:
            return self._default_threshold

        key = _CONFIDENCE_THRESHOLD_KEY_PATTERN.format(tenant_id=tenant_id)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Redis lookup for ocr_confidence_threshold failed tenant=%s: %s",
                tenant_id,
                exc,
            )
            return self._default_threshold

        if raw is None:
            return self._default_threshold

        try:
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            value = float(text)
        except (TypeError, ValueError, UnicodeDecodeError):
            logger.warning(
                "Malformed ocr_confidence_threshold for tenant=%s value=%r",
                tenant_id,
                raw,
            )
            return self._default_threshold

        if not 0.0 <= value <= 1.0:
            logger.warning(
                "Out-of-range ocr_confidence_threshold for tenant=%s value=%s",
                tenant_id,
                value,
            )
            return self._default_threshold

        return value

    # ------------------------------------------------------------------
    # Internals: Textract call + parsing
    # ------------------------------------------------------------------

    def _textract(self):
        """Lazily construct a boto3 Textract client on first use."""
        if self._textract_client is not None:
            return self._textract_client
        import boto3  # imported lazily so tests can import without AWS creds

        self._textract_client = boto3.client("textract", region_name=self._region)
        return self._textract_client

    async def _wrapped_call_textract(
        self,
        *,
        tenant_id: str,
        image_bytes: bytes,
        threshold: float,
    ) -> Tuple[Dict[str, Any], Optional[float], float, str, Optional[str], Optional[str]]:
        """Run Textract under the structured-log + circuit-breaker wrapper.

        The wrapper emits ``external_call_started`` /
        ``external_call_finished`` (or ``external_call_failed`` /
        ``external_call_rejected``) events with ``tenant_id``,
        ``provider=aws_textract``, ``operation=analyze_document``,
        ``duration_ms``, ``status``, and (on failure) ``error_code``. It
        also increments :data:`fuelops_ocr_calls_total` with the
        matching ``(tenant_id, provider, status)`` labels (Task 12.8 /
        Req 10.3.1).

        The status is ``success`` on a clean extraction,
        ``requires_manual_review`` when Textract returned but the
        parsed confidence is below the tenant's threshold, and
        ``timeout`` / ``error`` on upstream faults. ``circuit_open`` is
        emitted by the wrapper itself when the breaker has already
        tripped — the caller catches :class:`CircuitOpenError` and maps
        it to the canonical ``textract_circuit_open`` error_details so
        the POD flow falls back to manual entry.

        Returns:
            Tuple of (response, extracted_gallons, confidence, raw_text,
            meter_number, ticket_number).
        """

        async with trace_external_call(
            tenant_id=tenant_id,
            provider=PROVIDER_NAME,
            operation="analyze_document",
            circuit_breaker=self._circuit_breaker,
            metric=fuelops_ocr_calls_total,
        ) as call:
            response = await self._call_textract(image_bytes)
            extracted_gallons, confidence, raw_text, meter_number, ticket_number = (
                self._parse_textract_response(response)
            )
            # A low-confidence response is not an upstream error — the
            # call itself succeeded — but the metric surface reserves a
            # dedicated ``requires_manual_review`` status for it
            # (Req 10.3.1 / Task 12.8) so the dashboard can separate
            # network faults from low-confidence extractions.
            if extracted_gallons is None or confidence < threshold:
                call.set_status("requires_manual_review")
            return response, extracted_gallons, confidence, raw_text, meter_number, ticket_number

    async def _call_textract(self, image_bytes: bytes) -> Dict[str, Any]:
        """Invoke ``AnalyzeDocument`` with a hard timeout.

        boto3 clients are synchronous; we wrap the call in
        ``asyncio.to_thread`` so we can enforce ``timeout_seconds`` via
        ``asyncio.wait_for`` without blocking the event loop. Providers
        injected by tests can be synchronous or asynchronous — we adapt.
        """
        client = self._textract()

        def _call() -> Dict[str, Any]:
            return client.analyze_document(
                Document={"Bytes": image_bytes},
                FeatureTypes=list(_TEXTRACT_FEATURES),
            )

        response = await asyncio.wait_for(
            asyncio.to_thread(_call), timeout=self._timeout
        )
        if not isinstance(response, dict):
            raise TypeError(
                f"Textract response must be a dict, got {type(response).__name__}"
            )
        return response

    @staticmethod
    def _parse_textract_response(
        response: Dict[str, Any],
    ) -> Tuple[Optional[float], float, str, Optional[str], Optional[str]]:
        """Pull ``(extracted_gallons, confidence, raw_text, meter_number, ticket_number)`` from a response.

        * ``extracted_gallons`` — the first positive numeric value on a KV
          pair whose key contains one of ``_GALLON_KEY_TOKENS`` (preferring
          ``GALLONS`` over ``GAL`` over ``GROSS``).
        * ``confidence`` — the average of ``Confidence`` across every block
          returned, rescaled from Textract's [0, 100] range into [0.0, 1.0].
          Returns ``0.0`` when no blocks were returned.
        * ``raw_text`` — the concatenation of every ``LINE`` block's text
          joined by newlines. Used for dispatcher review on manual fallback.
        * ``meter_number`` — the meter serial/identification number extracted
          from a KV pair whose key matches ``_METER_NUMBER_KEY_TOKENS``.
          ``None`` when not found. (Requirement 8.1)
        * ``ticket_number`` — the sequential ticket number extracted from a
          KV pair whose key matches ``_TICKET_NUMBER_KEY_TOKENS``. ``None``
          when not found. (Requirement 8.1)
        """
        blocks = response.get("Blocks") or []
        if not isinstance(blocks, list):
            blocks = []

        # Index blocks by id for KV pair resolution.
        by_id: Dict[str, Dict[str, Any]] = {}
        for block in blocks:
            block_id = block.get("Id")
            if block_id:
                by_id[block_id] = block

        # Compute mean confidence across every block (Req 4.2.2). Textract
        # reports 0-100; Requirement 4.2.1 specifies a [0.0, 1.0] range.
        confidences: List[float] = []
        raw_text_lines: List[str] = []
        kv_keys: List[Dict[str, Any]] = []

        for block in blocks:
            conf = block.get("Confidence")
            if isinstance(conf, (int, float)) and 0 <= float(conf) <= 100:
                confidences.append(float(conf))

            block_type = block.get("BlockType")
            if block_type == "LINE":
                text = block.get("Text")
                if isinstance(text, str) and text:
                    raw_text_lines.append(text)

            # Textract FORMS emits KEY_VALUE_SET blocks with an
            # ``EntityTypes`` list containing ``KEY`` or ``VALUE``.
            if block_type == "KEY_VALUE_SET":
                entity_types = block.get("EntityTypes") or []
                if "KEY" in entity_types:
                    kv_keys.append(block)

        mean_conf_0_100 = (sum(confidences) / len(confidences)) if confidences else 0.0
        confidence = max(0.0, min(1.0, mean_conf_0_100 / 100.0))
        raw_text = "\n".join(raw_text_lines)

        # Resolve each KV key's linked value, text, and match against the
        # gallon key tokens. Preserve the preference order in
        # ``_GALLON_KEY_TOKENS`` by scanning it token-by-token.
        resolved_pairs: List[Tuple[str, str]] = []
        for key_block in kv_keys:
            key_text = _resolve_block_text(key_block, by_id)
            value_text = _resolve_linked_value_text(key_block, by_id)
            if key_text is None or value_text is None:
                continue
            resolved_pairs.append((key_text, value_text))

        extracted_gallons = _pick_gallons(resolved_pairs)
        meter_number = _pick_identifier(resolved_pairs, _METER_NUMBER_KEY_TOKENS)
        ticket_number = _pick_identifier(resolved_pairs, _TICKET_NUMBER_KEY_TOKENS)
        return extracted_gallons, confidence, raw_text, meter_number, ticket_number

    # ------------------------------------------------------------------
    # Internals: persistence
    # ------------------------------------------------------------------

    async def _persist(self, result: OCRResult) -> None:
        """Index ``result`` into the ``meter_ticket_ocr_results`` ES index."""
        document = result.model_dump(mode="json")
        await self._es.index_document(self.INDEX, result.ocr_result_id, document)
        logger.info(
            "Persisted OCR result ocr_result_id=%s tenant=%s pod=%s gallons=%s "
            "confidence=%.3f manual_review=%s",
            result.ocr_result_id,
            result.tenant_id,
            result.pod_id,
            result.extracted_gallons,
            result.confidence,
            result.requires_manual_review,
        )


# ---------------------------------------------------------------------------
# Module-private helpers (kept outside the class so they're easy to unit test
# in isolation without instantiating the full service).
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _accepts_actor(fn: Any) -> bool:
    """Return True if ``fn`` accepts an ``actor`` keyword argument.

    ``FileStorageService.get`` accepts ``actor`` for audit logging, but unit
    tests sometimes inject a minimal mock that only takes
    ``(tenant_id, file_ref)``. We detect this once per call so both shapes
    work without a try/except around the hot path.
    """
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "actor" in sig.parameters


def _normalize_key(text: str) -> str:
    """Return an uppercase, punctuation-free form for token matching."""
    return re.sub(r"[^A-Z0-9 ]+", " ", text.upper()).strip()


def _resolve_block_text(block: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Return the plain text of a KEY or VALUE block.

    Textract's ``KEY_VALUE_SET`` blocks do not carry text directly; they have
    ``Relationships`` of type ``CHILD`` pointing at the ``WORD`` blocks that
    make up the actual text. We concatenate those WORD block ``Text`` fields
    in id order.
    """
    words: List[str] = []
    for rel in block.get("Relationships") or []:
        if rel.get("Type") != "CHILD":
            continue
        for child_id in rel.get("Ids") or []:
            child = by_id.get(child_id)
            if not child:
                continue
            if child.get("BlockType") == "WORD":
                txt = child.get("Text")
                if isinstance(txt, str) and txt:
                    words.append(txt)
            elif child.get("BlockType") == "SELECTION_ELEMENT":
                # Checkbox / radio — irrelevant for gallon extraction.
                continue
    if not words:
        return None
    return " ".join(words).strip() or None


def _resolve_linked_value_text(
    key_block: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    """Follow a KEY block's ``VALUE`` relationship and return the value text."""
    for rel in key_block.get("Relationships") or []:
        if rel.get("Type") != "VALUE":
            continue
        for value_id in rel.get("Ids") or []:
            value_block = by_id.get(value_id)
            if not value_block:
                continue
            text = _resolve_block_text(value_block, by_id)
            if text:
                return text
    return None


def _pick_gallons(pairs: Iterable[Tuple[str, str]]) -> Optional[float]:
    """Select the best ``extracted_gallons`` from resolved KV pairs.

    The scan is token-preference ordered (``GALLONS`` → ``GAL`` → ``GROSS``):
    we first look for a pair whose key contains the more specific token and
    fall back to the next token only if none matched. Within a token, the
    first parseable positive value wins. This matches the design-doc
    directive while tolerating the wide variation in how US meter tickets
    label the gallon field.
    """
    pair_list = [(str(k), str(v)) for k, v in pairs]
    for token in _GALLON_KEY_TOKENS:
        for key_text, value_text in pair_list:
            if token not in _normalize_key(key_text):
                continue
            parsed = _parse_gallon_value(value_text)
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _parse_gallon_value(text: str) -> Optional[float]:
    """Return the first positive float found in ``text``.

    Accepts values like ``"1,234.56"``, ``"780"``, ``"GAL 312.5"`` and
    ``"312.50 GAL"``. Returns ``None`` when no numeric substring parses or
    when the parsed value is <= 0 (a negative or zero gallon reading is
    never a valid delivery).
    """
    if not isinstance(text, str) or not text:
        return None
    match = _NUMERIC_RE.search(text)
    if not match:
        return None
    whole = match.group(1).replace(",", "")
    fraction = match.group(2)
    try:
        if fraction:
            value = float(f"{whole}.{fraction}")
        else:
            value = float(whole)
    except ValueError:
        return None
    return value if value > 0 else None


def _pick_identifier(
    pairs: Iterable[Tuple[str, str]], key_tokens: Tuple[str, ...]
) -> Optional[str]:
    """Select the best identifier value from resolved KV pairs for the given key tokens.

    Scans the KV pairs in token-preference order (more specific tokens first).
    For each matching key, extracts the first alphanumeric identifier from the
    value text. Returns ``None`` when no matching key is found or when the
    value does not contain a parseable identifier.

    Used for extracting ``meter_number`` and ``ticket_number`` from US meter
    tickets (Requirement 8.1).
    """
    pair_list = [(str(k), str(v)) for k, v in pairs]
    for token in key_tokens:
        for key_text, value_text in pair_list:
            normalized_key = _normalize_key(key_text)
            if token not in normalized_key:
                continue
            parsed = _parse_identifier_value(value_text)
            if parsed is not None:
                return parsed
    return None


def _parse_identifier_value(text: str) -> Optional[str]:
    """Extract an alphanumeric identifier from free-text.

    Handles values like ``"M-12345"``, ``"SN 98765"``, ``"12345678"``,
    ``"MT-2024-001"``. Returns the longest matching identifier substring,
    or ``None`` when no valid identifier is found.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    # Find all identifier-like substrings and return the longest one
    # (most likely to be the actual serial/ticket number rather than a
    # short prefix or label fragment).
    matches = _IDENTIFIER_RE.findall(text.strip())
    if not matches:
        return None
    # Return the longest match — on US meter tickets the identifier is
    # typically the longest token in the value cell.
    return max(matches, key=len)


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_TEXTRACT_TIMEOUT_SECONDS",
    "MeterTicketOCRService",
    "OCRResult",
    "PROVIDER_NAME",
]
