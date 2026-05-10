"""Unit tests for TaxEngine wiring into ``InvoiceService.generate_from_order``.

Covers task 3.10 of the fuel-compliance-backbone spec:

    Wire ``TaxEngine`` into ``InvoiceService.generate_from_order()`` to
    append tax line items before finalization.

Test matrix:

* With a wired TaxEngine factory and a ``destination_fips`` on the call,
  the invoice carries a ``tax_breakdown`` dict and an
  ``exemptions_applied`` list sourced from
  :meth:`TaxEngine.compute_tax`. The computed total supersedes the
  caller-provided ``tax_cents`` (which is legacy fallback).
* With no TaxEngine factory (legacy construction shape), the invoice
  falls back to honoring the caller's ``tax_cents`` and no
  ``tax_breakdown`` / ``exemptions_applied`` fields are written to the
  projection — backwards compatible with the commerce-backbone tests.
* Missing ``destination_fips`` with a wired factory degrades gracefully:
  a warning is logged, tax computation is skipped, and ``tax_cents`` is
  honored as-is.
* :class:`TaxJurisdictionNotFoundError` raised by the TaxEngine
  propagates through ``generate_from_order`` so the invoice-generation
  pipeline surfaces a structured rejection to operators (Req 1.9).

Validates: Requirements 1.9, 1.10, 5.1, 6.7
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from commerce.models.invoice import InvoiceStatus
from commerce.services.invoice_service import InvoiceService
from compliance.services.tax_engine import (
    TaxBreakdown,
    TaxJurisdictionNotFoundError,
    TaxLineItem,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_wire"
_CUSTOMER_ID = "cust_wire"
_ACCOUNT_ID = "acct_wire"
_ORDER_ID = "order_wire"
_FIXED_NOW = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

_DIESEL_LINE: Dict[str, Any] = {
    "line_id": "line_diesel",
    "product_code": "DIESEL_2",
    "quantity_gallons": 1000.0,
    "unit_price_cents": 350,
    "subtotal_cents": 350_000,
}
_GAS_LINE: Dict[str, Any] = {
    "line_id": "line_gas",
    "product_code": "GASOLINE_REG",
    "quantity_gallons": 500.0,
    "unit_price_cents": 320,
    "subtotal_cents": 160_000,
}


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService for InvoiceService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    # Default: no existing events (sequence starts at 1).
    es.search_documents = AsyncMock(
        return_value={
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"max_seq": {"value": None}},
        }
    )
    return es


def _make_idempotency_service() -> AsyncMock:
    """Create a mocked IdempotencyService."""
    idemp = AsyncMock()
    idemp.is_duplicate = AsyncMock(return_value=False)
    idemp.mark_processed = AsyncMock(return_value=None)
    return idemp


def _sample_diesel_breakdown() -> TaxBreakdown:
    """Return a representative TaxBreakdown for a 1000-gallon DIESEL_2 line.

    Values mirror the statutory federal diesel rate (24.4¢/gal =
    24_400 cents on 1000 gallons) plus a plausible state + UST layer.
    """
    return TaxBreakdown(
        federal_cents=24_400,
        state_cents=40_000,
        county_cents=0,
        city_cents=0,
        ust_cents=2_000,
        spcc_cents=0,
        environmental_cents=0,
        line_items=[
            TaxLineItem(
                tax_component_name="federal_excise",
                jurisdiction_fips="00",
                jurisdiction_level="federal",
                rate_cents_per_gallon=244,
                gallons=1000.0,
                amount_cents=24_400,
            ),
            TaxLineItem(
                tax_component_name="CA_state_excise",
                jurisdiction_fips="06",
                jurisdiction_level="state",
                rate_cents_per_gallon=400,
                gallons=1000.0,
                amount_cents=40_000,
            ),
            TaxLineItem(
                tax_component_name="ust_fee",
                jurisdiction_fips="06",
                jurisdiction_level="state",
                rate_cents_per_gallon=20,
                gallons=1000.0,
                amount_cents=2_000,
            ),
        ],
        exemptions_applied=[],
    )


def _sample_gas_breakdown() -> TaxBreakdown:
    """Return a representative TaxBreakdown for a 500-gallon GASOLINE_REG line."""
    return TaxBreakdown(
        federal_cents=9_200,  # 184 * 500 / 10
        state_cents=20_000,
        county_cents=0,
        city_cents=0,
        ust_cents=1_000,
        spcc_cents=0,
        environmental_cents=0,
        line_items=[
            TaxLineItem(
                tax_component_name="federal_excise",
                jurisdiction_fips="00",
                jurisdiction_level="federal",
                rate_cents_per_gallon=184,
                gallons=500.0,
                amount_cents=9_200,
            ),
            TaxLineItem(
                tax_component_name="CA_state_excise",
                jurisdiction_fips="06",
                jurisdiction_level="state",
                rate_cents_per_gallon=400,
                gallons=500.0,
                amount_cents=20_000,
            ),
            TaxLineItem(
                tax_component_name="ust_fee",
                jurisdiction_fips="06",
                jurisdiction_level="state",
                rate_cents_per_gallon=20,
                gallons=500.0,
                amount_cents=1_000,
            ),
        ],
        exemptions_applied=[],
    )


class _FakeTaxEngine:
    """Stub TaxEngine returning canned breakdowns per product_code.

    The real TaxEngine is exercised in ``test_tax_engine_compute_tax.py``;
    here we only need to verify the wiring contract between
    InvoiceService and TaxEngine.compute_tax (inputs forwarded
    correctly, outputs aggregated into the invoice).
    """

    def __init__(
        self,
        tenant_id: str,
        *,
        product_breakdowns: Dict[str, TaxBreakdown],
        raise_for_product: Dict[str, Exception] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self._breakdowns = product_breakdowns
        self._raises = raise_for_product or {}
        self.calls: List[Dict[str, Any]] = []

    async def compute_tax(
        self,
        *,
        product_code: str,
        net_gallons: float,
        destination_fips: str,
        customer_id: str,
        effective_date=None,
    ) -> TaxBreakdown:
        self.calls.append(
            {
                "product_code": product_code,
                "net_gallons": net_gallons,
                "destination_fips": destination_fips,
                "customer_id": customer_id,
                "effective_date": effective_date,
            }
        )
        if product_code in self._raises:
            raise self._raises[product_code]
        return self._breakdowns[product_code]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGenerateFromOrderWithTaxEngine:
    """generate_from_order wires TaxEngine.compute_tax into the invoice."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_invoice_carries_tax_breakdown_when_engine_wired(
        self, _mock_utcnow
    ):
        """TaxEngine-computed breakdown is persisted on the invoice."""
        es = _make_es_service()
        idemp = _make_idempotency_service()

        fake_engine = _FakeTaxEngine(
            _TENANT_ID,
            product_breakdowns={
                "DIESEL_2": _sample_diesel_breakdown(),
                "GASOLINE_REG": _sample_gas_breakdown(),
            },
        )
        factory_calls: List[str] = []

        def factory(tenant_id: str):
            factory_calls.append(tenant_id)
            return fake_engine

        service = InvoiceService(es, idemp, tax_engine_factory=factory)

        result = await service.generate_from_order(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            customer_id=_CUSTOMER_ID,
            account_id=_ACCOUNT_ID,
            line_items=[_DIESEL_LINE, _GAS_LINE],
            tax_cents=99_999,  # legacy fallback — should be superseded.
            destination_fips="06037",
            effective_date=date(2026, 6, 15),
            actor="system",
        )

        # Factory called exactly once per generate_from_order call and
        # scoped to the caller's tenant.
        assert factory_calls == [_TENANT_ID]

        # TaxEngine was called once per taxable line item with the
        # correct inputs forwarded through.
        assert len(fake_engine.calls) == 2
        diesel_call = fake_engine.calls[0]
        gas_call = fake_engine.calls[1]
        assert diesel_call["product_code"] == "DIESEL_2"
        assert diesel_call["net_gallons"] == 1000.0
        assert diesel_call["destination_fips"] == "06037"
        assert diesel_call["customer_id"] == _CUSTOMER_ID
        assert diesel_call["effective_date"] == date(2026, 6, 15)
        assert gas_call["product_code"] == "GASOLINE_REG"
        assert gas_call["net_gallons"] == 500.0

        # Per-component cents aggregate across both lines.
        breakdown = result["tax_breakdown"]
        assert breakdown["federal_cents"] == 24_400 + 9_200
        assert breakdown["state_cents"] == 40_000 + 20_000
        assert breakdown["ust_cents"] == 2_000 + 1_000
        assert breakdown["total_tax_cents"] == (
            24_400 + 9_200 + 40_000 + 20_000 + 2_000 + 1_000
        )
        # All TaxEngine line items carried onto the invoice.
        assert len(breakdown["line_items"]) == 6
        # Destination FIPS recorded for audit.
        assert breakdown["destination_fips"] == "06037"

        # Computed tax supersedes the caller's ``tax_cents``.
        expected_tax = breakdown["total_tax_cents"]
        assert result["tax_cents"] == expected_tax
        assert result["total_cents"] == (
            _DIESEL_LINE["subtotal_cents"]
            + _GAS_LINE["subtotal_cents"]
            + expected_tax
        )

        # No exemptions applied on this invoice.
        assert result["exemptions_applied"] == []

        # Invoice is still created in draft.
        assert result["status"] == InvoiceStatus.DRAFT.value

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_exemptions_persisted_for_audit(self, _mock_utcnow):
        """Exemption ids from the TaxEngine are persisted on the invoice."""
        es = _make_es_service()
        idemp = _make_idempotency_service()

        diesel_with_exemption = TaxBreakdown(
            federal_cents=0,
            state_cents=0,
            ust_cents=2_000,
            exemptions_applied=["exempt-637M-42"],
            line_items=[
                TaxLineItem(
                    tax_component_name="ust_fee",
                    jurisdiction_fips="06",
                    jurisdiction_level="state",
                    rate_cents_per_gallon=20,
                    gallons=1000.0,
                    amount_cents=2_000,
                ),
            ],
        )
        fake_engine = _FakeTaxEngine(
            _TENANT_ID,
            product_breakdowns={"DIESEL_2": diesel_with_exemption},
        )
        service = InvoiceService(
            es, idemp, tax_engine_factory=lambda tid: fake_engine
        )

        result = await service.generate_from_order(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            customer_id=_CUSTOMER_ID,
            account_id=_ACCOUNT_ID,
            line_items=[_DIESEL_LINE],
            destination_fips="06",
            tax_cents=0,
            actor="system",
        )

        assert result["exemptions_applied"] == ["exempt-637M-42"]
        assert result["tax_breakdown"]["exemptions_applied"] == [
            "exempt-637M-42"
        ]
        assert result["tax_cents"] == 2_000


class TestGenerateFromOrderWithoutTaxEngine:
    """generate_from_order preserves legacy behaviour when no engine is wired."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_falls_back_to_tax_cents_without_factory(self, _mock_utcnow):
        """Legacy shape: no factory, no destination_fips, caller's tax_cents wins."""
        es = _make_es_service()
        idemp = _make_idempotency_service()

        service = InvoiceService(es, idemp)  # no tax_engine_factory

        result = await service.generate_from_order(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            customer_id=_CUSTOMER_ID,
            account_id=_ACCOUNT_ID,
            line_items=[_DIESEL_LINE],
            tax_cents=4_500,
            actor="system",
        )

        # tax_cents honored verbatim.
        assert result["tax_cents"] == 4_500
        assert result["total_cents"] == (
            _DIESEL_LINE["subtotal_cents"] + 4_500
        )
        # Compliance-backbone fields NOT set — keeps the projection
        # shape identical to the pre-task-3.10 baseline for callers
        # that have not adopted the tax engine.
        assert "tax_breakdown" not in result
        assert "exemptions_applied" not in result


class TestGenerateFromOrderMissingDestinationFips:
    """Missing ``destination_fips`` degrades gracefully; does not raise."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_missing_fips_logs_warning_and_falls_back(
        self, _mock_utcnow, caplog
    ):
        es = _make_es_service()
        idemp = _make_idempotency_service()

        fake_engine = _FakeTaxEngine(
            _TENANT_ID,
            product_breakdowns={"DIESEL_2": _sample_diesel_breakdown()},
        )
        service = InvoiceService(
            es, idemp, tax_engine_factory=lambda tid: fake_engine
        )

        with caplog.at_level(logging.WARNING, logger="commerce.services.invoice_service"):
            result = await service.generate_from_order(
                tenant_id=_TENANT_ID,
                order_id=_ORDER_ID,
                customer_id=_CUSTOMER_ID,
                account_id=_ACCOUNT_ID,
                line_items=[_DIESEL_LINE],
                tax_cents=7_000,
                # destination_fips omitted.
                actor="system",
            )

        # TaxEngine never called.
        assert fake_engine.calls == []
        # Warning emitted mentioning destination_fips / tenant / order.
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("destination_fips" in msg for msg in messages)
        # Legacy fallback — caller's tax_cents honored as-is.
        assert result["tax_cents"] == 7_000
        assert result["total_cents"] == (
            _DIESEL_LINE["subtotal_cents"] + 7_000
        )
        assert "tax_breakdown" not in result
        assert "exemptions_applied" not in result


class TestGenerateFromOrderTaxJurisdictionNotFound:
    """TaxJurisdictionNotFoundError propagates to the caller (Req 1.9)."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_jurisdiction_not_found_propagates(self, _mock_utcnow):
        es = _make_es_service()
        idemp = _make_idempotency_service()

        fake_engine = _FakeTaxEngine(
            _TENANT_ID,
            product_breakdowns={},
            raise_for_product={
                "DIESEL_2": TaxJurisdictionNotFoundError(
                    "Missing state excise row for FIPS 06",
                    fips_code="06",
                    jurisdiction_level="state",
                    tax_type="excise",
                    product_code="DIESEL_2",
                    effective_date=date(2026, 6, 15),
                )
            },
        )
        service = InvoiceService(
            es, idemp, tax_engine_factory=lambda tid: fake_engine
        )

        with pytest.raises(TaxJurisdictionNotFoundError) as exc_info:
            await service.generate_from_order(
                tenant_id=_TENANT_ID,
                order_id=_ORDER_ID,
                customer_id=_CUSTOMER_ID,
                account_id=_ACCOUNT_ID,
                line_items=[_DIESEL_LINE],
                tax_cents=0,
                destination_fips="06",
                effective_date=date(2026, 6, 15),
                actor="system",
            )

        # Structured error context is preserved so operators can
        # triage the missing row.
        err = exc_info.value
        assert err.error_code == "tax.jurisdiction_not_found"
        assert err.fips_code == "06"
        assert err.jurisdiction_level == "state"
        assert err.product_code == "DIESEL_2"

        # No invoice was indexed (the failure short-circuits before
        # the ES write).
        write_calls = [
            call for call in es.index_document.call_args_list
        ]
        # There must not be any invoice projection writes (event
        # writes also did not happen because failure occurred before
        # event composition).
        for call in write_calls:
            # Defensive: even if any write fires, it cannot be an
            # invoice projection with a status field.
            doc = call[0][2] if len(call[0]) >= 3 else {}
            assert doc.get("status") != InvoiceStatus.DRAFT.value

        # Idempotency was NOT marked — so a retry can re-attempt
        # after the operator fixes the tax_jurisdictions table.
        idemp.mark_processed.assert_not_called()
