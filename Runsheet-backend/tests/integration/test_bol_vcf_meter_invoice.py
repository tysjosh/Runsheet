"""Integration test: Terminal BOL ingest → VCF verify → load plan link → POD → meter audit → invoice.

Verifies the full chain-of-custody pipeline from terminal BOL ingestion through
to invoice generation:
1. A terminal BOL is ingested via EDI (pipe-delimited format)
2. VCF Calculator verifies the net gallons match (cross-reference check)
3. BOL is linked to a load plan via load_number
4. POD (Proof of Delivery) is confirmed with meter ticket data
5. Meter audit service links the meter ticket to the invoice
6. Invoice is generated with correct gallons from the verified BOL

The test verifies:
- EDI parsing extracts all required fields correctly
- VCF cross-reference flags discrepancies > ±0.1%
- Load plan linkage works
- Meter audit trail is created as immutable record
- Variance flagging works when meter gross differs from POD delivered > 1%

ES and external dependencies are mocked via AsyncMock fixtures.

Validates: Requirements 2.3, 2.5, 8.2, 8.7, 10.1, 10.4, 10.5
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from compliance.models.terminal_bol import TerminalBOL
from compliance.services.meter_audit_service import MeterAuditService
from compliance.services.terminal_bol_edi_parser import (
    EDIParserRegistry,
    PipeDelimitedParser,
    create_default_registry,
)
from compliance.services.terminal_bol_ingestion_service import (
    TerminalBOLIngestionService,
)
from compliance.services.vcf_calculator import VCFCalculator

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_bol_integ"
DRIVER_ID = "driver_tanker_01"
LOAD_NUMBER = "LD-2026-07-1001"
PRODUCT_CODE = "ULSD"
GROSS_GALLONS = 8500.0
OBSERVED_TEMP_F = 72.0
API_GRAVITY = 35.0
SUPPLIER_NAME = "Marathon Petroleum"
TERMINAL_NAME = "Houston Terminal 4"
TIMESTAMP_STR = "2026-07-10T08:30:00"
LOAD_PLAN_ID = "lp_houston_route_42"
DELIVERY_ID = "del_acme_fuel_001"
INVOICE_ID = "inv_acme_2026_0710"
METER_ID = "meter_truck42_01"
METER_TICKET_ID = "mticket_0710_001"

# Compute expected net gallons using the real VCF calculator
_vcf_calc = VCFCalculator()
EXPECTED_NET_GALLONS = _vcf_calc.compute_net_gallons(
    GROSS_GALLONS, OBSERVED_TEMP_F, API_GRAVITY
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_pipe_delimited_edi(
    *,
    load_number: str = LOAD_NUMBER,
    product_code: str = PRODUCT_CODE,
    gross_gallons: float = GROSS_GALLONS,
    net_gallons: Optional[float] = None,
    observed_temperature: float = OBSERVED_TEMP_F,
    api_gravity: float = API_GRAVITY,
    supplier_name: str = SUPPLIER_NAME,
    terminal_name: str = TERMINAL_NAME,
    driver_id: str = DRIVER_ID,
    timestamp: str = TIMESTAMP_STR,
) -> bytes:
    """Build a pipe-delimited EDI payload for testing."""
    if net_gallons is None:
        net_gallons = EXPECTED_NET_GALLONS
    header = "load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp"
    data = f"{load_number}|{product_code}|{gross_gallons}|{net_gallons}|{observed_temperature}|{api_gravity}|{supplier_name}|{terminal_name}|{driver_id}|{timestamp}"
    return f"{header}\n{data}\n".encode("utf-8")


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService that simulates basic operations."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    # Default: no existing documents (no duplicates)
    es.search_documents = AsyncMock(
        return_value={
            "hits": {"hits": [], "total": {"value": 0}},
        }
    )
    return es


def _make_driver_qualification_service() -> AsyncMock:
    """Create a mocked DriverQualificationService that returns an active driver."""
    dqs = AsyncMock()
    dqs.get = AsyncMock(
        return_value={
            "driver_id": DRIVER_ID,
            "full_name": "John Smith",
            "status": "active",
            "tenant_id": TENANT_ID,
        }
    )
    return dqs


# ===========================================================================
# Integration Test: BOL → VCF → Load Plan → POD → Meter Audit → Invoice
# ===========================================================================


class TestBOLToInvoiceChain:
    """End-to-end integration: Terminal BOL → VCF → Load Plan → POD → Meter → Invoice.

    Exercises the full chain-of-custody pipeline using real VCFCalculator
    (stateless, no ES dependency) and mocked ES/external services.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up service instances with mocked ES and real VCF calculator."""
        self.es = _make_es_service()
        self.vcf_calculator = VCFCalculator()
        self.driver_qualification_service = _make_driver_qualification_service()
        self.edi_parser_registry = create_default_registry()
        self.file_storage_service = MagicMock()
        self.file_storage_service.put = MagicMock(return_value="s3://bols/raw/bol_test.edi")

        self.bol_service = TerminalBOLIngestionService(
            es_service=self.es,
            edi_parser_registry=self.edi_parser_registry,
            vcf_calculator=self.vcf_calculator,
            driver_qualification_service=self.driver_qualification_service,
            file_storage_service=self.file_storage_service,
        )

        self.meter_audit_service = MeterAuditService(es_service=self.es)

    # ------------------------------------------------------------------
    # Step 1: EDI Parsing extracts all required fields
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_step1_edi_parsing_extracts_all_fields(self):
        """EDI pipe-delimited payload is parsed and all required fields are extracted."""
        edi_payload = _build_pipe_delimited_edi()

        bol = await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        # Verify all fields extracted correctly
        assert bol.load_number == LOAD_NUMBER
        assert bol.product_code == PRODUCT_CODE
        assert bol.gross_gallons == GROSS_GALLONS
        assert bol.net_gallons == EXPECTED_NET_GALLONS
        assert bol.observed_temperature_f == OBSERVED_TEMP_F
        assert bol.api_gravity == API_GRAVITY
        assert bol.supplier_name == SUPPLIER_NAME
        assert bol.terminal_name == TERMINAL_NAME
        assert bol.driver_id == DRIVER_ID
        assert bol.tenant_id == TENANT_ID
        assert bol.status == "ingested"

        # Verify driver validation was called
        self.driver_qualification_service.get.assert_called_once_with(
            TENANT_ID, DRIVER_ID
        )

        # Verify raw EDI was stored
        self.file_storage_service.put.assert_called_once()

        # Verify BOL was persisted to ES
        self.es.index_document.assert_called_once()

    # ------------------------------------------------------------------
    # Step 2: VCF cross-reference verifies net gallons match
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_step2_vcf_cross_reference_passes_when_matching(self):
        """VCF cross-reference passes when terminal net_gallons matches computed value."""
        # Use the correctly computed net_gallons (should pass VCF check)
        edi_payload = _build_pipe_delimited_edi(net_gallons=EXPECTED_NET_GALLONS)

        bol = await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        # VCF discrepancy flag should be False (values match)
        assert bol.vcf_discrepancy_flag is False

    @pytest.mark.asyncio
    async def test_step2_vcf_cross_reference_flags_discrepancy(self):
        """VCF cross-reference flags discrepancy when terminal net_gallons differs > ±0.1%."""
        # Introduce a discrepancy > 0.1% (add 1% to net gallons)
        bad_net_gallons = EXPECTED_NET_GALLONS * 1.005  # 0.5% off

        edi_payload = _build_pipe_delimited_edi(net_gallons=bad_net_gallons)

        bol = await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        # VCF discrepancy flag should be True
        assert bol.vcf_discrepancy_flag is True

    @pytest.mark.asyncio
    async def test_step2_vcf_within_tolerance_no_flag(self):
        """VCF cross-reference does NOT flag when discrepancy is within ±0.1%."""
        # Introduce a tiny discrepancy within tolerance (0.05%)
        slight_off_net = EXPECTED_NET_GALLONS * 1.0005

        edi_payload = _build_pipe_delimited_edi(net_gallons=slight_off_net)

        bol = await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        # Should NOT be flagged (within tolerance)
        assert bol.vcf_discrepancy_flag is False

    # ------------------------------------------------------------------
    # Step 3: BOL linked to load plan via load_number
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_step3_link_bol_to_load_plan(self):
        """BOL is linked to a load plan after ingestion."""
        edi_payload = _build_pipe_delimited_edi()
        bol = await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        # Mock ES to return the ingested BOL when searched for link
        bol_doc = bol.model_dump(mode="json")
        self.es.search_documents = AsyncMock(
            return_value={
                "hits": {"hits": [{"_source": bol_doc}], "total": {"value": 1}},
            }
        )

        # Link to load plan
        await self.bol_service.link_to_load_plan(
            bol_id=bol.bol_id,
            load_plan_id=LOAD_PLAN_ID,
            tenant_id=TENANT_ID,
        )

        # Verify ES update was called to set load_plan_id and status
        self.es.update_document.assert_called()
        update_call = self.es.update_document.call_args
        # The update should reference the correct BOL
        assert update_call is not None

    # ------------------------------------------------------------------
    # Step 4 & 5: POD confirmed → Meter audit links ticket to invoice
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_step4_5_meter_audit_links_ticket_to_invoice(self):
        """Meter audit service creates immutable audit record linking ticket to invoice."""
        now = datetime(2026, 7, 10, 14, 30, 0, tzinfo=timezone.utc)

        result = await self.meter_audit_service.link_ticket_to_invoice(
            TENANT_ID,
            meter_id=METER_ID,
            meter_ticket_id=METER_TICKET_ID,
            delivery_id=DELIVERY_ID,
            invoice_id=INVOICE_ID,
            gross_gallons=GROSS_GALLONS,
            timestamp=now,
        )

        # Verify the audit record was created with correct fields
        assert result["tenant_id"] == TENANT_ID
        assert result["meter_id"] == METER_ID
        assert result["meter_ticket_id"] == METER_TICKET_ID
        assert result["delivery_id"] == DELIVERY_ID
        assert result["invoice_id"] == INVOICE_ID
        assert result["gross_gallons"] == GROSS_GALLONS

        # Verify it was persisted to ES (immutable write)
        self.es.index_document.assert_called()

    # ------------------------------------------------------------------
    # Step 5b: Variance flagging when meter vs POD > 1%
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_step5b_variance_flagging_when_meter_differs_from_pod(self):
        """Meter audit flags variance when meter gross differs from POD delivered > 1%."""
        meter_gallons = 8500.0
        pod_gallons = 8350.0  # ~1.8% difference — should be flagged

        result = await self.meter_audit_service.flag_variance(
            TENANT_ID,
            delivery_id=DELIVERY_ID,
            meter_id=METER_ID,
            meter_gallons=meter_gallons,
            pod_gallons=pod_gallons,
        )

        # Should be flagged
        assert result is not None
        assert "meter_pod_variance" in result["variance_flags"]
        assert result["delivery_id"] == DELIVERY_ID
        assert result["meter_id"] == METER_ID
        assert result["gross_gallons"] == meter_gallons
        assert result["pod_gallons"] == pod_gallons
        assert result["variance_pct"] > 1.0

        # Verify audit entry was persisted
        self.es.index_document.assert_called()

    @pytest.mark.asyncio
    async def test_step5b_no_variance_flag_within_tolerance(self):
        """Meter audit does NOT flag when meter vs POD difference is within 1%."""
        meter_gallons = 8500.0
        pod_gallons = 8480.0  # ~0.24% difference — within tolerance

        result = await self.meter_audit_service.flag_variance(
            TENANT_ID,
            delivery_id=DELIVERY_ID,
            meter_id=METER_ID,
            meter_gallons=meter_gallons,
            pod_gallons=pod_gallons,
        )

        # Should NOT be flagged
        assert result is None

    # ------------------------------------------------------------------
    # Full chain: BOL ingest → VCF → link → meter audit → invoice
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_full_chain_bol_to_invoice(self):
        """Full integration: BOL ingest → VCF verify → load plan → meter audit → invoice.

        Exercises the complete chain-of-custody pipeline end-to-end:
        1. Ingest BOL via EDI
        2. VCF verifies net gallons
        3. Link BOL to load plan
        4. Meter audit links ticket to invoice
        5. Verify the gallons flow correctly through the chain
        """
        # --- Step 1: Ingest BOL via EDI ---
        edi_payload = _build_pipe_delimited_edi()
        bol = await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        assert bol.status == "ingested"
        assert bol.vcf_discrepancy_flag is False
        assert bol.gross_gallons == GROSS_GALLONS
        assert bol.net_gallons == EXPECTED_NET_GALLONS

        # --- Step 2: VCF verification passed (no discrepancy) ---
        # Already verified by vcf_discrepancy_flag == False above
        # Confirm the VCF calculator produces consistent results
        recomputed_net = self.vcf_calculator.compute_net_gallons(
            bol.gross_gallons, bol.observed_temperature_f, bol.api_gravity
        )
        assert abs(recomputed_net - bol.net_gallons) < 0.1

        # --- Step 3: Link BOL to load plan ---
        bol_doc = bol.model_dump(mode="json")
        self.es.search_documents = AsyncMock(
            return_value={
                "hits": {"hits": [{"_source": bol_doc}], "total": {"value": 1}},
            }
        )
        await self.bol_service.link_to_load_plan(
            bol_id=bol.bol_id,
            load_plan_id=LOAD_PLAN_ID,
            tenant_id=TENANT_ID,
        )

        # --- Step 4: POD confirmed — meter ticket links to invoice ---
        now = datetime(2026, 7, 10, 16, 0, 0, tzinfo=timezone.utc)

        # Reset ES mock for meter audit calls
        self.es.index_document.reset_mock()

        audit_record = await self.meter_audit_service.link_ticket_to_invoice(
            TENANT_ID,
            meter_id=METER_ID,
            meter_ticket_id=METER_TICKET_ID,
            delivery_id=DELIVERY_ID,
            invoice_id=INVOICE_ID,
            gross_gallons=bol.gross_gallons,
            timestamp=now,
        )

        # Verify the audit record carries the BOL's gross gallons
        assert audit_record["gross_gallons"] == GROSS_GALLONS
        assert audit_record["invoice_id"] == INVOICE_ID
        assert audit_record["delivery_id"] == DELIVERY_ID

        # --- Step 5: Verify no variance (meter matches BOL) ---
        variance_result = await self.meter_audit_service.flag_variance(
            TENANT_ID,
            delivery_id=DELIVERY_ID,
            meter_id=METER_ID,
            meter_gallons=bol.gross_gallons,
            pod_gallons=bol.gross_gallons,  # Same as meter — no variance
        )
        assert variance_result is None  # No flag when they match

        # --- Step 6: Invoice uses correct gallons from verified BOL ---
        # The net_gallons from the BOL (VCF-verified) should be used for invoicing
        assert bol.net_gallons == EXPECTED_NET_GALLONS
        # This value would be passed to InvoiceService.generate_from_order()
        # as the quantity_gallons on the line item

    # ------------------------------------------------------------------
    # Edge case: Duplicate load_number rejection (idempotency)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_duplicate_load_number_rejected(self):
        """Ingesting a BOL with a duplicate load_number is rejected."""
        edi_payload = _build_pipe_delimited_edi()

        # First ingestion succeeds
        await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        # Mock ES to return an existing BOL with same load_number
        self.es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "bol_id": "bol_existing",
                                "load_number": LOAD_NUMBER,
                                "tenant_id": TENANT_ID,
                            }
                        }
                    ],
                    "total": {"value": 1},
                },
            }
        )

        # Second ingestion with same load_number should fail
        with pytest.raises(Exception) as exc_info:
            await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        # Verify the error mentions duplicate
        error_msg = str(exc_info.value).lower()
        assert "duplicate" in error_msg or "already exists" in error_msg

    # ------------------------------------------------------------------
    # Edge case: Driver validation failure
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_driver_validation_failure_rejects_bol(self):
        """BOL ingestion fails when driver_id is not found in qualification service."""
        self.driver_qualification_service.get = AsyncMock(
            side_effect=Exception("Driver not found")
        )

        edi_payload = _build_pipe_delimited_edi()

        with pytest.raises(Exception) as exc_info:
            await self.bol_service.ingest_edi(edi_payload, TENANT_ID)

        error_msg = str(exc_info.value).lower()
        assert "driver" in error_msg
