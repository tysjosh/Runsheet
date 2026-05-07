"""
Unit tests for ``fuel.services.prioritization_helpers.compute_business_impact``.

Validates Requirements 3.3.1, 3.3.2, and 3.3.5:
    * 3.3.1 — ``CustomerProfile`` carries ``annual_revenue_usd``,
              ``contract_penalty_usd_per_day``, ``sla_tier``, and
              ``missed_delivery_cost_usd`` (the model extensions are
              exercised indirectly via the score calculator).
    * 3.3.2 — normalized weighted-sum formula with weights
              {annual_revenue_usd: 0.4, contract_penalty_usd_per_day: 0.3,
               missed_delivery_cost_usd: 0.2, sla_tier (tier-score × 0.1)}.
    * 3.3.5 — missing profile fields default to zero and surface a
              ``missing_profile_field:{field}`` reason.
"""
from __future__ import annotations

import math

import pytest

from fuel.services.prioritization_helpers import (
    BUSINESS_IMPACT_WEIGHTS,
    SLA_TIER_SCORES,
    compute_business_impact,
)
from fuel.storm_mode_models import CustomerProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_max(
    annual_revenue_usd: float = 1_000_000.0,
    contract_penalty_usd_per_day: float = 10_000.0,
    missed_delivery_cost_usd: float = 50_000.0,
) -> dict:
    """Return a tenant-max mapping with realistic defaults."""
    return {
        "annual_revenue_usd": annual_revenue_usd,
        "contract_penalty_usd_per_day": contract_penalty_usd_per_day,
        "missed_delivery_cost_usd": missed_delivery_cost_usd,
    }


def _profile(**overrides) -> CustomerProfile:
    """Create a :class:`CustomerProfile` with identity defaults filled in."""
    base = {"customer_id": "cust-1", "tenant_id": "tenant-a"}
    base.update(overrides)
    return CustomerProfile(**base)


# ---------------------------------------------------------------------------
# Constant sanity checks (Requirement 3.3.2)
# ---------------------------------------------------------------------------


def test_weights_match_design_capability_3() -> None:
    """Req 3.3.2: the four weights are 0.4 / 0.3 / 0.2 / 0.1."""
    assert BUSINESS_IMPACT_WEIGHTS == {
        "annual_revenue_usd": 0.4,
        "contract_penalty_usd_per_day": 0.3,
        "missed_delivery_cost_usd": 0.2,
        "sla_tier": 0.1,
    }


def test_weights_sum_to_unit() -> None:
    """The weights partition 1.0 so the score stays bounded in [0, 1]."""
    assert math.isclose(sum(BUSINESS_IMPACT_WEIGHTS.values()), 1.0)


def test_sla_tier_scores_match_design() -> None:
    """Req 3.3.2: platinum/gold/silver/bronze map to 1.0/0.75/0.5/0.25."""
    assert SLA_TIER_SCORES == {
        "platinum": 1.0,
        "gold": 0.75,
        "silver": 0.5,
        "bronze": 0.25,
    }


# ---------------------------------------------------------------------------
# Score formula (Requirement 3.3.2)
# ---------------------------------------------------------------------------


def test_maxed_profile_with_platinum_tier_scores_one() -> None:
    """A profile at or above every tenant max with platinum tier = 1.0."""
    profile = _profile(
        annual_revenue_usd=1_000_000.0,
        contract_penalty_usd_per_day=10_000.0,
        missed_delivery_cost_usd=50_000.0,
        sla_tier="platinum",
    )
    score, reasons = compute_business_impact(profile, _tenant_max())
    # 0.4 + 0.3 + 0.2 + (1.0 * 0.1) = 1.0
    assert score == pytest.approx(1.0)
    # No "missing_profile_field" entries when every field is populated.
    assert not [r for r in reasons if r.startswith("missing_profile_field")]


def test_bronze_tier_only_profile_scores_tier_component_only() -> None:
    """Only the tier component contributes when monetary fields are zero.

    Req 3.3.5: the three monetary fields default to zero with
    ``missing_profile_field`` reasons; bronze tier contributes
    ``0.25 * 0.1 = 0.025``.
    """
    profile = _profile(sla_tier="bronze")
    score, reasons = compute_business_impact(profile, _tenant_max())
    assert score == pytest.approx(0.025)
    assert "missing_profile_field:annual_revenue_usd" in reasons
    assert "missing_profile_field:contract_penalty_usd_per_day" in reasons
    assert "missing_profile_field:missed_delivery_cost_usd" in reasons
    # sla_tier IS populated → no missing reason for it.
    assert "missing_profile_field:sla_tier" not in reasons


def test_half_max_monetary_fields_half_component_contributions() -> None:
    """Half the tenant max → half of each component's weight."""
    profile = _profile(
        annual_revenue_usd=500_000.0,  # 50% of max
        contract_penalty_usd_per_day=5_000.0,  # 50% of max
        missed_delivery_cost_usd=25_000.0,  # 50% of max
        sla_tier="silver",  # 0.5 tier score
    )
    score, _ = compute_business_impact(profile, _tenant_max())
    # 0.2 + 0.15 + 0.10 + (0.5 * 0.1) = 0.5
    assert score == pytest.approx(0.5)


def test_profile_above_tenant_max_is_clamped_to_weight() -> None:
    """A value above the tenant max should not push the component past its weight."""
    profile = _profile(
        annual_revenue_usd=5_000_000.0,  # 5× the tenant max
        sla_tier="platinum",
    )
    score, _ = compute_business_impact(profile, _tenant_max())
    # Clamped: annual_revenue_usd contribution = 0.4 (not 2.0).
    # missed_delivery / penalty missing, so total = 0.4 + (1.0 * 0.1) = 0.5.
    assert score == pytest.approx(0.5)
    assert score <= 1.0


# ---------------------------------------------------------------------------
# Missing-field behavior (Requirement 3.3.5)
# ---------------------------------------------------------------------------


def test_completely_empty_profile_scores_tier_fallback() -> None:
    """No monetary data + no sla_tier → score is the bronze fallback tier only."""
    profile = _profile()  # every business-impact field None
    score, reasons = compute_business_impact(profile, _tenant_max())
    # Bronze fallback (0.25) * sla_tier weight (0.1) = 0.025.
    assert score == pytest.approx(0.025)
    assert "missing_profile_field:annual_revenue_usd" in reasons
    assert "missing_profile_field:contract_penalty_usd_per_day" in reasons
    assert "missing_profile_field:missed_delivery_cost_usd" in reasons
    assert "missing_profile_field:sla_tier" in reasons


def test_zero_value_monetary_fields_flagged_as_missing() -> None:
    """Req 3.3.5: a zero value is treated the same as a missing field."""
    profile = _profile(
        annual_revenue_usd=0.0,
        contract_penalty_usd_per_day=0.0,
        missed_delivery_cost_usd=0.0,
        sla_tier="gold",
    )
    score, reasons = compute_business_impact(profile, _tenant_max())
    # Only gold tier contributes: 0.75 * 0.1 = 0.075
    assert score == pytest.approx(0.075)
    for field in (
        "annual_revenue_usd",
        "contract_penalty_usd_per_day",
        "missed_delivery_cost_usd",
    ):
        assert f"missing_profile_field:{field}" in reasons


def test_unknown_sla_tier_defaults_to_bronze_with_missing_reason() -> None:
    """Unrecognized tier strings fall back to bronze per design."""
    # Using a dict input because CustomerProfile rejects unknown sla_tier values.
    profile = {
        "annual_revenue_usd": None,
        "contract_penalty_usd_per_day": None,
        "missed_delivery_cost_usd": None,
        "sla_tier": "diamond",
    }
    score, reasons = compute_business_impact(profile, _tenant_max())
    assert score == pytest.approx(0.025)
    assert "missing_profile_field:sla_tier" in reasons


# ---------------------------------------------------------------------------
# tenant_max edge cases
# ---------------------------------------------------------------------------


def test_zero_tenant_max_does_not_divide_by_zero() -> None:
    """A zero tenant_max entry falls back to 1.0 without erroring."""
    profile = _profile(
        annual_revenue_usd=1000.0,
        contract_penalty_usd_per_day=50.0,
        missed_delivery_cost_usd=500.0,
        sla_tier="gold",
    )
    tenant_max = {
        "annual_revenue_usd": 0.0,
        "contract_penalty_usd_per_day": 0.0,
        "missed_delivery_cost_usd": 0.0,
    }
    score, _ = compute_business_impact(profile, tenant_max)
    # Each monetary ratio = min(value / 1.0, 1.0) = 1.0 → full weight applied.
    # So score = 0.4 + 0.3 + 0.2 + (0.75 * 0.1) = 0.975.
    assert score == pytest.approx(0.975)


def test_missing_tenant_max_field_falls_back_to_unit() -> None:
    """A tenant_max that omits a field still yields a valid score."""
    profile = _profile(
        annual_revenue_usd=1.0,
        sla_tier="silver",
    )
    # tenant_max is empty → every missing max is treated as 1.0.
    score, _ = compute_business_impact(profile, {})
    # annual_revenue ratio clamped at 1.0 → contributes 0.4.
    # Other monetary fields missing → reasons recorded, contributions 0.
    # SLA silver = 0.5 * 0.1 = 0.05.
    assert score == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_negative_profile_value_raises_value_error() -> None:
    """Negative monetary values are a programming error, not data."""
    profile = {
        "annual_revenue_usd": -100.0,
        "contract_penalty_usd_per_day": None,
        "missed_delivery_cost_usd": None,
        "sla_tier": "gold",
    }
    with pytest.raises(ValueError):
        compute_business_impact(profile, _tenant_max())


def test_nan_profile_value_raises_value_error() -> None:
    profile = {
        "annual_revenue_usd": float("nan"),
        "contract_penalty_usd_per_day": None,
        "missed_delivery_cost_usd": None,
        "sla_tier": "gold",
    }
    with pytest.raises(ValueError):
        compute_business_impact(profile, _tenant_max())


def test_negative_tenant_max_raises_value_error() -> None:
    profile = _profile(annual_revenue_usd=100.0, sla_tier="gold")
    with pytest.raises(ValueError):
        compute_business_impact(
            profile,
            {"annual_revenue_usd": -1.0},
        )


def test_nan_tenant_max_raises_value_error() -> None:
    profile = _profile(annual_revenue_usd=100.0, sla_tier="gold")
    with pytest.raises(ValueError):
        compute_business_impact(
            profile,
            {"annual_revenue_usd": float("nan")},
        )


def test_non_string_sla_tier_raises_value_error() -> None:
    profile = {
        "annual_revenue_usd": None,
        "contract_penalty_usd_per_day": None,
        "missed_delivery_cost_usd": None,
        "sla_tier": 1,  # ints are not tiers
    }
    with pytest.raises(ValueError):
        compute_business_impact(profile, _tenant_max())


# ---------------------------------------------------------------------------
# Input-shape flexibility (mapping vs model access)
# ---------------------------------------------------------------------------


def test_accepts_plain_mapping_profile() -> None:
    """Plain-dict profiles work without wrapping in the Pydantic model."""
    profile = {
        "annual_revenue_usd": 500_000.0,
        "contract_penalty_usd_per_day": 5_000.0,
        "missed_delivery_cost_usd": 25_000.0,
        "sla_tier": "silver",
    }
    score, _ = compute_business_impact(profile, _tenant_max())
    # Same as half-max model test: 0.2 + 0.15 + 0.10 + (0.5 * 0.1) = 0.5.
    assert score == pytest.approx(0.5)


def test_sla_tier_is_case_insensitive() -> None:
    """A tier written in any case still matches the canonical tier score."""
    profile = {
        "annual_revenue_usd": None,
        "contract_penalty_usd_per_day": None,
        "missed_delivery_cost_usd": None,
        "sla_tier": "PLATINUM",
    }
    score, reasons = compute_business_impact(profile, _tenant_max())
    # 1.0 (platinum) * 0.1 weight = 0.1
    assert score == pytest.approx(0.1)
    assert "missing_profile_field:sla_tier" not in reasons


# ---------------------------------------------------------------------------
# Dominant-component reasons (UI explainability)
# ---------------------------------------------------------------------------


def test_dominant_component_surfaced_when_single_driver_exceeds_half() -> None:
    """When one component is more than half the score it appears in reasons."""
    profile = _profile(
        annual_revenue_usd=1_000_000.0,  # full 0.4 weight
        sla_tier="bronze",  # 0.25 * 0.1 = 0.025
    )
    score, reasons = compute_business_impact(profile, _tenant_max())
    # Score = 0.4 + 0.025 = 0.425. Half = 0.2125. Only the revenue component
    # (0.4) exceeds that threshold.
    assert score == pytest.approx(0.425)
    assert "dominant_component:annual_revenue_usd" in reasons
    assert "dominant_component:sla_tier" not in reasons


def test_score_always_bounded_in_unit_interval() -> None:
    """For a wide range of inputs the returned score stays within [0, 1]."""
    cases = [
        dict(),
        dict(annual_revenue_usd=1.0),
        dict(annual_revenue_usd=10 ** 9, sla_tier="platinum"),
        dict(
            annual_revenue_usd=100.0,
            contract_penalty_usd_per_day=0.0,
            missed_delivery_cost_usd=999_999.0,
            sla_tier="gold",
        ),
    ]
    for kwargs in cases:
        profile = _profile(**kwargs)
        score, _ = compute_business_impact(profile, _tenant_max())
        assert 0.0 <= score <= 1.0, kwargs
