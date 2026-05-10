"""Unit tests for TerminalBOLIngestionService.

Tests cover:
- ingest_edi: valid X12 856 payload, valid pipe-delimited payload
- ingest_edi: EDI parse error propagation
- ingest_edi: model validation failure (e.g., missing required field)
- ingest_edi: driver_id validation against DriverQualificationService
- ingest_manual: creates BOL with pending_confirmation status
- ingest_manual: invalid content_type is rejected
- ingest_manual: stores raw document via FileStorageService
- confirm_manual_bol: updates BOL with confirmed fields
- confirm_manual_bol: transitions status to ingested
- confirm_manual_bol: validates driver_id on confirmation
- Skeleton methods raise NotImplementedError

Validates: Requirement 10.1, 10.2, 10.3
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from compliance.models.terminal_bol import TerminalBOL
from compliance.services.terminal_bol_edi_parser import (
    EDIParseError,
    EDIParserRegistry,
    create_default_registry,
)
from compliance.services.terminal_bol_ingestion_service import (
    TerminalBOLIngestionService,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_fuel_co"


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es.update_document = AsyncMock(return_value=None)
    return es


def _make_valid_x12_payload() -> bytes:
    """A valid X12 856 EDI payload with all required BOL fields."""
    segments = [
        "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *240115*1030*U*00401*000000001*0*P*>",
        "GS*SH*SENDER*RECEIVER*20240115*1030*1*X*004010",
        "ST*856*0001",
        "BSN*00*0001*20240115*1030",
        "HL*1**S*1",
        "TD1*CTN*1*UNL87",
        "REF*LN*LOAD-2024-001",
        "REF*DR*DRV-100",
        "QTY*GR*8500.0",
        "QTY*NT*8450.5",
        "MEA*TM*TE*72.5",
        "MEA*PD*AG*58.2",
        "N1*SU*Marathon Petroleum",
        "N1*TL*Pasadena Terminal",
        "DTM*011*202401151030",
        "SE*13*0001",
        "GE*1*1",
        "IEA*1*000000001",
    ]
    return "~".join(segments).encode("utf-8") + b"~"


def _make_valid_pipe_payload() -> bytes:
    """A valid pipe-delimited payload with all required BOL fields."""
    header = "load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp"
    data = "LOAD-2024-002|ULSD|9200.0|9150.3|68.0|35.5|Valero Energy|Houston Terminal|DRV-200|2024-01-15T10:30:00"
    return f"{header}\n{data}\n".encode("utf-8")


@pytest.fixture
def es_service() -> AsyncMock:
    """Mocked ES service."""
    return _make_es_service()


@pytest.fixture
def registry() -> EDIParserRegistry:
    """Pre-configured registry with both strategies."""
    return create_default_registry()


@pytest.fixture
def service(es_service, registry) -> TerminalBOLIngestionService:
    """TerminalBOLIngestionService with mocked dependencies."""
    return TerminalBOLIngestionService(
        es_service=es_service,
        edi_parser_registry=registry,
    )


# ---------------------------------------------------------------------------
# Tests: ingest_edi — valid payloads
# ---------------------------------------------------------------------------


class TestIngestEdiValid:
    """Tests for ingest_edi with valid payloads."""

    @pytest.mark.asyncio
    async def test_ingest_x12_payload_returns_terminal_bol(self, service, es_service):
        """ingest_edi with a valid X12 856 payload returns a TerminalBOL."""
        payload = _make_valid_x12_payload()

        result = await service.ingest_edi(payload, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.tenant_id == _TENANT_ID
        assert result.load_number == "LOAD-2024-001"
        assert result.product_code == "UNL87"
        assert result.gross_gallons == 8500.0
        assert result.net_gallons == 8450.5
        assert result.observed_temperature_f == 72.5
        assert result.api_gravity == 58.2
        assert result.supplier_name == "Marathon Petroleum"
        assert result.terminal_name == "Pasadena Terminal"
        assert result.driver_id == "DRV-100"
        assert result.timestamp is not None
        assert result.status == "ingested"
        assert result.bol_id.startswith("bol_")

    @pytest.mark.asyncio
    async def test_ingest_x12_payload_persists_to_es(self, service, es_service):
        """ingest_edi persists the BOL document to the terminal_bols index."""
        payload = _make_valid_x12_payload()

        result = await service.ingest_edi(payload, _TENANT_ID)

        es_service.index_document.assert_called_once()
        call_args = es_service.index_document.call_args
        assert call_args[0][0] == "terminal_bols"
        assert call_args[0][1] == result.bol_id
        doc = call_args[0][2]
        assert doc["tenant_id"] == _TENANT_ID
        assert doc["load_number"] == "LOAD-2024-001"

    @pytest.mark.asyncio
    async def test_ingest_pipe_payload_returns_terminal_bol(self, service, es_service):
        """ingest_edi with a valid pipe-delimited payload returns a TerminalBOL."""
        payload = _make_valid_pipe_payload()

        result = await service.ingest_edi(payload, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.tenant_id == _TENANT_ID
        assert result.load_number == "LOAD-2024-002"
        assert result.product_code == "ULSD"
        assert result.gross_gallons == 9200.0
        assert result.net_gallons == 9150.3
        assert result.observed_temperature_f == 68.0
        assert result.api_gravity == 35.5
        assert result.supplier_name == "Valero Energy"
        assert result.terminal_name == "Houston Terminal"
        assert result.driver_id == "DRV-200"
        assert result.timestamp is not None
        assert result.status == "ingested"

    @pytest.mark.asyncio
    async def test_ingest_pipe_payload_persists_to_es(self, service, es_service):
        """ingest_edi persists pipe-delimited BOL to ES."""
        payload = _make_valid_pipe_payload()

        result = await service.ingest_edi(payload, _TENANT_ID)

        es_service.index_document.assert_called_once()
        call_args = es_service.index_document.call_args
        assert call_args[0][0] == "terminal_bols"
        assert call_args[0][1] == result.bol_id


# ---------------------------------------------------------------------------
# Tests: ingest_edi — error cases
# ---------------------------------------------------------------------------


class TestIngestEdiErrors:
    """Tests for ingest_edi error handling."""

    @pytest.mark.asyncio
    async def test_ingest_invalid_payload_raises_edi_parse_error(self, service):
        """ingest_edi raises EDIParseError for unrecognized payload format."""
        payload = b"this is not a valid EDI payload"

        with pytest.raises(EDIParseError):
            await service.ingest_edi(payload, _TENANT_ID)

    @pytest.mark.asyncio
    async def test_ingest_empty_payload_raises_edi_parse_error(self, service):
        """ingest_edi raises EDIParseError for empty payload."""
        with pytest.raises(EDIParseError):
            await service.ingest_edi(b"", _TENANT_ID)

    @pytest.mark.asyncio
    async def test_ingest_x12_missing_fields_raises_edi_parse_error(self, service):
        """ingest_edi raises EDIParseError when X12 payload is missing required fields."""
        # X12 payload with only ISA and ST segments (no BOL data)
        segments = [
            "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *240115*1030*U*00401*000000001*0*P*>",
            "GS*SH*SENDER*RECEIVER*20240115*1030*1*X*004010",
            "ST*856*0001",
            "SE*1*0001",
            "GE*1*1",
            "IEA*1*000000001",
        ]
        payload = "~".join(segments).encode("utf-8") + b"~"

        with pytest.raises(EDIParseError):
            await service.ingest_edi(payload, _TENANT_ID)


# ---------------------------------------------------------------------------
# Tests: ingest_manual — Validates: Requirement 10.2
# ---------------------------------------------------------------------------


class TestIngestManual:
    """Tests for ingest_manual with operator confirmation workflow."""

    @pytest.mark.asyncio
    async def test_manual_upload_creates_bol_with_pending_confirmation(self, service, es_service):
        """ingest_manual creates a BOL with pending_confirmation status."""
        file_bytes = b"%PDF-1.4 fake pdf content"
        content_type = "application/pdf"

        result = await service.ingest_manual(file_bytes, content_type, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.tenant_id == _TENANT_ID
        assert result.status == "pending_confirmation"
        assert result.needs_operator_confirmation is True
        assert result.bol_id.startswith("bol_")
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_manual_upload_accepts_jpeg(self, service, es_service):
        """ingest_manual accepts image/jpeg content type."""
        file_bytes = b"\xff\xd8\xff\xe0 fake jpeg"
        content_type = "image/jpeg"

        result = await service.ingest_manual(file_bytes, content_type, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.status == "pending_confirmation"

    @pytest.mark.asyncio
    async def test_manual_upload_accepts_png(self, service, es_service):
        """ingest_manual accepts image/png content type."""
        file_bytes = b"\x89PNG fake png"
        content_type = "image/png"

        result = await service.ingest_manual(file_bytes, content_type, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.status == "pending_confirmation"

    @pytest.mark.asyncio
    async def test_manual_upload_rejects_invalid_content_type(self, service):
        """ingest_manual raises ValueError for unsupported content types."""
        file_bytes = b"some content"
        content_type = "text/plain"

        with pytest.raises(Exception) as exc_info:
            await service.ingest_manual(file_bytes, content_type, _TENANT_ID)

        assert "content type" in str(exc_info.value).lower() or "Unsupported" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_manual_upload_rejects_empty_file(self, service):
        """ingest_manual raises ValueError for empty file bytes."""
        with pytest.raises(Exception):
            await service.ingest_manual(b"", "application/pdf", _TENANT_ID)

    @pytest.mark.asyncio
    async def test_manual_upload_stores_raw_document(self, es_service, registry):
        """ingest_manual stores the raw document via FileStorageService when available."""
        file_storage = MagicMock()
        file_storage.put = MagicMock(return_value="tenant_fuel_co/terminal_bols/doc123.pdf")

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            file_storage_service=file_storage,
        )

        file_bytes = b"%PDF-1.4 fake pdf content"
        result = await svc.ingest_manual(file_bytes, "application/pdf", _TENANT_ID)

        file_storage.put.assert_called_once_with(
            tenant_id=_TENANT_ID,
            category="terminal_bols",
            content_bytes=file_bytes,
            content_type="application/pdf",
        )
        assert result.raw_document_ref == "tenant_fuel_co/terminal_bols/doc123.pdf"

    @pytest.mark.asyncio
    async def test_manual_upload_persists_to_es(self, service, es_service):
        """ingest_manual persists the BOL document to the terminal_bols index."""
        file_bytes = b"%PDF-1.4 fake pdf content"

        result = await service.ingest_manual(file_bytes, "application/pdf", _TENANT_ID)

        es_service.index_document.assert_called_once()
        call_args = es_service.index_document.call_args
        assert call_args[0][0] == "terminal_bols"
        assert call_args[0][1] == result.bol_id
        doc = call_args[0][2]
        assert doc["tenant_id"] == _TENANT_ID
        assert doc["status"] == "pending_confirmation"
        assert doc["needs_operator_confirmation"] is True


# ---------------------------------------------------------------------------
# Tests: confirm_manual_bol — Validates: Requirement 10.2
# ---------------------------------------------------------------------------


class TestConfirmManualBol:
    """Tests for confirm_manual_bol operator confirmation workflow."""

    @pytest.mark.asyncio
    async def test_confirm_updates_bol_with_correct_fields(self, es_service, registry):
        """confirm_manual_bol updates the BOL with operator-confirmed values."""
        # Setup: mock ES to return a pending_confirmation BOL
        bol_id = "bol_test_123"
        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "PENDING",
                        "product_code": "PENDING",
                        "gross_gallons": 0.1,
                        "net_gallons": 0.1,
                        "observed_temperature_f": 60.0,
                        "api_gravity": 0.0,
                        "supplier_name": "PENDING",
                        "terminal_name": "PENDING",
                        "driver_id": "PENDING",
                        "timestamp": "2024-01-15T10:30:00+00:00",
                        "status": "pending_confirmation",
                        "needs_operator_confirmation": True,
                        "created_at": "2024-01-15T10:30:00+00:00",
                        "updated_at": "2024-01-15T10:30:00+00:00",
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        confirmed_fields = {
            "load_number": "LOAD-2024-100",
            "product_code": "UNL87",
            "gross_gallons": 8500.0,
            "net_gallons": 8450.5,
            "observed_temperature_f": 72.5,
            "api_gravity": 58.2,
            "supplier_name": "Marathon Petroleum",
            "terminal_name": "Pasadena Terminal",
            "driver_id": "DRV-100",
        }

        result = await svc.confirm_manual_bol(_TENANT_ID, bol_id, confirmed_fields)

        assert result.status == "ingested"
        assert result.needs_operator_confirmation is False
        assert result.load_number == "LOAD-2024-100"
        assert result.product_code == "UNL87"
        assert result.gross_gallons == 8500.0
        assert result.net_gallons == 8450.5
        assert result.supplier_name == "Marathon Petroleum"
        assert result.terminal_name == "Pasadena Terminal"
        assert result.driver_id == "DRV-100"
        es_service.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_confirm_rejects_non_pending_bol(self, es_service, registry):
        """confirm_manual_bol raises error if BOL is not in pending_confirmation status."""
        bol_id = "bol_test_456"
        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "status": "ingested",
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        with pytest.raises(Exception) as exc_info:
            await svc.confirm_manual_bol(_TENANT_ID, bol_id, {"load_number": "X"})

        assert "pending_confirmation" in str(exc_info.value).lower() or "not in" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_confirm_rejects_not_found_bol(self, es_service, registry):
        """confirm_manual_bol raises error if BOL is not found."""
        es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        with pytest.raises(Exception) as exc_info:
            await svc.confirm_manual_bol(_TENANT_ID, "bol_nonexistent", {"load_number": "X"})

        assert "not found" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Tests: link_to_load_plan (Task 11.8 — Validates: Requirement 10.5)
# ---------------------------------------------------------------------------


class TestLinkToLoadPlan:
    """Tests for link_to_load_plan chain-of-custody traceability.

    Validates: Requirement 10.5
    """

    @pytest.mark.asyncio
    async def test_link_to_load_plan_updates_bol_with_load_plan_id(self, es_service, registry):
        """link_to_load_plan sets load_plan_id on the BOL document.

        Validates: Requirement 10.5
        """
        bol_id = "bol_test_link_001"
        load_plan_id = "lp_2024_001"

        # Mock ES to return an ingested BOL
        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "LOAD-2024-001",
                        "status": "ingested",
                        "load_plan_id": None,
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        await svc.link_to_load_plan(bol_id, load_plan_id, _TENANT_ID)

        # Verify update_document was called with correct payload
        es_service.update_document.assert_called_once()
        call_args = es_service.update_document.call_args
        assert call_args[0][0] == "terminal_bols"
        assert call_args[0][1] == bol_id
        update_payload = call_args[0][2]
        assert update_payload["doc"]["load_plan_id"] == load_plan_id
        assert update_payload["doc"]["status"] == "linked"

    @pytest.mark.asyncio
    async def test_link_to_load_plan_transitions_status_to_linked(self, es_service, registry):
        """link_to_load_plan transitions BOL status from 'ingested' to 'linked'.

        Validates: Requirement 10.5
        """
        bol_id = "bol_test_link_002"
        load_plan_id = "lp_2024_002"

        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "LOAD-2024-002",
                        "status": "ingested",
                        "load_plan_id": None,
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        await svc.link_to_load_plan(bol_id, load_plan_id, _TENANT_ID)

        call_args = es_service.update_document.call_args
        update_payload = call_args[0][2]
        assert update_payload["doc"]["status"] == "linked"
        assert "updated_at" in update_payload["doc"]

    @pytest.mark.asyncio
    async def test_link_to_load_plan_raises_error_when_bol_not_found(self, es_service, registry):
        """link_to_load_plan raises validation error when BOL does not exist.

        Validates: Requirement 10.5
        """
        es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        with pytest.raises(Exception) as exc_info:
            await svc.link_to_load_plan("bol_nonexistent", "lp_123", _TENANT_ID)

        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_link_to_load_plan_raises_error_when_bol_not_linkable(self, es_service, registry):
        """link_to_load_plan raises error when BOL is in pending_confirmation status.

        Only BOLs with status 'ingested' or 'verified' can be linked.

        Validates: Requirement 10.5
        """
        bol_id = "bol_test_link_pending"

        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "PENDING",
                        "status": "pending_confirmation",
                        "load_plan_id": None,
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        with pytest.raises(Exception) as exc_info:
            await svc.link_to_load_plan(bol_id, "lp_123", _TENANT_ID)

        assert "cannot be linked" in str(exc_info.value).lower() or "not linkable" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_link_to_load_plan_already_linked_raises_error(self, es_service, registry):
        """link_to_load_plan raises error when BOL is already in 'linked' status.

        Validates: Requirement 10.5
        """
        bol_id = "bol_test_link_already"

        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "LOAD-2024-003",
                        "status": "linked",
                        "load_plan_id": "lp_existing",
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        with pytest.raises(Exception) as exc_info:
            await svc.link_to_load_plan(bol_id, "lp_new", _TENANT_ID)

        assert "cannot be linked" in str(exc_info.value).lower() or "not linkable" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_link_to_load_plan_verified_bol_can_be_linked(self, es_service, registry):
        """link_to_load_plan succeeds when BOL status is 'verified'.

        BOLs in 'verified' status should also be linkable.

        Validates: Requirement 10.5
        """
        bol_id = "bol_test_link_verified"
        load_plan_id = "lp_2024_verified"

        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "LOAD-2024-004",
                        "status": "verified",
                        "load_plan_id": None,
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        await svc.link_to_load_plan(bol_id, load_plan_id, _TENANT_ID)

        es_service.update_document.assert_called_once()
        call_args = es_service.update_document.call_args
        update_payload = call_args[0][2]
        assert update_payload["doc"]["load_plan_id"] == load_plan_id
        assert update_payload["doc"]["status"] == "linked"

    @pytest.mark.asyncio
    async def test_link_to_load_plan_uses_tenant_scoped_query(self, es_service, registry):
        """link_to_load_plan applies tenant_id filter when querying for the BOL.

        Validates: Requirement 10.5 (tenant isolation)
        """
        bol_id = "bol_test_link_tenant"
        load_plan_id = "lp_2024_tenant"

        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "LOAD-2024-005",
                        "status": "ingested",
                        "load_plan_id": None,
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        await svc.link_to_load_plan(bol_id, load_plan_id, _TENANT_ID)

        # Verify the search query includes tenant_id filter
        search_call = es_service.search_documents.call_args
        query = search_call[0][1]
        # The query should be wrapped with inject_tenant_filter
        assert "bool" in query.get("query", {})
        bool_query = query["query"]["bool"]
        filters = bool_query.get("filter", [])
        tenant_filter_found = any(
            f.get("term", {}).get("tenant_id") == _TENANT_ID
            for f in filters
        )
        assert tenant_filter_found, "Query must include tenant_id filter"


# ---------------------------------------------------------------------------
# Tests: driver_id validation (Task 11.6 — Validates: Requirement 10.3)
# ---------------------------------------------------------------------------


class TestDriverIdValidation:
    """Tests for driver_id validation against DriverQualificationService."""

    @pytest.mark.asyncio
    async def test_ingest_edi_with_active_driver_succeeds(self, es_service, registry):
        """ingest_edi succeeds when driver_id matches an active driver.

        Validates: Requirement 10.3
        """
        # Setup: mock DriverQualificationService returning an active driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-100",
            "tenant_id": _TENANT_ID,
            "full_name": "John Smith",
            "status": "active",
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.driver_id == "DRV-100"
        assert result.status == "ingested"
        driver_qual_service.get.assert_called_once_with(_TENANT_ID, "DRV-100")

    @pytest.mark.asyncio
    async def test_ingest_edi_with_nonexistent_driver_raises_error(self, es_service, registry):
        """ingest_edi raises validation error when driver_id is not found.

        Validates: Requirement 10.3
        """
        # Setup: mock DriverQualificationService raising resource_not_found
        from errors.exceptions import resource_not_found

        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(
            side_effect=resource_not_found(
                "Driver 'DRV-100' not found",
                details={"driver_id": "DRV-100"},
            )
        )

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
        )

        payload = _make_valid_x12_payload()

        with pytest.raises(Exception) as exc_info:
            await svc.ingest_edi(payload, _TENANT_ID)

        assert "not found" in str(exc_info.value).lower() or "driver" in str(exc_info.value).lower()
        # BOL should NOT be persisted to ES
        es_service.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_edi_with_suspended_driver_raises_error(self, es_service, registry):
        """ingest_edi raises validation error when driver is suspended (not active).

        Validates: Requirement 10.3
        """
        # Setup: mock DriverQualificationService returning a suspended driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-100",
            "tenant_id": _TENANT_ID,
            "full_name": "John Smith",
            "status": "suspended",
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
        )

        payload = _make_valid_x12_payload()

        with pytest.raises(Exception) as exc_info:
            await svc.ingest_edi(payload, _TENANT_ID)

        assert "not active" in str(exc_info.value).lower() or "suspended" in str(exc_info.value).lower()
        # BOL should NOT be persisted to ES
        es_service.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_edi_with_expired_driver_raises_error(self, es_service, registry):
        """ingest_edi raises validation error when driver status is expired.

        Validates: Requirement 10.3
        """
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-100",
            "tenant_id": _TENANT_ID,
            "full_name": "John Smith",
            "status": "expired",
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
        )

        payload = _make_valid_x12_payload()

        with pytest.raises(Exception) as exc_info:
            await svc.ingest_edi(payload, _TENANT_ID)

        assert "not active" in str(exc_info.value).lower() or "expired" in str(exc_info.value).lower()
        es_service.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_edi_without_driver_service_skips_validation(self, service, es_service):
        """ingest_edi skips driver validation when service is not configured.

        This ensures backward compatibility — when the
        DriverQualificationService is not injected, BOL ingestion still
        works without driver validation.

        Validates: Requirement 10.3 (graceful degradation)
        """
        payload = _make_valid_x12_payload()

        result = await service.ingest_edi(payload, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.driver_id == "DRV-100"
        assert result.status == "ingested"
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_confirm_manual_bol_validates_driver_id(self, es_service, registry):
        """confirm_manual_bol validates driver_id when provided in confirmed fields.

        Validates: Requirement 10.3
        """
        bol_id = "bol_test_driver_val"
        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "PENDING",
                        "product_code": "PENDING",
                        "gross_gallons": 0.1,
                        "net_gallons": 0.1,
                        "observed_temperature_f": 60.0,
                        "api_gravity": 0.0,
                        "supplier_name": "PENDING",
                        "terminal_name": "PENDING",
                        "driver_id": "PENDING",
                        "timestamp": "2024-01-15T10:30:00+00:00",
                        "status": "pending_confirmation",
                        "needs_operator_confirmation": True,
                        "created_at": "2024-01-15T10:30:00+00:00",
                        "updated_at": "2024-01-15T10:30:00+00:00",
                    }
                }]
            }
        })

        # Mock DriverQualificationService returning a suspended driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-300",
            "tenant_id": _TENANT_ID,
            "full_name": "Jane Doe",
            "status": "suspended",
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
        )

        confirmed_fields = {
            "load_number": "LOAD-2024-300",
            "product_code": "UNL87",
            "gross_gallons": 8500.0,
            "net_gallons": 8450.5,
            "observed_temperature_f": 72.5,
            "api_gravity": 58.2,
            "supplier_name": "Marathon Petroleum",
            "terminal_name": "Pasadena Terminal",
            "driver_id": "DRV-300",
        }

        with pytest.raises(Exception) as exc_info:
            await svc.confirm_manual_bol(_TENANT_ID, bol_id, confirmed_fields)

        assert "not active" in str(exc_info.value).lower() or "suspended" in str(exc_info.value).lower()
        # Update should NOT be persisted
        es_service.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_manual_bol_with_active_driver_succeeds(self, es_service, registry):
        """confirm_manual_bol succeeds when driver_id matches an active driver.

        Validates: Requirement 10.3
        """
        bol_id = "bol_test_driver_ok"
        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "PENDING",
                        "product_code": "PENDING",
                        "gross_gallons": 0.1,
                        "net_gallons": 0.1,
                        "observed_temperature_f": 60.0,
                        "api_gravity": 0.0,
                        "supplier_name": "PENDING",
                        "terminal_name": "PENDING",
                        "driver_id": "PENDING",
                        "timestamp": "2024-01-15T10:30:00+00:00",
                        "status": "pending_confirmation",
                        "needs_operator_confirmation": True,
                        "created_at": "2024-01-15T10:30:00+00:00",
                        "updated_at": "2024-01-15T10:30:00+00:00",
                    }
                }]
            }
        })

        # Mock DriverQualificationService returning an active driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-400",
            "tenant_id": _TENANT_ID,
            "full_name": "Bob Johnson",
            "status": "active",
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
        )

        confirmed_fields = {
            "load_number": "LOAD-2024-400",
            "product_code": "ULSD",
            "gross_gallons": 9000.0,
            "net_gallons": 8950.0,
            "observed_temperature_f": 70.0,
            "api_gravity": 35.0,
            "supplier_name": "Valero Energy",
            "terminal_name": "Houston Terminal",
            "driver_id": "DRV-400",
        }

        result = await svc.confirm_manual_bol(_TENANT_ID, bol_id, confirmed_fields)

        assert result.status == "ingested"
        assert result.driver_id == "DRV-400"
        assert result.needs_operator_confirmation is False
        driver_qual_service.get.assert_called_once_with(_TENANT_ID, "DRV-400")
        es_service.update_document.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: VCF cross-reference (Task 11.7 — Validates: Requirement 10.4)
# ---------------------------------------------------------------------------


class TestVCFCrossReference:
    """Tests for VCF cross-reference validation against VCFCalculator.

    Validates: Requirement 10.4
    """

    @pytest.mark.asyncio
    async def test_ingest_edi_no_discrepancy_sets_flag_false(self, es_service, registry):
        """ingest_edi sets vcf_discrepancy_flag=False when net_gallons match within ±0.1%.

        Validates: Requirement 10.4
        """
        # VCFCalculator that returns a value matching the BOL's net_gallons
        # The X12 payload has gross=8500.0, net=8450.5, temp=72.5, api=58.2
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8450.5)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert result.vcf_discrepancy_flag is False
        vcf_calculator.compute_net_gallons.assert_called_once_with(
            gross_gallons=8500.0,
            temperature_f=72.5,
            api_gravity=58.2,
        )
        # BOL should still be persisted
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_edi_discrepancy_exceeds_threshold_sets_flag_true(self, es_service, registry):
        """ingest_edi sets vcf_discrepancy_flag=True when discrepancy exceeds ±0.1%.

        Validates: Requirement 10.4
        """
        # The X12 payload has net_gallons=8450.5
        # Return a computed value that differs by more than 0.1%
        # 0.1% of 8450.5 = 8.4505, so a difference of ~9 gallons should trigger
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8440.0)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert result.vcf_discrepancy_flag is True
        # BOL should still be persisted (flagged, not rejected)
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_edi_discrepancy_at_boundary_not_flagged(self, es_service, registry):
        """ingest_edi does NOT flag when discrepancy is exactly at ±0.1% boundary.

        The threshold is > 0.1%, so exactly 0.1% should NOT be flagged.

        Validates: Requirement 10.4
        """
        # The X12 payload has net_gallons=8450.5
        # Exactly 0.1% discrepancy: computed = 8450.5 / 1.001 ≈ 8442.057
        # Or: computed such that |8450.5 - computed| / computed = 0.001
        # computed = 8450.5 / 1.001 = 8442.057...
        # Let's use a value where discrepancy is exactly 0.001 (0.1%)
        # |8450.5 - x| / x = 0.001 → 8450.5 = x * 1.001 → x = 8450.5 / 1.001 ≈ 8442.057
        computed_at_boundary = 8450.5 / 1.001  # exactly 0.1% discrepancy

        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=computed_at_boundary)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        # Exactly at boundary should NOT be flagged (threshold is strictly >)
        assert result.vcf_discrepancy_flag is False

    @pytest.mark.asyncio
    async def test_ingest_edi_vcf_calculator_not_configured_skips(self, service, es_service):
        """ingest_edi skips VCF cross-reference when VCFCalculator is not configured.

        Validates: Requirement 10.4 (graceful degradation)
        """
        payload = _make_valid_x12_payload()
        result = await service.ingest_edi(payload, _TENANT_ID)

        # vcf_discrepancy_flag should remain None (not checked)
        assert result.vcf_discrepancy_flag is None
        # BOL should still be persisted
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_edi_vcf_calculator_raises_error_skips_gracefully(self, es_service, registry):
        """ingest_edi skips VCF cross-reference when VCFCalculator raises an error.

        The BOL should still be ingested — VCF errors are non-blocking.

        Validates: Requirement 10.4
        """
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(
            side_effect=ValueError("vcf.input_out_of_range: temperature 200°F outside [-50, 150]")
        )

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        # vcf_discrepancy_flag should remain None (error during check)
        assert result.vcf_discrepancy_flag is None
        # BOL should still be persisted
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_edi_negative_discrepancy_flagged(self, es_service, registry):
        """ingest_edi flags when computed net_gallons is HIGHER than terminal-reported.

        Tests the case where the terminal under-reports net gallons.

        Validates: Requirement 10.4
        """
        # The X12 payload has net_gallons=8450.5
        # Computed value is higher: 8462.0 → discrepancy = |8450.5 - 8462| / 8462 ≈ 0.136%
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8462.0)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert result.vcf_discrepancy_flag is True
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_pipe_payload_vcf_cross_reference(self, es_service, registry):
        """ingest_edi with pipe-delimited payload also performs VCF cross-reference.

        Validates: Requirement 10.4
        """
        # Pipe payload has gross=9200.0, net=9150.3, temp=68.0, api=35.5
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=9150.3)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_pipe_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert result.vcf_discrepancy_flag is False
        vcf_calculator.compute_net_gallons.assert_called_once_with(
            gross_gallons=9200.0,
            temperature_f=68.0,
            api_gravity=35.5,
        )

    @pytest.mark.asyncio
    async def test_confirm_manual_bol_vcf_cross_reference_flags_discrepancy(self, es_service, registry):
        """confirm_manual_bol performs VCF cross-reference and flags discrepancy.

        Validates: Requirement 10.4
        """
        bol_id = "bol_test_vcf_confirm"
        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": bol_id,
                        "tenant_id": _TENANT_ID,
                        "load_number": "PENDING",
                        "product_code": "PENDING",
                        "gross_gallons": 0.1,
                        "net_gallons": 0.1,
                        "observed_temperature_f": 60.0,
                        "api_gravity": 0.0,
                        "supplier_name": "PENDING",
                        "terminal_name": "PENDING",
                        "driver_id": "PENDING",
                        "timestamp": "2024-01-15T10:30:00+00:00",
                        "status": "pending_confirmation",
                        "needs_operator_confirmation": True,
                        "created_at": "2024-01-15T10:30:00+00:00",
                        "updated_at": "2024-01-15T10:30:00+00:00",
                    }
                }]
            }
        })

        # VCFCalculator returns a value that differs by more than 0.1%
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8400.0)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            vcf_calculator=vcf_calculator,
        )

        confirmed_fields = {
            "load_number": "LOAD-2024-500",
            "product_code": "UNL87",
            "gross_gallons": 8500.0,
            "net_gallons": 8450.5,
            "observed_temperature_f": 72.5,
            "api_gravity": 58.2,
            "supplier_name": "Marathon Petroleum",
            "terminal_name": "Pasadena Terminal",
            "driver_id": "DRV-500",
        }

        result = await svc.confirm_manual_bol(_TENANT_ID, bol_id, confirmed_fields)

        assert result.vcf_discrepancy_flag is True
        # Verify the update_document call includes the flag
        update_call = es_service.update_document.call_args
        update_payload = update_call[0][2]  # third positional arg
        assert update_payload["doc"]["vcf_discrepancy_flag"] is True


# ---------------------------------------------------------------------------
# Tests: Idempotency — duplicate load_number rejection (Task 11.9)
# Validates: Requirement 10.6
# ---------------------------------------------------------------------------


class TestDuplicateLoadNumberRejection:
    """Tests for idempotency enforcement via duplicate load_number rejection.

    Validates: Requirement 10.6
    """

    @pytest.mark.asyncio
    async def test_ingest_edi_rejects_duplicate_load_number(self, registry):
        """ingest_edi rejects a BOL when load_number already exists for the tenant.

        Validates: Requirement 10.6
        """
        es_service = _make_es_service()

        # Mock: search_documents returns an existing BOL with the same load_number
        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": "bol_existing_001",
                        "tenant_id": _TENANT_ID,
                        "load_number": "LOAD-2024-001",
                        "status": "ingested",
                    }
                }]
            }
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        payload = _make_valid_x12_payload()

        with pytest.raises(Exception) as exc_info:
            await svc.ingest_edi(payload, _TENANT_ID)

        error = exc_info.value
        assert "duplicate" in str(error).lower() or "already exists" in str(error).lower()
        assert hasattr(error, "details") and error.details.get("error_code") == "bol.duplicate_load_number"
        # BOL should NOT be persisted to ES
        es_service.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_manual_bol_rejects_duplicate_load_number(self, registry):
        """confirm_manual_bol rejects when confirmed load_number already exists.

        Validates: Requirement 10.6
        """
        es_service = _make_es_service()

        # First call: search for the BOL by bol_id (returns pending BOL)
        # Second call: search for duplicate load_number (returns existing BOL)
        es_service.search_documents = AsyncMock(side_effect=[
            # First call: lookup the pending BOL by bol_id
            {
                "hits": {
                    "hits": [{
                        "_source": {
                            "bol_id": "bol_pending_001",
                            "tenant_id": _TENANT_ID,
                            "load_number": "PENDING",
                            "product_code": "PENDING",
                            "gross_gallons": 0.1,
                            "net_gallons": 0.1,
                            "observed_temperature_f": 60.0,
                            "api_gravity": 0.0,
                            "supplier_name": "PENDING",
                            "terminal_name": "PENDING",
                            "driver_id": "PENDING",
                            "timestamp": "2024-01-15T10:30:00+00:00",
                            "status": "pending_confirmation",
                            "needs_operator_confirmation": True,
                            "created_at": "2024-01-15T10:30:00+00:00",
                            "updated_at": "2024-01-15T10:30:00+00:00",
                        }
                    }]
                }
            },
            # Second call: duplicate check finds existing BOL with same load_number
            {
                "hits": {
                    "hits": [{
                        "_source": {
                            "bol_id": "bol_existing_002",
                            "tenant_id": _TENANT_ID,
                            "load_number": "LOAD-2024-DUP",
                            "status": "ingested",
                        }
                    }]
                }
            },
        ])

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        confirmed_fields = {
            "load_number": "LOAD-2024-DUP",
            "product_code": "UNL87",
            "gross_gallons": 8500.0,
            "net_gallons": 8450.5,
            "observed_temperature_f": 72.5,
            "api_gravity": 58.2,
            "supplier_name": "Marathon Petroleum",
            "terminal_name": "Pasadena Terminal",
            "driver_id": "DRV-100",
        }

        with pytest.raises(Exception) as exc_info:
            await svc.confirm_manual_bol(_TENANT_ID, "bol_pending_001", confirmed_fields)

        error = exc_info.value
        assert "duplicate" in str(error).lower() or "already exists" in str(error).lower()
        assert hasattr(error, "details") and error.details.get("error_code") == "bol.duplicate_load_number"
        # BOL should NOT be updated in ES
        es_service.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_edi_succeeds_when_no_duplicate(self, registry):
        """ingest_edi succeeds when no BOL with the same load_number exists.

        Validates: Requirement 10.6
        """
        es_service = _make_es_service()

        # Mock: search_documents returns no hits (no duplicate)
        es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.load_number == "LOAD-2024-001"
        assert result.status == "ingested"
        # BOL should be persisted to ES
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_check_is_tenant_scoped(self, registry):
        """Idempotency check queries with tenant_id filter (tenant isolation).

        Validates: Requirement 10.6
        """
        es_service = _make_es_service()

        # Mock: no duplicate found
        es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
        )

        payload = _make_valid_x12_payload()
        await svc.ingest_edi(payload, _TENANT_ID)

        # Verify the search query includes tenant_id filter
        search_call = es_service.search_documents.call_args
        query = search_call[0][1]
        assert "bool" in query.get("query", {})
        bool_query = query["query"]["bool"]
        filters = bool_query.get("filter", [])
        tenant_filter_found = any(
            f.get("term", {}).get("tenant_id") == _TENANT_ID
            for f in filters
        )
        assert tenant_filter_found, "Duplicate check query must include tenant_id filter"


# ---------------------------------------------------------------------------
# Tests: Raw EDI persistence (Task 11.10 — Validates: Requirement 10.7)
# ---------------------------------------------------------------------------


class TestIngestEdiRawDocumentPersistence:
    """Tests for raw EDI payload persistence via FileStorageService.

    Validates: Requirement 10.7 — THE Terminal_BOL_Ingestion_Service SHALL
    store the raw EDI payload or uploaded document as an immutable attachment
    alongside the parsed record for audit purposes.
    """

    @pytest.mark.asyncio
    async def test_ingest_edi_stores_raw_payload_via_file_storage(self, es_service, registry):
        """ingest_edi stores the raw EDI payload via FileStorageService.put().

        Validates: Requirement 10.7
        """
        file_storage = MagicMock()
        file_storage.put = MagicMock(
            return_value="tenants/tenant_fuel_co/terminal_bols/2024/01/15/abc123.edi"
        )

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            file_storage_service=file_storage,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        # Verify FileStorageService.put() was called with the raw EDI bytes
        file_storage.put.assert_called_once_with(
            tenant_id=_TENANT_ID,
            category="terminal_bols",
            content_bytes=payload,
            content_type="application/edi-x12",
        )
        # Verify the returned S3 reference is set on the BOL
        assert result.raw_document_ref == "tenants/tenant_fuel_co/terminal_bols/2024/01/15/abc123.edi"

    @pytest.mark.asyncio
    async def test_ingest_edi_raw_document_ref_persisted_to_es(self, es_service, registry):
        """ingest_edi persists the raw_document_ref in the ES document.

        Validates: Requirement 10.7
        """
        file_storage = MagicMock()
        file_storage.put = MagicMock(
            return_value="tenants/tenant_fuel_co/terminal_bols/2024/01/15/doc456.edi"
        )

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            file_storage_service=file_storage,
        )

        payload = _make_valid_pipe_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        # Verify the ES document includes raw_document_ref
        es_service.index_document.assert_called_once()
        call_args = es_service.index_document.call_args
        doc = call_args[0][2]
        assert doc["raw_document_ref"] == "tenants/tenant_fuel_co/terminal_bols/2024/01/15/doc456.edi"

    @pytest.mark.asyncio
    async def test_ingest_edi_skips_storage_when_file_service_not_configured(self, es_service, registry):
        """ingest_edi skips raw storage gracefully when FileStorageService is None.

        Validates: Requirement 10.7 (graceful degradation)
        """
        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            file_storage_service=None,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        # BOL should still be ingested successfully
        assert isinstance(result, TerminalBOL)
        assert result.status == "ingested"
        # raw_document_ref should be None when storage is not configured
        assert result.raw_document_ref is None
        # ES persistence should still happen
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_edi_continues_when_file_storage_fails(self, es_service, registry):
        """ingest_edi continues ingestion even if FileStorageService.put() raises.

        The raw document storage is best-effort — a failure should not block
        BOL ingestion.

        Validates: Requirement 10.7 (fault tolerance)
        """
        file_storage = MagicMock()
        file_storage.put = MagicMock(side_effect=RuntimeError("S3 connection timeout"))

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            file_storage_service=file_storage,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        # BOL should still be ingested successfully despite storage failure
        assert isinstance(result, TerminalBOL)
        assert result.status == "ingested"
        # raw_document_ref should be None since storage failed
        assert result.raw_document_ref is None
        # ES persistence should still happen
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_edi_stores_pipe_delimited_payload(self, es_service, registry):
        """ingest_edi stores pipe-delimited EDI payload with same content_type.

        Validates: Requirement 10.7
        """
        file_storage = MagicMock()
        file_storage.put = MagicMock(
            return_value="tenants/tenant_fuel_co/terminal_bols/2024/01/15/pipe789.edi"
        )

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            file_storage_service=file_storage,
        )

        payload = _make_valid_pipe_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        # Verify the exact raw bytes are stored (not the parsed version)
        file_storage.put.assert_called_once_with(
            tenant_id=_TENANT_ID,
            category="terminal_bols",
            content_bytes=payload,
            content_type="application/edi-x12",
        )
        assert result.raw_document_ref == "tenants/tenant_fuel_co/terminal_bols/2024/01/15/pipe789.edi"


# ---------------------------------------------------------------------------
# Tests: Combined scenarios — driver validation + VCF + duplicate check
# (Task 11.13 — Validates: Requirements 10.3, 10.4, 10.6)
# ---------------------------------------------------------------------------


class TestCombinedValidationScenarios:
    """Tests for combined validation flows where multiple checks interact.

    These tests verify that driver validation, VCF cross-reference, and
    duplicate load_number rejection all work correctly together in a single
    ingest_edi or confirm_manual_bol call.

    Validates: Requirements 10.3, 10.4, 10.6
    """

    @pytest.mark.asyncio
    async def test_all_validations_pass_together(self, registry):
        """ingest_edi succeeds when driver is active, VCF matches, and no duplicate exists.

        Validates: Requirements 10.3, 10.4, 10.6
        """
        es_service = _make_es_service()
        # No duplicate found
        es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })

        # Active driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-100",
            "tenant_id": _TENANT_ID,
            "full_name": "John Smith",
            "status": "active",
        })

        # VCF matches within tolerance
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8450.5)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.status == "ingested"
        assert result.driver_id == "DRV-100"
        assert result.vcf_discrepancy_flag is False
        # All services were called
        driver_qual_service.get.assert_called_once_with(_TENANT_ID, "DRV-100")
        vcf_calculator.compute_net_gallons.assert_called_once()
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_driver_validation_fails_before_duplicate_check(self, registry):
        """ingest_edi fails on driver validation before reaching duplicate check.

        Driver validation runs before idempotency check in the pipeline.
        When driver validation fails, the duplicate check should NOT be reached.

        Validates: Requirements 10.3, 10.6
        """
        es_service = _make_es_service()

        # Suspended driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-100",
            "tenant_id": _TENANT_ID,
            "full_name": "John Smith",
            "status": "suspended",
        })

        # VCF calculator should NOT be called
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8450.5)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()

        with pytest.raises(Exception) as exc_info:
            await svc.ingest_edi(payload, _TENANT_ID)

        assert "not active" in str(exc_info.value).lower() or "suspended" in str(exc_info.value).lower()
        # Duplicate check (search_documents) should NOT have been called
        es_service.search_documents.assert_not_called()
        # VCF should NOT have been called
        vcf_calculator.compute_net_gallons.assert_not_called()
        # BOL should NOT be persisted
        es_service.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_check_fails_after_driver_passes(self, registry):
        """ingest_edi fails on duplicate check after driver validation passes.

        Validates: Requirements 10.3, 10.6
        """
        es_service = _make_es_service()
        # Duplicate found
        es_service.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{
                    "_source": {
                        "bol_id": "bol_existing",
                        "tenant_id": _TENANT_ID,
                        "load_number": "LOAD-2024-001",
                        "status": "ingested",
                    }
                }]
            }
        })

        # Active driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-100",
            "tenant_id": _TENANT_ID,
            "full_name": "John Smith",
            "status": "active",
        })

        # VCF should NOT be called (duplicate check fails first)
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8450.5)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()

        with pytest.raises(Exception) as exc_info:
            await svc.ingest_edi(payload, _TENANT_ID)

        error = exc_info.value
        assert "duplicate" in str(error).lower() or "already exists" in str(error).lower()
        # Driver was validated (passed)
        driver_qual_service.get.assert_called_once()
        # VCF should NOT have been called (duplicate check fails before VCF)
        vcf_calculator.compute_net_gallons.assert_not_called()
        # BOL should NOT be persisted
        es_service.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_vcf_discrepancy_with_active_driver_and_no_duplicate(self, registry):
        """ingest_edi succeeds with VCF discrepancy flag when driver is active and no duplicate.

        The BOL is still ingested (VCF discrepancy is a warning, not a rejection).

        Validates: Requirements 10.3, 10.4, 10.6
        """
        es_service = _make_es_service()
        # No duplicate
        es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })

        # Active driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-100",
            "tenant_id": _TENANT_ID,
            "full_name": "John Smith",
            "status": "active",
        })

        # VCF discrepancy: computed value differs by more than 0.1%
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8430.0)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        # BOL is still ingested despite VCF discrepancy
        assert isinstance(result, TerminalBOL)
        assert result.status == "ingested"
        assert result.vcf_discrepancy_flag is True
        assert result.driver_id == "DRV-100"
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_confirm_manual_bol_combined_driver_vcf_duplicate(self, registry):
        """confirm_manual_bol validates driver, checks duplicate, and cross-references VCF.

        Validates: Requirements 10.3, 10.4, 10.6
        """
        es_service = _make_es_service()

        # First call: lookup pending BOL; Second call: duplicate check (no duplicate)
        es_service.search_documents = AsyncMock(side_effect=[
            # First call: lookup the pending BOL
            {
                "hits": {
                    "hits": [{
                        "_source": {
                            "bol_id": "bol_combined_001",
                            "tenant_id": _TENANT_ID,
                            "load_number": "PENDING",
                            "product_code": "PENDING",
                            "gross_gallons": 0.1,
                            "net_gallons": 0.1,
                            "observed_temperature_f": 60.0,
                            "api_gravity": 0.0,
                            "supplier_name": "PENDING",
                            "terminal_name": "PENDING",
                            "driver_id": "PENDING",
                            "timestamp": "2024-01-15T10:30:00+00:00",
                            "status": "pending_confirmation",
                            "needs_operator_confirmation": True,
                            "created_at": "2024-01-15T10:30:00+00:00",
                            "updated_at": "2024-01-15T10:30:00+00:00",
                        }
                    }]
                }
            },
            # Second call: no duplicate found
            {"hits": {"hits": []}},
        ])

        # Active driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-500",
            "tenant_id": _TENANT_ID,
            "full_name": "Alice Johnson",
            "status": "active",
        })

        # VCF matches
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8450.5)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
            vcf_calculator=vcf_calculator,
        )

        confirmed_fields = {
            "load_number": "LOAD-2024-COMBINED",
            "product_code": "UNL87",
            "gross_gallons": 8500.0,
            "net_gallons": 8450.5,
            "observed_temperature_f": 72.5,
            "api_gravity": 58.2,
            "supplier_name": "Marathon Petroleum",
            "terminal_name": "Pasadena Terminal",
            "driver_id": "DRV-500",
        }

        result = await svc.confirm_manual_bol(_TENANT_ID, "bol_combined_001", confirmed_fields)

        assert result.status == "ingested"
        assert result.driver_id == "DRV-500"
        assert result.vcf_discrepancy_flag is False
        assert result.needs_operator_confirmation is False
        driver_qual_service.get.assert_called_once_with(_TENANT_ID, "DRV-500")
        vcf_calculator.compute_net_gallons.assert_called_once()
        es_service.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_pipe_payload_all_validations_pass(self, registry):
        """ingest_edi with pipe-delimited payload passes all validations together.

        Validates: Requirements 10.3, 10.4, 10.6
        """
        es_service = _make_es_service()
        # No duplicate
        es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })

        # Active driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-200",
            "tenant_id": _TENANT_ID,
            "full_name": "Jane Doe",
            "status": "active",
        })

        # VCF matches
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=9150.3)

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
            vcf_calculator=vcf_calculator,
        )

        payload = _make_valid_pipe_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.status == "ingested"
        assert result.load_number == "LOAD-2024-002"
        assert result.driver_id == "DRV-200"
        assert result.vcf_discrepancy_flag is False
        driver_qual_service.get.assert_called_once_with(_TENANT_ID, "DRV-200")
        vcf_calculator.compute_net_gallons.assert_called_once_with(
            gross_gallons=9200.0,
            temperature_f=68.0,
            api_gravity=35.5,
        )
        es_service.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_validations_with_file_storage(self, registry):
        """ingest_edi with all validations + file storage persists raw document.

        Validates: Requirements 10.3, 10.4, 10.6, 10.7
        """
        es_service = _make_es_service()
        # No duplicate
        es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })

        # Active driver
        driver_qual_service = AsyncMock()
        driver_qual_service.get = AsyncMock(return_value={
            "driver_id": "DRV-100",
            "tenant_id": _TENANT_ID,
            "full_name": "John Smith",
            "status": "active",
        })

        # VCF matches
        vcf_calculator = MagicMock()
        vcf_calculator.compute_net_gallons = MagicMock(return_value=8450.5)

        # File storage
        file_storage = MagicMock()
        file_storage.put = MagicMock(
            return_value="tenants/tenant_fuel_co/terminal_bols/2024/01/15/full.edi"
        )

        svc = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=registry,
            driver_qualification_service=driver_qual_service,
            vcf_calculator=vcf_calculator,
            file_storage_service=file_storage,
        )

        payload = _make_valid_x12_payload()
        result = await svc.ingest_edi(payload, _TENANT_ID)

        assert isinstance(result, TerminalBOL)
        assert result.status == "ingested"
        assert result.vcf_discrepancy_flag is False
        assert result.raw_document_ref == "tenants/tenant_fuel_co/terminal_bols/2024/01/15/full.edi"
        # All services were called
        driver_qual_service.get.assert_called_once()
        vcf_calculator.compute_net_gallons.assert_called_once()
        file_storage.put.assert_called_once()
        es_service.index_document.assert_called_once()
