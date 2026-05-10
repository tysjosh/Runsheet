"""Unit tests for :class:`commerce.models.pricing_rule.PricingRule`.

Covers Task 5.1 of the Fuel Compliance Backbone spec, which validates
Requirement 11.1 (four supported pricing strategies: posted_price,
rack_plus_margin, tiered_volume, cost_plus, with strategy-specific
required parameters).

The tests assert:
- Happy-path construction for each of the four strategies.
- Strategy-driven required-field validation (posted_price_cents,
  margin_cents, terminal_id, freight_rate_cents_per_mile, tier_thresholds).
- ``tier_thresholds`` structural invariants (non-empty, sorted,
  non-overlapping, only-last-tier unbounded).
- ``expiry_date >= effective_date`` cross-field check.
- Non-negative integer cents on every cents field (Constraint C1).
- Optional keyword stripping (customer_id, account_id, terminal_id).
- ``product_code`` non-empty requirement and schema hygiene
  (``extra="forbid"``, invalid enum rejection).
- ``rule_id`` default shape ``rule_<uuid4>`` and ``tenant_id`` scoping.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from commerce.models.pricing_rule import PricingRule, TierBreak


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    """Baseline posted_price rule payload; override per-test as needed."""
    payload = {
        "tenant_id": "tenant-1",
        "customer_id": "cust-1",
        "account_id": "acct-1",
        "product_code": "HEATING_OIL",
        "strategy": "posted_price",
        "posted_price_cents": 325,
        "priority": 10,
        "effective_date": date(2026, 1, 1),
        "expiry_date": date(2026, 12, 31),
    }
    payload.update(overrides)
    return payload


def _simple_tiers() -> list[dict]:
    """Three sorted, non-overlapping tiers with an unbounded top tier."""
    return [
        {"min_gallons": 0.0, "max_gallons": 500.0, "unit_price_cents": 340},
        {"min_gallons": 500.0, "max_gallons": 2000.0, "unit_price_cents": 325},
        {"min_gallons": 2000.0, "max_gallons": None, "unit_price_cents": 310},
    ]


# ---------------------------------------------------------------------------
# Happy path — one per strategy (Req 11.1)
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Each of the four strategies constructs with its required fields."""

    def test_posted_price_strategy(self):
        rule = PricingRule(**_base_payload())

        assert rule.strategy == "posted_price"
        assert rule.posted_price_cents == 325
        assert rule.status == "active"
        assert rule.rule_id.startswith("rule_")
        assert rule.tenant_id == "tenant-1"
        assert rule.customer_id == "cust-1"

    def test_rack_plus_margin_strategy(self):
        rule = PricingRule(
            **_base_payload(
                strategy="rack_plus_margin",
                posted_price_cents=None,
                margin_cents=15,
                terminal_id="term-chi-01",
            )
        )

        assert rule.strategy == "rack_plus_margin"
        assert rule.margin_cents == 15
        assert rule.terminal_id == "term-chi-01"

    def test_tiered_volume_strategy(self):
        rule = PricingRule(
            **_base_payload(
                strategy="tiered_volume",
                posted_price_cents=None,
                tier_thresholds=_simple_tiers(),
            )
        )

        assert rule.strategy == "tiered_volume"
        assert len(rule.tier_thresholds) == 3
        assert rule.tier_thresholds[0].unit_price_cents == 340
        assert rule.tier_thresholds[-1].max_gallons is None

    def test_cost_plus_strategy(self):
        rule = PricingRule(
            **_base_payload(
                strategy="cost_plus",
                posted_price_cents=None,
                margin_cents=12,
                freight_rate_cents_per_mile=3,
                terminal_id="term-ny-05",
            )
        )

        assert rule.strategy == "cost_plus"
        assert rule.margin_cents == 12
        assert rule.freight_rate_cents_per_mile == 3

    def test_product_default_rule_has_null_customer(self):
        """A product-default rule sets customer_id=None (Req 11.2)."""
        rule = PricingRule(
            **_base_payload(customer_id=None, account_id=None)
        )
        assert rule.customer_id is None
        assert rule.account_id is None

    def test_expiry_date_is_optional(self):
        """Rules without an expiry date are active indefinitely."""
        rule = PricingRule(**_base_payload(expiry_date=None))
        assert rule.expiry_date is None


# ---------------------------------------------------------------------------
# Strategy-driven required-field validators (Req 11.1)
# ---------------------------------------------------------------------------


class TestPostedPriceValidators:
    """posted_price requires posted_price_cents."""

    def test_missing_posted_price_cents_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(**_base_payload(posted_price_cents=None))
        assert "posted_price_cents" in str(exc_info.value)


class TestRackPlusMarginValidators:
    """rack_plus_margin requires margin_cents and terminal_id."""

    def test_missing_margin_cents_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="rack_plus_margin",
                    posted_price_cents=None,
                    margin_cents=None,
                    terminal_id="term-chi-01",
                )
            )
        assert "margin_cents" in str(exc_info.value)

    def test_missing_terminal_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="rack_plus_margin",
                    posted_price_cents=None,
                    margin_cents=15,
                    terminal_id=None,
                )
            )
        assert "terminal_id" in str(exc_info.value)

    def test_whitespace_only_terminal_id_treated_as_missing(self):
        """Whitespace-only terminal_id is stripped to None and rejected."""
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="rack_plus_margin",
                    posted_price_cents=None,
                    margin_cents=15,
                    terminal_id="   ",
                )
            )
        assert "terminal_id" in str(exc_info.value)


class TestTieredVolumeValidators:
    """tiered_volume requires non-empty tier_thresholds."""

    def test_missing_tier_thresholds_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="tiered_volume",
                    posted_price_cents=None,
                    tier_thresholds=None,
                )
            )
        assert "tier_thresholds" in str(exc_info.value)

    def test_empty_tier_thresholds_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="tiered_volume",
                    posted_price_cents=None,
                    tier_thresholds=[],
                )
            )
        assert "tier_thresholds" in str(exc_info.value)

    def test_single_unbounded_tier_accepted(self):
        """A single tier with max_gallons=None is a legitimate top-only rule."""
        rule = PricingRule(
            **_base_payload(
                strategy="tiered_volume",
                posted_price_cents=None,
                tier_thresholds=[
                    {
                        "min_gallons": 0.0,
                        "max_gallons": None,
                        "unit_price_cents": 300,
                    }
                ],
            )
        )
        assert len(rule.tier_thresholds) == 1
        assert rule.tier_thresholds[0].max_gallons is None

    def test_overlapping_tiers_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="tiered_volume",
                    posted_price_cents=None,
                    tier_thresholds=[
                        {
                            "min_gallons": 0.0,
                            "max_gallons": 1000.0,
                            "unit_price_cents": 340,
                        },
                        {
                            "min_gallons": 500.0,
                            "max_gallons": 2000.0,
                            "unit_price_cents": 325,
                        },
                    ],
                )
            )
        assert "tier_thresholds" in str(exc_info.value)

    def test_non_final_unbounded_tier_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="tiered_volume",
                    posted_price_cents=None,
                    tier_thresholds=[
                        {
                            "min_gallons": 0.0,
                            "max_gallons": None,
                            "unit_price_cents": 340,
                        },
                        {
                            "min_gallons": 500.0,
                            "max_gallons": 2000.0,
                            "unit_price_cents": 325,
                        },
                    ],
                )
            )
        assert "unbounded" in str(exc_info.value) or "max_gallons" in str(
            exc_info.value
        )

    def test_zero_width_tier_rejected(self):
        with pytest.raises(ValidationError):
            TierBreak(
                min_gallons=500.0,
                max_gallons=500.0,
                unit_price_cents=300,
            )

    def test_inverted_tier_rejected(self):
        with pytest.raises(ValidationError):
            TierBreak(
                min_gallons=500.0,
                max_gallons=100.0,
                unit_price_cents=300,
            )

    def test_negative_tier_min_rejected(self):
        with pytest.raises(ValidationError):
            TierBreak(
                min_gallons=-0.1,
                max_gallons=100.0,
                unit_price_cents=300,
            )

    def test_negative_tier_unit_price_rejected(self):
        with pytest.raises(ValidationError):
            TierBreak(
                min_gallons=0.0,
                max_gallons=100.0,
                unit_price_cents=-1,
            )


class TestCostPlusValidators:
    """cost_plus requires margin_cents and freight_rate_cents_per_mile."""

    def test_missing_margin_cents_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="cost_plus",
                    posted_price_cents=None,
                    margin_cents=None,
                    freight_rate_cents_per_mile=3,
                )
            )
        assert "margin_cents" in str(exc_info.value)

    def test_missing_freight_rate_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    strategy="cost_plus",
                    posted_price_cents=None,
                    margin_cents=12,
                    freight_rate_cents_per_mile=None,
                )
            )
        assert "freight_rate_cents_per_mile" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Effective window
# ---------------------------------------------------------------------------


class TestEffectiveWindow:
    """``expiry_date`` must not precede ``effective_date`` when provided."""

    def test_expiry_before_effective_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingRule(
                **_base_payload(
                    effective_date=date(2026, 6, 1),
                    expiry_date=date(2026, 5, 31),
                )
            )
        assert "expiry_date" in str(exc_info.value)

    def test_same_day_window_allowed(self):
        """A single-day rule is legitimate (promotional override)."""
        rule = PricingRule(
            **_base_payload(
                effective_date=date(2026, 7, 1),
                expiry_date=date(2026, 7, 1),
            )
        )
        assert rule.effective_date == rule.expiry_date


# ---------------------------------------------------------------------------
# Integer-cents fields (Constraint C1)
# ---------------------------------------------------------------------------


class TestCentsFieldRanges:
    """All cents fields must be non-negative when provided."""

    def test_negative_posted_price_cents_rejected(self):
        with pytest.raises(ValidationError):
            PricingRule(**_base_payload(posted_price_cents=-1))

    def test_negative_margin_cents_rejected(self):
        with pytest.raises(ValidationError):
            PricingRule(
                **_base_payload(
                    strategy="rack_plus_margin",
                    posted_price_cents=None,
                    margin_cents=-1,
                    terminal_id="term-x",
                )
            )

    def test_negative_freight_rate_rejected(self):
        with pytest.raises(ValidationError):
            PricingRule(
                **_base_payload(
                    strategy="cost_plus",
                    posted_price_cents=None,
                    margin_cents=12,
                    freight_rate_cents_per_mile=-1,
                )
            )

    def test_zero_posted_price_cents_allowed(self):
        """Zero ¢/gal is a legitimate promotional/giveaway price."""
        rule = PricingRule(**_base_payload(posted_price_cents=0))
        assert rule.posted_price_cents == 0


# ---------------------------------------------------------------------------
# Product code + optional keyword normalization
# ---------------------------------------------------------------------------


class TestProductCodeAndKeywords:
    """``product_code`` is non-empty; optional keywords are stripped."""

    def test_empty_product_code_rejected(self):
        with pytest.raises(ValidationError):
            PricingRule(**_base_payload(product_code=""))

    def test_whitespace_only_product_code_rejected(self):
        with pytest.raises(ValidationError):
            PricingRule(**_base_payload(product_code="   "))

    def test_product_code_stripped(self):
        rule = PricingRule(**_base_payload(product_code="  HEATING_OIL  "))
        assert rule.product_code == "HEATING_OIL"

    def test_customer_id_whitespace_collapses_to_none(self):
        rule = PricingRule(**_base_payload(customer_id="   "))
        assert rule.customer_id is None

    def test_account_id_whitespace_collapses_to_none(self):
        rule = PricingRule(**_base_payload(account_id="   "))
        assert rule.account_id is None


# ---------------------------------------------------------------------------
# Schema hygiene
# ---------------------------------------------------------------------------


class TestSchemaHygiene:
    """Unknown fields are forbidden; enum-typed fields reject unknowns."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            PricingRule(**_base_payload(unexpected_field="x"))

    def test_invalid_strategy_rejected(self):
        with pytest.raises(ValidationError):
            PricingRule(**_base_payload(strategy="hand_wave"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            PricingRule(**_base_payload(status="pending"))

    def test_tier_break_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            TierBreak(
                min_gallons=0.0,
                max_gallons=100.0,
                unit_price_cents=300,
                unexpected="x",
            )


# ---------------------------------------------------------------------------
# Priority field
# ---------------------------------------------------------------------------


class TestPriority:
    """``priority`` defaults to 0 and supports arbitrary ordering."""

    def test_priority_defaults_to_zero(self):
        payload = _base_payload()
        payload.pop("priority")
        rule = PricingRule(**payload)
        assert rule.priority == 0

    def test_priority_accepts_arbitrary_integers(self):
        rule = PricingRule(**_base_payload(priority=100))
        assert rule.priority == 100
