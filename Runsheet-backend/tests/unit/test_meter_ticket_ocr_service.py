"""
Unit tests for MeterTicketOCRService — AWS Textract-backed gallon extraction.

Validates Requirement 4.2.1 (``extract`` method contract + ``OCRResult``
fields), Requirement 4.2.2 (Textract FORMS + persistence to
``meter_ticket_ocr_results``), and Requirement 4.2.3 (tenant-configurable
``ocr_confidence_threshold`` driving ``requires_manual_review``). boto3
Textract and the Elasticsearch service are mocked so no AWS or ES calls are
made.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from fuel.services.fuel_ops_es_mappings import METER_TICKET_OCR_RESULTS_INDEX
from services.meter_ticket_ocr_service import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_TEXTRACT_TIMEOUT_SECONDS,
    MeterTicketOCRService,
    OCRResult,
    PROVIDER_NAME,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeFileStorage:
    """Minimal stand-in for ``FileStorageService``.

    Records the ``(tenant_id, file_ref)`` tuples the service asks for and
    returns canned bytes per file_ref. Cross-tenant accesses raise
    ``PermissionError`` to mirror the real service's contract so the OCR
    service's tenant-scoping can be asserted end-to-end.
    """

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        # Map file_ref -> tenant that "owns" it. The fake mimics the real
        # tenant-prefix check without re-implementing the key regex.
        self.owners: Dict[str, str] = {}
        self.get_calls: List[Dict[str, Any]] = []

    def put(self, tenant_id: str, file_ref: str, data: bytes) -> None:
        self.objects[file_ref] = data
        self.owners[file_ref] = tenant_id

    def get(self, tenant_id: str, file_ref: str, actor: Optional[str] = None) -> bytes:
        self.get_calls.append(
            {"tenant_id": tenant_id, "file_ref": file_ref, "actor": actor}
        )
        owner = self.owners.get(file_ref)
        if owner is None:
            raise KeyError(file_ref)
        if owner != tenant_id:
            raise PermissionError("cross_tenant_file_ref")
        return self.objects[file_ref]


class _FakeES:
    """In-memory ES service exposing ``index_document``."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.index_calls: List[Dict[str, Any]] = []

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]):
        assert index == METER_TICKET_OCR_RESULTS_INDEX, (
            f"unexpected index: {index}"
        )
        self.index_calls.append({"index": index, "id": doc_id, "doc": dict(document)})
        self.docs[doc_id] = dict(document)
        return {"result": "created"}


class _FakeTextract:
    """Synchronous stand-in for ``boto3.client('textract')``.

    Tests set ``next_response`` or ``raise_on_call`` to control behavior.
    Every ``analyze_document`` call is recorded so assertions can verify
    that ``FeatureTypes=["FORMS"]`` was requested (Req 4.2.2).
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.next_response: Optional[Dict[str, Any]] = None
        self.raise_on_call: Optional[Exception] = None

    def analyze_document(self, *, Document: Dict[str, Any], FeatureTypes: List[str]):
        self.calls.append({"Document": Document, "FeatureTypes": list(FeatureTypes)})
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if self.next_response is None:
            return {"Blocks": []}
        return self.next_response


class _FakeRedis:
    """Minimal async Redis stub supporting ``get`` with bytes/str returns."""

    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        self._values = dict(values or {})
        self.get_calls: List[str] = []

    async def get(self, key: str):
        self.get_calls.append(key)
        return self._values.get(key)


# ---------------------------------------------------------------------------
# Textract response builders
# ---------------------------------------------------------------------------


def _line_block(block_id: str, text: str, confidence: float) -> Dict[str, Any]:
    return {
        "Id": block_id,
        "BlockType": "LINE",
        "Text": text,
        "Confidence": confidence,
    }


def _word_block(block_id: str, text: str, confidence: float = 99.0) -> Dict[str, Any]:
    return {
        "Id": block_id,
        "BlockType": "WORD",
        "Text": text,
        "Confidence": confidence,
    }


def _kv_key(block_id: str, word_ids: List[str], value_id: str, confidence: float) -> Dict[str, Any]:
    return {
        "Id": block_id,
        "BlockType": "KEY_VALUE_SET",
        "EntityTypes": ["KEY"],
        "Confidence": confidence,
        "Relationships": [
            {"Type": "CHILD", "Ids": list(word_ids)},
            {"Type": "VALUE", "Ids": [value_id]},
        ],
    }


def _kv_value(block_id: str, word_ids: List[str], confidence: float) -> Dict[str, Any]:
    return {
        "Id": block_id,
        "BlockType": "KEY_VALUE_SET",
        "EntityTypes": ["VALUE"],
        "Confidence": confidence,
        "Relationships": [{"Type": "CHILD", "Ids": list(word_ids)}],
    }


def _build_kv_pair(
    key_text: str,
    value_text: str,
    confidence: float = 99.0,
    prefix: str = "",
) -> List[Dict[str, Any]]:
    """Return the Textract blocks representing a single KEY=VALUE form pair."""
    key_words = key_text.split()
    value_words = value_text.split()
    blocks: List[Dict[str, Any]] = []
    key_word_ids: List[str] = []
    for i, word in enumerate(key_words):
        word_id = f"{prefix}kw{i}_{word}"
        blocks.append(_word_block(word_id, word, confidence))
        key_word_ids.append(word_id)
    value_word_ids: List[str] = []
    for i, word in enumerate(value_words):
        word_id = f"{prefix}vw{i}_{word}"
        blocks.append(_word_block(word_id, word, confidence))
        value_word_ids.append(word_id)
    key_id = f"{prefix}key_{key_text.replace(' ', '_')}"
    value_id = f"{prefix}value_{value_text.replace(' ', '_')}"
    blocks.append(_kv_key(key_id, key_word_ids, value_id, confidence))
    blocks.append(_kv_value(value_id, value_word_ids, confidence))
    return blocks


def _build_response(
    pairs: List[tuple[str, str]],
    lines: Optional[List[str]] = None,
    confidence: float = 99.0,
) -> Dict[str, Any]:
    blocks: List[Dict[str, Any]] = []
    for idx, (k, v) in enumerate(pairs):
        blocks.extend(_build_kv_pair(k, v, confidence=confidence, prefix=f"p{idx}_"))
    for idx, line in enumerate(lines or []):
        blocks.append(_line_block(f"line{idx}", line, confidence))
    return {"Blocks": blocks}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def file_storage() -> _FakeFileStorage:
    fs = _FakeFileStorage()
    # Seed one sample meter-ticket image owned by tenant-A.
    fs.put("tenant-A", "tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg", b"JPEGBYTES")
    # A second image owned by tenant-B for cross-tenant isolation tests.
    fs.put("tenant-B", "tenants/tenant-B/meter_ticket/2024/01/01/xyz.jpg", b"JPEGBYTES")
    return fs


@pytest.fixture
def es() -> _FakeES:
    return _FakeES()


@pytest.fixture
def textract() -> _FakeTextract:
    return _FakeTextract()


@pytest.fixture
def redis_client() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def service(
    file_storage: _FakeFileStorage,
    es: _FakeES,
    textract: _FakeTextract,
    redis_client: _FakeRedis,
) -> MeterTicketOCRService:
    return MeterTicketOCRService(
        file_storage=file_storage,
        es_service=es,
        textract_client=textract,
        redis_client=redis_client,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_missing_file_storage(self, es: _FakeES):
        with pytest.raises(ValueError):
            MeterTicketOCRService(file_storage=None, es_service=es)

    def test_rejects_missing_es_service(self, file_storage: _FakeFileStorage):
        with pytest.raises(ValueError):
            MeterTicketOCRService(file_storage=file_storage, es_service=None)

    def test_rejects_invalid_default_threshold(
        self, file_storage: _FakeFileStorage, es: _FakeES
    ):
        with pytest.raises(ValueError):
            MeterTicketOCRService(
                file_storage=file_storage,
                es_service=es,
                default_confidence_threshold=1.5,
            )
        with pytest.raises(ValueError):
            MeterTicketOCRService(
                file_storage=file_storage,
                es_service=es,
                default_confidence_threshold=-0.1,
            )

    def test_rejects_non_positive_timeout(
        self, file_storage: _FakeFileStorage, es: _FakeES
    ):
        with pytest.raises(ValueError):
            MeterTicketOCRService(
                file_storage=file_storage, es_service=es, timeout_seconds=0
            )

    def test_defaults_match_spec(
        self, file_storage: _FakeFileStorage, es: _FakeES
    ):
        svc = MeterTicketOCRService(file_storage=file_storage, es_service=es)
        assert DEFAULT_CONFIDENCE_THRESHOLD == 0.85
        assert DEFAULT_TEXTRACT_TIMEOUT_SECONDS == 15.0
        # The service instance honors the defaults (white-box via
        # threshold resolution with no Redis override configured).
        # Cheap sanity check without poking private state.
        assert svc is not None


# ---------------------------------------------------------------------------
# extract — happy path
# ---------------------------------------------------------------------------


class TestExtractHappyPath:
    @pytest.mark.asyncio
    async def test_extracts_gallons_from_forms_kv_pair(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
        es: _FakeES,
    ):
        textract.next_response = _build_response(
            pairs=[("GALLONS", "780")],
            lines=["METER TICKET", "DELIVERY 12345", "GALLONS 780"],
        )
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
            pod_id="pod-1",
        )
        assert isinstance(result, OCRResult)
        assert result.extracted_gallons == 780.0
        # Average confidence: every block is 99 → 99/100 = 0.99
        assert 0.98 <= result.confidence <= 1.0
        assert result.requires_manual_review is False
        assert result.provider == PROVIDER_NAME
        assert result.tenant_id == "tenant-A"
        assert result.pod_id == "pod-1"
        assert "METER TICKET" in result.raw_text
        assert result.error_details is None
        # Persisted with matching id.
        assert len(es.index_calls) == 1
        assert es.index_calls[0]["index"] == METER_TICKET_OCR_RESULTS_INDEX
        assert es.index_calls[0]["id"] == result.ocr_result_id
        # Timestamps are timezone-aware UTC.
        assert result.processed_at.tzinfo is not None
        assert result.created_at == result.processed_at
        assert result.updated_at == result.processed_at

    @pytest.mark.asyncio
    async def test_requests_forms_feature_against_bytes(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        textract.next_response = _build_response([("GALLONS", "100")])
        await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert len(textract.calls) == 1
        call = textract.calls[0]
        # Req 4.2.2 — FORMS feature.
        assert call["FeatureTypes"] == ["FORMS"]
        # Document bytes sourced from the file storage service.
        assert call["Document"] == {"Bytes": b"JPEGBYTES"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value_text,expected",
        [
            ("780", 780.0),
            ("1,234.56", 1234.56),
            ("312.5 GAL", 312.5),
            ("GROSS 500.25", 500.25),
        ],
    )
    async def test_parses_varied_numeric_formats(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
        value_text: str,
        expected: float,
    ):
        textract.next_response = _build_response([("GAL", value_text)])
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons == expected

    @pytest.mark.asyncio
    async def test_prefers_gallons_over_gal_over_gross(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        # GALLONS is the most specific key — should win even when a GROSS
        # pair is earlier in the response.
        textract.next_response = _build_response(
            pairs=[
                ("GROSS", "999"),
                ("NET GAL", "700"),
                ("GALLONS", "1000"),
            ]
        )
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons == 1000.0

    @pytest.mark.asyncio
    async def test_matches_gross_gal_when_no_plain_gallons_key(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        # Many US tickets label the field "GROSS GAL" — the GAL token must
        # still match.
        textract.next_response = _build_response(
            pairs=[("GROSS GAL", "432.1")]
        )
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons == 432.1


# ---------------------------------------------------------------------------
# extract — confidence threshold + manual review
# ---------------------------------------------------------------------------


class TestConfidenceAndManualReview:
    @pytest.mark.asyncio
    async def test_default_threshold_flags_low_confidence(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        # Every block has confidence 70 → mean 0.70 < 0.85 default.
        textract.next_response = _build_response(
            pairs=[("GALLONS", "780")], confidence=70.0
        )
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons == 780.0
        assert abs(result.confidence - 0.70) < 1e-9
        assert result.requires_manual_review is True

    @pytest.mark.asyncio
    async def test_high_confidence_does_not_require_review(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        textract.next_response = _build_response(
            pairs=[("GALLONS", "780")], confidence=95.0
        )
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.requires_manual_review is False

    @pytest.mark.asyncio
    async def test_tenant_override_raises_threshold(
        self,
        file_storage: _FakeFileStorage,
        es: _FakeES,
        textract: _FakeTextract,
    ):
        # Tenant-A requires 95% confidence; default would accept 90%.
        redis = _FakeRedis({"ocr_confidence_threshold:tenant-A": b"0.95"})
        svc = MeterTicketOCRService(
            file_storage=file_storage,
            es_service=es,
            textract_client=textract,
            redis_client=redis,
        )
        textract.next_response = _build_response(
            pairs=[("GALLONS", "780")], confidence=90.0
        )
        result = await svc.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert 0.89 <= result.confidence <= 0.91
        assert result.requires_manual_review is True
        # Redis was consulted.
        assert "ocr_confidence_threshold:tenant-A" in redis.get_calls

    @pytest.mark.asyncio
    async def test_tenant_override_lowers_threshold(
        self,
        file_storage: _FakeFileStorage,
        es: _FakeES,
        textract: _FakeTextract,
    ):
        # Tenant-A lowers the bar to 60%.
        redis = _FakeRedis({"ocr_confidence_threshold:tenant-A": "0.60"})
        svc = MeterTicketOCRService(
            file_storage=file_storage,
            es_service=es,
            textract_client=textract,
            redis_client=redis,
        )
        textract.next_response = _build_response(
            pairs=[("GALLONS", "780")], confidence=70.0
        )
        result = await svc.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.requires_manual_review is False

    @pytest.mark.asyncio
    async def test_malformed_override_falls_back_to_default(
        self,
        file_storage: _FakeFileStorage,
        es: _FakeES,
        textract: _FakeTextract,
    ):
        redis = _FakeRedis({"ocr_confidence_threshold:tenant-A": "not-a-float"})
        svc = MeterTicketOCRService(
            file_storage=file_storage,
            es_service=es,
            textract_client=textract,
            redis_client=redis,
        )
        textract.next_response = _build_response(
            pairs=[("GALLONS", "780")], confidence=90.0
        )
        result = await svc.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        # With default 0.85 threshold, confidence 0.90 clears it.
        assert result.requires_manual_review is False

    @pytest.mark.asyncio
    async def test_out_of_range_override_falls_back_to_default(
        self,
        file_storage: _FakeFileStorage,
        es: _FakeES,
        textract: _FakeTextract,
    ):
        redis = _FakeRedis({"ocr_confidence_threshold:tenant-A": "1.5"})
        svc = MeterTicketOCRService(
            file_storage=file_storage,
            es_service=es,
            textract_client=textract,
            redis_client=redis,
        )
        textract.next_response = _build_response(
            pairs=[("GALLONS", "780")], confidence=90.0
        )
        result = await svc.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.requires_manual_review is False

    @pytest.mark.asyncio
    async def test_no_redis_uses_default_threshold(
        self,
        file_storage: _FakeFileStorage,
        es: _FakeES,
        textract: _FakeTextract,
    ):
        svc = MeterTicketOCRService(
            file_storage=file_storage,
            es_service=es,
            textract_client=textract,
            redis_client=None,
        )
        textract.next_response = _build_response(
            pairs=[("GALLONS", "780")], confidence=80.0
        )
        result = await svc.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert abs(result.confidence - 0.80) < 1e-9
        assert result.requires_manual_review is True  # 0.80 < default 0.85


# ---------------------------------------------------------------------------
# extract — parse failures + provider errors
# ---------------------------------------------------------------------------


class TestParseAndProviderFailures:
    @pytest.mark.asyncio
    async def test_no_gallon_kv_returns_none_and_flags_review(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        textract.next_response = _build_response(
            pairs=[("DRIVER", "JANE DOE"), ("TRUCK", "T-42")],
            lines=["METER TICKET"],
        )
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons is None
        assert result.requires_manual_review is True

    @pytest.mark.asyncio
    async def test_non_numeric_value_returns_none(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        textract.next_response = _build_response(
            pairs=[("GALLONS", "N/A")]
        )
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons is None
        assert result.requires_manual_review is True

    @pytest.mark.asyncio
    async def test_zero_gallons_rejected_as_invalid(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        textract.next_response = _build_response(
            pairs=[("GALLONS", "0")]
        )
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons is None
        assert result.requires_manual_review is True

    @pytest.mark.asyncio
    async def test_textract_error_produces_failure_record(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
        es: _FakeES,
    ):
        textract.raise_on_call = RuntimeError("throttled")
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
            pod_id="pod-err",
        )
        assert result.extracted_gallons is None
        assert result.confidence == 0.0
        assert result.requires_manual_review is True
        assert result.error_details is not None
        assert "throttled" in result.error_details
        # The failure record is still persisted for audit (Req 4.2.2).
        assert len(es.index_calls) == 1
        assert es.index_calls[0]["doc"]["requires_manual_review"] is True
        assert es.index_calls[0]["doc"]["extracted_gallons"] is None

    @pytest.mark.asyncio
    async def test_textract_timeout_produces_failure_record(
        self,
        file_storage: _FakeFileStorage,
        es: _FakeES,
    ):
        import asyncio as _asyncio

        class _SlowTextract:
            def analyze_document(self, **kwargs):  # noqa: D401
                # Block long enough to exceed the 0.05s timeout below.
                import time

                time.sleep(0.2)
                return {"Blocks": []}

        svc = MeterTicketOCRService(
            file_storage=file_storage,
            es_service=es,
            textract_client=_SlowTextract(),
            timeout_seconds=0.05,
        )
        result = await svc.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons is None
        assert result.error_details == "textract_timeout"
        assert result.requires_manual_review is True


# ---------------------------------------------------------------------------
# extract — tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_cross_tenant_ref_raises_permission_error(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        # Tenant-A requesting tenant-B's ref must be rejected by the
        # file storage service before Textract is ever called.
        with pytest.raises(PermissionError):
            await service.extract(
                tenant_id="tenant-A",
                file_ref="tenants/tenant-B/meter_ticket/2024/01/01/xyz.jpg",
            )
        assert textract.calls == []

    @pytest.mark.asyncio
    async def test_empty_tenant_id_rejected(
        self,
        service: MeterTicketOCRService,
    ):
        with pytest.raises(ValueError):
            await service.extract(
                tenant_id="",
                file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
            )

    @pytest.mark.asyncio
    async def test_empty_file_ref_rejected(
        self,
        service: MeterTicketOCRService,
    ):
        with pytest.raises(ValueError):
            await service.extract(tenant_id="tenant-A", file_ref="")


# ---------------------------------------------------------------------------
# extract — persistence shape
# ---------------------------------------------------------------------------


class TestPersistenceShape:
    @pytest.mark.asyncio
    async def test_persisted_document_matches_mapping_fields(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
        es: _FakeES,
    ):
        textract.next_response = _build_response([("GALLONS", "500")])
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
            pod_id="pod-123",
        )
        doc = es.index_calls[0]["doc"]
        expected_fields = {
            "ocr_result_id",
            "tenant_id",
            "pod_id",
            "file_ref",
            "extracted_gallons",
            "confidence",
            "raw_text",
            "requires_manual_review",
            "provider",
            "processed_at",
            "error_details",
            "created_at",
            "updated_at",
        }
        assert set(doc.keys()) == expected_fields
        assert doc["ocr_result_id"] == result.ocr_result_id
        assert doc["tenant_id"] == "tenant-A"
        assert doc["pod_id"] == "pod-123"
        assert doc["file_ref"] == "tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg"
        assert doc["extracted_gallons"] == 500.0
        assert doc["provider"] == PROVIDER_NAME
        # ISO-8601 strings ready for ES ``date`` field.
        assert isinstance(doc["processed_at"], str)
        assert isinstance(doc["created_at"], str)
        assert isinstance(doc["updated_at"], str)

    @pytest.mark.asyncio
    async def test_persistence_failure_does_not_mask_result(
        self,
        file_storage: _FakeFileStorage,
        textract: _FakeTextract,
    ):
        class _BrokenES:
            async def index_document(self, *args, **kwargs):
                raise RuntimeError("es down")

        svc = MeterTicketOCRService(
            file_storage=file_storage,
            es_service=_BrokenES(),
            textract_client=textract,
        )
        textract.next_response = _build_response([("GALLONS", "780")])
        # Caller still receives a usable result even when persistence fails.
        result = await svc.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.extracted_gallons == 780.0


# ---------------------------------------------------------------------------
# Confidence aggregation edge cases
# ---------------------------------------------------------------------------


class TestConfidenceAggregation:
    @pytest.mark.asyncio
    async def test_empty_blocks_yields_zero_confidence(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        textract.next_response = {"Blocks": []}
        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        assert result.confidence == 0.0
        assert result.requires_manual_review is True

    @pytest.mark.asyncio
    async def test_mixed_confidences_average_into_0_to_1_range(
        self,
        service: MeterTicketOCRService,
        textract: _FakeTextract,
    ):
        # Build response with two pairs at confidence 80 and 100.
        blocks = _build_kv_pair("GALLONS", "100", confidence=80.0, prefix="a_")
        blocks.extend(_build_kv_pair("DRIVER", "JANE", confidence=100.0, prefix="b_"))
        textract.next_response = {"Blocks": blocks}

        result = await service.extract(
            tenant_id="tenant-A",
            file_ref="tenants/tenant-A/meter_ticket/2024/01/01/abc.jpg",
        )
        # Every block shares the same pair confidence so the mean sits
        # between 0.80 and 1.00 in the [0, 1] range. We assert the
        # bounds rather than an exact value because the pair builder
        # creates multiple word blocks per KV at matching confidences.
        assert 0.80 <= result.confidence <= 1.00
