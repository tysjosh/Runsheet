"""Unit tests for :class:`commerce.models.price_protection_contract.PriceProtectionContract`.

Covers Task 4.1 of the Fuel Compliance Backbone spec, which validates
Requirements 3.1 (three supported contract types: fixed_price, cap_price,
collar) and 3.2 (required per-contract-type pricing parameters and volume
tracking fields).

The tests assert:
- Happy-path construction for each contract_type.
- ``contract_type``-driven pricing-parameter validation (fixed_price /
  cap_price / collar required and forbidden fields).
- Collar ``price_floor_cents <= price_cap_cents`` cross-field check.
- ``end_date >= start_date`` cross-field check.
- ``contracted_gallons > 0`` and ``remaining_gallons`` invariants
  (default, non-negative, <= contracted).
- Non-negative integer cents on every cents field (Constraint C1).
- Optional text normalization and schema hygiene (``extra="forbid"``).
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from commerce.models.price_protection_contract import PriceProtectionContract


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    """Baseline fixed_price contract payload; override per-test as needed."""
    payload = {
        "tenant_id": "tenant-1",
        "customer_id": "cust-1",
        "account_id": "acct-1",
        "product_code": "HEATING_OIL",
        "contract_type": "fixed_price",
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 12, 31),
        "contracted_gallons": 10_000.0,
        "fixed_price_cents": 325,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path — one per contract_type (Req 3.1)
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Each of the three contract types constructs with its required fields."""

    def test_fixed_price_contract(self):
        contract = PriceProtectionContract(**_base_payload())

        assert contract.contract_type == "fixed_price"
        assert contract.fixed_price_cents == 325
        assert contract.price_cap_cents is None
        assert contract.price_floor_cents is None
        # remaining_gallons defaults to contracted_gallons.
        assert contract.remaining_gallons == contract.contracted_gallons
        assert contract.status == "active"
        assert contract.version == 0
        assert contract.contract_id.startswith("contract_")

    def test_cap_price_contract(self):
        contract = PriceProtectionContract(
            **_base_payload(
                contract_type="cap_price",
                fixed_price_cents=None,
                price_cap_cents=340,
            )
        )

        assert contract.contract_type == "cap_price"
        assert contract.price_cap_cents == 340
        assert contract.fixed_price_cents is None
        assert contract.price_floor_cents is None

    def test_collar_contract(self):
        contract = PriceProtectionContract(
            **_base_payload(
                contract_type="collar",
                fixed_price_cents=None,
                price_cap_cents=340,
                price_floor_cents=290,
            )
        )

        assert contract.contract_type == "collar"
        assert contract.price_cap_cents == 340
        assert contract.price_floor_cents == 290
        assert contract.fixed_price_cents is None

    def test_collar_equal_cap_and_floor_is_accepted(self):
        """A collar with floor == cap is a legitimate degenerate fixed-price."""
        contract = PriceProtectionContract(
            **_base_payload(
                contract_type="collar",
                fixed_price_cents=None,
                price_cap_cents=300,
                price_floor_cents=300,
            )
        )

        assert contract.price_cap_cents == contract.price_floor_cents == 300

    def test_explicit_remaining_gallons_is_preserved(self):
        contract = PriceProtectionContract(
            **_base_payload(
                contracted_gallons=10_000.0,
                remaining_gallons=7_500.0,
            )
        )

        assert contract.contracted_gallons == 10_000.0
        assert contract.remaining_gallons == 7_500.0


# ---------------------------------------------------------------------------
# contract_type-driven validator paths (Req 3.1, 3.2)
# ---------------------------------------------------------------------------


class TestFixedPriceValidators:
    """fixed_price requires fixed_price_cents; rejects cap/floor."""

    def test_missing_fixed_price_cents_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(**_base_payload(fixed_price_cents=None))

        assert "fixed_price_cents" in str(exc_info.value)

    def test_price_cap_cents_forbidden(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    fixed_price_cents=325,
                    price_cap_cents=340,
                )
            )

        assert "price_cap_cents" in str(exc_info.value)

    def test_price_floor_cents_forbidden(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    fixed_price_cents=325,
                    price_floor_cents=290,
                )
            )

        assert "price_floor_cents" in str(exc_info.value)


class TestCapPriceValidators:
    """cap_price requires price_cap_cents; rejects fixed/floor."""

    def test_missing_price_cap_cents_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contract_type="cap_price",
                    fixed_price_cents=None,
                    price_cap_cents=None,
                )
            )

        assert "price_cap_cents" in str(exc_info.value)

    def test_fixed_price_cents_forbidden(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contract_type="cap_price",
                    fixed_price_cents=325,
                    price_cap_cents=340,
                )
            )

        assert "fixed_price_cents" in str(exc_info.value)

    def test_price_floor_cents_forbidden(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contract_type="cap_price",
                    fixed_price_cents=None,
                    price_cap_cents=340,
                    price_floor_cents=290,
                )
            )

        assert "price_floor_cents" in str(exc_info.value)


class TestCollarValidators:
    """collar requires both cap and floor; floor <= cap; rejects fixed."""

    def test_missing_price_cap_cents_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contract_type="collar",
                    fixed_price_cents=None,
                    price_cap_cents=None,
                    price_floor_cents=290,
                )
            )

        assert "price_cap_cents" in str(exc_info.value)

    def test_missing_price_floor_cents_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contract_type="collar",
                    fixed_price_cents=None,
                    price_cap_cents=340,
                    price_floor_cents=None,
                )
            )

        assert "price_floor_cents" in str(exc_info.value)

    def test_fixed_price_cents_forbidden(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contract_type="collar",
                    fixed_price_cents=325,
                    price_cap_cents=340,
                    price_floor_cents=290,
                )
            )

        assert "fixed_price_cents" in str(exc_info.value)

    def test_floor_greater_than_cap_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contract_type="collar",
                    fixed_price_cents=None,
                    price_cap_cents=300,
                    price_floor_cents=350,
                )
            )

        assert "price_floor_cents" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Effective window (Req 3.2)
# ---------------------------------------------------------------------------


class TestEffectiveWindow:
    """``end_date`` must not precede ``start_date``."""

    def test_end_before_start_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    start_date=date(2026, 6, 1),
                    end_date=date(2026, 5, 31),
                )
            )

        assert "end_date" in str(exc_info.value)

    def test_same_day_window_is_allowed(self):
        """A one-day contract is legitimate (single-delivery override)."""
        contract = PriceProtectionContract(
            **_base_payload(
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 1),
            )
        )

        assert contract.start_date == contract.end_date


# ---------------------------------------------------------------------------
# Volume tracking invariants (Req 3.2, 3.4)
# ---------------------------------------------------------------------------


class TestVolumeTracking:
    """``contracted_gallons > 0`` and ``0 <= remaining_gallons <= contracted``."""

    def test_zero_contracted_gallons_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(**_base_payload(contracted_gallons=0.0))

        assert "contracted_gallons" in str(exc_info.value)

    def test_negative_contracted_gallons_rejected(self):
        with pytest.raises(ValidationError):
            PriceProtectionContract(**_base_payload(contracted_gallons=-1.0))

    def test_negative_remaining_gallons_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contracted_gallons=10_000.0,
                    remaining_gallons=-0.1,
                )
            )

        assert "remaining_gallons" in str(exc_info.value)

    def test_remaining_gallons_above_contracted_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PriceProtectionContract(
                **_base_payload(
                    contracted_gallons=10_000.0,
                    remaining_gallons=10_001.0,
                )
            )

        assert "remaining_gallons" in str(exc_info.value)

    def test_remaining_gallons_defaults_to_contracted(self):
        contract = PriceProtectionContract(
            **_base_payload(contracted_gallons=5_000.0)
        )
        assert contract.remaining_gallons == 5_000.0

    def test_zero_remaining_gallons_is_allowed(self):
        """A contract may persist with zero remaining before status transitions."""
        contract = PriceProtectionContract(
            **_base_payload(
                contracted_gallons=10_000.0,
                remaining_gallons=0.0,
            )
        )
        assert contract.remaining_gallons == 0.0


# ---------------------------------------------------------------------------
# Integer-cents fields (Constraint C1)
# ---------------------------------------------------------------------------


class TestCentsFieldRanges:
    """All cents fields must be non-negative when provided."""

    def test_negative_fixed_price_cents_rejected(self):
        with pytest.raises(ValidationError):
            PriceProtectionContract(**_base_payload(fixed_price_cents=-1))

    def test_negative_cap_cents_rejected(self):
        with pytest.raises(ValidationError):
            PriceProtectionContract(
                **_base_payload(
                    contract_type="cap_price",
                    fixed_price_cents=None,
                    price_cap_cents=-1,
                )
            )

    def test_negative_floor_cents_rejected(self):
        with pytest.raises(ValidationError):
            PriceProtectionContract(
                **_base_payload(
                    contract_type="collar",
                    fixed_price_cents=None,
                    price_cap_cents=340,
                    price_floor_cents=-1,
                )
            )

    def test_zero_fixed_price_cents_allowed(self):
        """A zero-cent fixed price is a legitimate promotional override."""
        contract = PriceProtectionContract(
            **_base_payload(fixed_price_cents=0)
        )
        assert contract.fixed_price_cents == 0


# ---------------------------------------------------------------------------
# Version counter (OCC — Task 4.3)
# ---------------------------------------------------------------------------


class TestVersionCounter:
    """``version`` defaults to 0 and rejects negatives."""

    def test_version_defaults_to_zero(self):
        contract = PriceProtectionContract(**_base_payload())
        assert contract.version == 0

    def test_negative_version_rejected(self):
        with pytest.raises(ValidationError):
            PriceProtectionContract(**_base_payload(version=-1))


# ---------------------------------------------------------------------------
# Optional text normalization
# ---------------------------------------------------------------------------


class TestOptionalTextNormalization:
    """``notes`` is stripped; all-whitespace collapses to ``None``."""

    def test_notes_is_stripped(self):
        contract = PriceProtectionContract(
            **_base_payload(notes="  winter pre-buy  ")
        )
        assert contract.notes == "winter pre-buy"

    def test_whitespace_only_notes_becomes_none(self):
        contract = PriceProtectionContract(**_base_payload(notes="   "))
        assert contract.notes is None


# ---------------------------------------------------------------------------
# Schema hygiene
# ---------------------------------------------------------------------------


class TestSchemaHygiene:
    """Unknown fields are forbidden and enum-typed fields reject unknowns."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            PriceProtectionContract(**_base_payload(unexpected_field="x"))

    def test_invalid_contract_type_rejected(self):
        with pytest.raises(ValidationError):
            PriceProtectionContract(**_base_payload(contract_type="swap"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            PriceProtectionContract(**_base_payload(status="pending"))
