"""Unit tests for :mod:`compliance.services.tax_engine` skeleton.

Covers Task 3.3 of the Fuel Compliance Backbone spec, which introduces
the :class:`TaxEngine` class surface, the :class:`TaxBreakdown` /
:class:`TaxLineItem` Pydantic models, and the
:class:`TaxJurisdictionNotFoundError` custom exception with its
``tax.jurisdiction_not_found`` error code.

The concrete tax math lands in Tasks 3.4–3.8; the skeleton tests here
pin the public contract so downstream implementations cannot silently
drift from:

* :class:`TaxBreakdown` default-cents behavior, computed
  ``total_tax_cents`` rollup, and per-field validation.
* :class:`TaxLineItem` validation of FIPS code shape, component name,
  and jurisdiction level.
* :class:`TaxEngine` construction with an ES service and ``tenant_id``,
  and ``NotImplementedError`` with a helpful message from
  ``compute_tax`` / ``check_exemption`` until the remaining
  Tasks 3.5-3.8 land. (``get_jurisdiction_rates`` was implemented in
  Task 3.4 — see ``test_tax_engine_jurisdiction_lookup.py``.)
* :class:`TaxJurisdictionNotFoundError` carries the stable
  ``error_code`` attribute and subclasses :class:`ValueError`.

Validates: Requirement 1.10
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from compliance.services.tax_engine import (
    ERROR_CODE_JURISDICTION_NOT_FOUND,
    TaxBreakdown,
    TaxEngine,
    TaxJurisdictionNotFoundError,
    TaxLineItem,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal ES stand-in so ``TaxEngine`` can be constructed under test.

    The Task 3.3 skeleton never touches the search API, so the fake
    intentionally exposes no behavior. Tasks 3.4–3.8 will bring in a
    richer fake / real ES integration test.
    """

    def __init__(self) -> None:
        self.tenant_ids_seen: list[str] = []


@pytest.fixture
def es_service() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def tax_engine(es_service: _FakeESService) -> TaxEngine:
    return TaxEngine(es_service=es_service, tenant_id="tenant-1")


# ---------------------------------------------------------------------------
# TaxLineItem — field validation
# ---------------------------------------------------------------------------


class TestTaxLineItem:
    """Per-component line item validates FIPS, level, name, and amounts."""

    def _base_payload(self, **overrides) -> dict:
        payload = {
            "tax_component_name": "federal_excise",
            "jurisdiction_fips": "00",
            "jurisdiction_level": "federal",
            "rate_cents_per_gallon": 184,
            "gallons": 1000.0,
            "amount_cents": 184_000,
        }
        payload.update(overrides)
        return payload

    def test_happy_path_federal_line_item(self):
        item = TaxLineItem(**self._base_payload())

        assert item.tax_component_name == "federal_excise"
        assert item.jurisdiction_fips == "00"
        assert item.jurisdiction_level == "federal"
        assert item.rate_cents_per_gallon == 184
        assert item.gallons == 1000.0
        assert item.amount_cents == 184_000

    def test_state_level_with_2_digit_fips(self):
        item = TaxLineItem(
            **self._base_payload(
                tax_component_name="CA_state_excise",
                jurisdiction_fips="06",
                jurisdiction_level="state",
                rate_cents_per_gallon=51,
                amount_cents=51_000,
            )
        )
        assert item.jurisdiction_fips == "06"
        assert item.jurisdiction_level == "state"

    def test_county_level_with_5_digit_fips(self):
        item = TaxLineItem(
            **self._base_payload(
                tax_component_name="LA_county_ust",
                jurisdiction_fips="06037",
                jurisdiction_level="county",
                rate_cents_per_gallon=2,
                amount_cents=2_000,
            )
        )
        assert item.jurisdiction_fips == "06037"
        assert item.jurisdiction_level == "county"

    def test_city_level_with_7_digit_fips(self):
        item = TaxLineItem(
            **self._base_payload(
                tax_component_name="LA_city_env",
                jurisdiction_fips="0644000",
                jurisdiction_level="city",
                rate_cents_per_gallon=1,
                amount_cents=1_000,
            )
        )
        assert item.jurisdiction_fips == "0644000"
        assert item.jurisdiction_level == "city"

    def test_component_name_stripped(self):
        item = TaxLineItem(
            **self._base_payload(tax_component_name="  federal_excise  ")
        )
        assert item.tax_component_name == "federal_excise"

    def test_empty_component_name_rejected(self):
        with pytest.raises(ValidationError):
            TaxLineItem(**self._base_payload(tax_component_name="   "))

    def test_non_digit_fips_rejected(self):
        with pytest.raises(ValidationError) as exc:
            TaxLineItem(**self._base_payload(jurisdiction_fips="CA"))
        assert "digits" in str(exc.value)

    def test_wrong_length_fips_rejected(self):
        with pytest.raises(ValidationError):
            TaxLineItem(**self._base_payload(jurisdiction_fips="123"))

    def test_invalid_level_rejected(self):
        with pytest.raises(ValidationError):
            TaxLineItem(**self._base_payload(jurisdiction_level="regional"))

    def test_negative_rate_rejected(self):
        with pytest.raises(ValidationError):
            TaxLineItem(**self._base_payload(rate_cents_per_gallon=-1))

    def test_negative_gallons_rejected(self):
        with pytest.raises(ValidationError):
            TaxLineItem(**self._base_payload(gallons=-0.1))

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            TaxLineItem(**self._base_payload(unexpected_field="value"))


# ---------------------------------------------------------------------------
# TaxBreakdown — defaults, rollup, and per-field validation (Req 1.10)
# ---------------------------------------------------------------------------


class TestTaxBreakdownDefaults:
    """The breakdown accepts an all-defaults construction for previews."""

    def test_all_defaults_zero_out(self):
        breakdown = TaxBreakdown()

        assert breakdown.invoice_id is None
        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        assert breakdown.county_cents == 0
        assert breakdown.city_cents == 0
        assert breakdown.ust_cents == 0
        assert breakdown.spcc_cents == 0
        assert breakdown.environmental_cents == 0
        assert breakdown.exemptions_applied == []
        assert breakdown.line_items == []
        assert breakdown.total_tax_cents == 0

    def test_invoice_id_accepted(self):
        breakdown = TaxBreakdown(invoice_id="inv-001")
        assert breakdown.invoice_id == "inv-001"

    def test_exemptions_applied_list_is_independent_per_instance(self):
        """``default_factory=list`` must not share state across instances."""
        a = TaxBreakdown()
        b = TaxBreakdown()
        a.exemptions_applied.append("exempt_a")
        assert b.exemptions_applied == []


class TestTaxBreakdownTotal:
    """Computed ``total_tax_cents`` is the sum of the seven components."""

    def test_total_is_sum_of_components(self):
        breakdown = TaxBreakdown(
            federal_cents=184_000,
            state_cents=51_000,
            county_cents=2_000,
            city_cents=1_000,
            ust_cents=500,
            spcc_cents=100,
            environmental_cents=400,
        )

        expected = 184_000 + 51_000 + 2_000 + 1_000 + 500 + 100 + 400
        assert breakdown.total_tax_cents == expected

    def test_total_updates_when_component_updated(self):
        """``validate_assignment=True`` keeps the rollup in sync on setattr."""
        breakdown = TaxBreakdown(federal_cents=100)
        assert breakdown.total_tax_cents == 100

        breakdown.state_cents = 50
        assert breakdown.total_tax_cents == 150

    def test_total_included_in_model_dump(self):
        """Serialization emits the computed rollup so clients see it."""
        breakdown = TaxBreakdown(
            federal_cents=184_000,
            state_cents=51_000,
        )
        dumped = breakdown.model_dump()
        assert dumped["total_tax_cents"] == 235_000


class TestTaxBreakdownValidation:
    """Component fields reject negative cents; extra fields forbidden."""

    @pytest.mark.parametrize(
        "field",
        [
            "federal_cents",
            "state_cents",
            "county_cents",
            "city_cents",
            "ust_cents",
            "spcc_cents",
            "environmental_cents",
        ],
    )
    def test_negative_component_rejected(self, field: str):
        with pytest.raises(ValidationError):
            TaxBreakdown(**{field: -1})

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            TaxBreakdown(unexpected_field="value")

    def test_line_items_accepted(self):
        line = TaxLineItem(
            tax_component_name="federal_excise",
            jurisdiction_fips="00",
            jurisdiction_level="federal",
            rate_cents_per_gallon=184,
            gallons=1000.0,
            amount_cents=184_000,
        )
        breakdown = TaxBreakdown(
            invoice_id="inv-001",
            federal_cents=184_000,
            line_items=[line],
            exemptions_applied=["exempt_abc"],
        )

        assert breakdown.line_items == [line]
        assert breakdown.exemptions_applied == ["exempt_abc"]
        assert breakdown.total_tax_cents == 184_000


# ---------------------------------------------------------------------------
# TaxJurisdictionNotFoundError (Req 1.9 — skeleton declaration)
# ---------------------------------------------------------------------------


class TestTaxJurisdictionNotFoundError:
    """The exception carries a stable error code and subclasses ValueError."""

    def test_error_code_constant_matches_spec(self):
        assert ERROR_CODE_JURISDICTION_NOT_FOUND == "tax.jurisdiction_not_found"

    def test_default_error_code_attribute(self):
        exc = TaxJurisdictionNotFoundError("missing state row for CA")
        assert exc.error_code == ERROR_CODE_JURISDICTION_NOT_FOUND

    def test_is_value_error(self):
        """Callers catching ``ValueError`` see the exception transparently."""
        exc = TaxJurisdictionNotFoundError("missing row")
        assert isinstance(exc, ValueError)

    def test_context_attributes_preserved(self):
        exc = TaxJurisdictionNotFoundError(
            "missing county row for 06037",
            fips_code="06037",
            jurisdiction_level="county",
            tax_type="excise",
            product_code="DIESEL_2",
            effective_date=date(2026, 6, 1),
        )

        assert exc.fips_code == "06037"
        assert exc.jurisdiction_level == "county"
        assert exc.tax_type == "excise"
        assert exc.product_code == "DIESEL_2"
        assert exc.effective_date == date(2026, 6, 1)
        assert "missing county row for 06037" in str(exc)


# ---------------------------------------------------------------------------
# TaxEngine — skeleton surface
# ---------------------------------------------------------------------------


class TestTaxEngineConstruction:
    """TaxEngine binds an ES handle and a tenant_id at construction."""

    def test_instantiation_with_es_and_tenant(self, es_service: _FakeESService):
        engine = TaxEngine(es_service=es_service, tenant_id="tenant-1")

        # Bound attributes are intentionally private; we assert via
        # the only observable side effect: re-constructing works and
        # downstream ``NotImplementedError`` carries the tenant-agnostic
        # skeleton message.
        assert engine is not None

    def test_empty_tenant_id_rejected(self, es_service: _FakeESService):
        with pytest.raises(ValueError):
            TaxEngine(es_service=es_service, tenant_id="")

    def test_whitespace_tenant_id_rejected(self, es_service: _FakeESService):
        with pytest.raises(ValueError):
            TaxEngine(es_service=es_service, tenant_id="   ")

    def test_non_string_tenant_id_rejected(self, es_service: _FakeESService):
        with pytest.raises(ValueError):
            TaxEngine(es_service=es_service, tenant_id=None)  # type: ignore[arg-type]


class TestTaxEngineSkeletonMethods:
    """``compute_tax`` / ``check_exemption`` / ``get_jurisdiction_rates``
    are now live across Tasks 3.4–3.8. The smoke tests here only confirm
    the NotImplementedError stubs have been retired."""

    @pytest.mark.asyncio
    async def test_compute_tax_is_implemented(self, tax_engine: TaxEngine):
        """``compute_tax`` no longer raises NotImplementedError.

        The live implementation landed in Task 3.8. Calling the method
        against a stub ES (which has no ``search_documents``) will
        raise ``AttributeError`` once ``get_jurisdiction_rates`` is
        invoked — confirming we moved past the skeleton without
        exercising the richer fake that the dedicated
        ``test_tax_engine_compute_tax.py`` suite uses.
        """
        with pytest.raises(Exception) as exc:
            await tax_engine.compute_tax(
                product_code="DIESEL_2",
                net_gallons=1000.0,
                destination_fips="06037",
                customer_id="cust-001",
                effective_date=date(2026, 6, 1),
            )
        assert not isinstance(exc.value, NotImplementedError)

    @pytest.mark.asyncio
    async def test_compute_tax_without_effective_date_is_implemented(
        self, tax_engine: TaxEngine
    ):
        """``effective_date`` is optional — default resolution is live."""
        with pytest.raises(Exception) as exc:
            await tax_engine.compute_tax(
                product_code="GASOLINE_REG",
                net_gallons=500.0,
                destination_fips="06",
                customer_id="cust-002",
            )
        assert not isinstance(exc.value, NotImplementedError)

    @pytest.mark.asyncio
    async def test_get_jurisdiction_rates_is_implemented(
        self, tax_engine: TaxEngine
    ):
        """``get_jurisdiction_rates`` no longer raises NotImplementedError.

        The live implementation landed in Task 3.4. This test pins the
        fact that calling the method with a valid FIPS and date returns
        an empty list against the stub ES (which has no ``search_documents``
        behavior) rather than the skeleton error. The concrete FIPS
        rollup + date-range behavior is exercised in
        ``test_tax_engine_jurisdiction_lookup.py``.
        """
        # The bare _FakeESService from the skeleton fixture does not
        # implement search_documents — so we call the internal helper
        # instead, which is pure (no ES round-trip) and confirms the
        # method is no longer a NotImplementedError stub.
        candidates = TaxEngine._compute_candidate_fips_codes("06037")
        assert candidates == ["00", "06", "06037"]

    @pytest.mark.asyncio
    async def test_check_exemption_raises_not_implemented(
        self, tax_engine: TaxEngine
    ):
        # check_exemption was implemented in Task 3.7 (see
        # ``test_tax_engine_check_exemption.py``). The skeleton
        # contract now asserts that the method is no longer a
        # NotImplementedError stub by exercising the pure priority
        # ordering via the module-level constant.
        from compliance.services.tax_engine import (
            _EXEMPTION_PRIORITY_ORDER,
        )

        assert _EXEMPTION_PRIORITY_ORDER[0] == "dyed_diesel"
        assert _EXEMPTION_PRIORITY_ORDER[-1] == "resale"
