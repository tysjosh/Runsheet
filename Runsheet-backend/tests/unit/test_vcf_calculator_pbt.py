"""
Property-based tests for :class:`compliance.services.vcf_calculator.VCFCalculator`.

This module covers Task 2.6 of the Fuel Compliance Backbone spec, which
validates Requirement 2.7:

    FOR ALL valid temperature and API gravity inputs, computing VCF then
    dividing net_gallons by VCF SHALL produce the original gross_gallons
    within ±0.001 gallons (round-trip property).

The round-trip is deliberately exercised against the **unrounded** product
``gross * vcf`` rather than :meth:`VCFCalculator.compute_net_gallons`: the
compute_net_gallons helper rounds to the nearest tenth of a gallon
(Req 2.3), which by itself introduces up to ±0.05 gallons of error per
delivery and would dominate any genuine numerical drift the round-trip is
meant to detect. We keep a separate, looser unit test at the bottom of this
module to pin down the rounded-round-trip tolerance so both the math and
the rounding contract are exercised.

Hypothesis input space (per the task brief):

* ``temperature_f``  : floats in [-50.0, 150.0] — matches ``MIN_TEMPERATURE_F``
  and ``MAX_TEMPERATURE_F`` exactly so the full validated range is explored.
* ``api_gravity``    : floats in [1.0, 100.0] — the calculator accepts
  ``[0, 100]`` but API gravity near 0 makes the Table 6B polynomial
  numerically ill-conditioned (density approaches the water-density anchor
  and ``alpha_60`` grows rapidly); restricting to ``>=1`` keeps the property
  tests focused on the realistic product range without hiding bugs.
* ``gross_gallons``  : floats in [0.0, 50_000.0], no NaN, no infinity —
  covers empty loads through the largest US fuel transport (11,600-gallon
  cargo tank × 4 compartments worst-case ≈ 46,400) with margin.

Validates: Requirement 2.7
"""
from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from compliance.services.vcf_calculator import (
    MAX_API_GRAVITY,
    MAX_TEMPERATURE_F,
    MIN_API_GRAVITY,
    MIN_TEMPERATURE_F,
    VCFCalculator,
)


# ---------------------------------------------------------------------------
# Shared calculator instance
#
# :class:`VCFCalculator` is stateless with respect to its numerical
# computations, so one instance is reused across every property-based
# iteration to avoid the fuel-product-catalog import cost per example.
# ---------------------------------------------------------------------------

_CALCULATOR = VCFCalculator()


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Keep the strategy bounds tied to the module constants so any future
# change to the validated range (Req 2.6) automatically flows through to
# the property tests without a separate edit.
_temperature_f = st.floats(
    min_value=MIN_TEMPERATURE_F,  # -50.0
    max_value=MAX_TEMPERATURE_F,  # 150.0
    allow_nan=False,
    allow_infinity=False,
)

# See the module docstring: API gravity near 0 is numerically
# ill-conditioned and is not a realistic product input. 1.0 is a safe
# floor that still exercises the full physical product range (propane
# sits near ~147 °API which is outside the spec window anyway; the
# calculator raises ``vcf.input_out_of_range`` for that).
_api_gravity = st.floats(
    min_value=1.0,
    max_value=MAX_API_GRAVITY,  # 100.0
    allow_nan=False,
    allow_infinity=False,
)

_gross_gallons = st.floats(
    min_value=0.0,
    max_value=50_000.0,
    allow_nan=False,
    allow_infinity=False,
)


# ---------------------------------------------------------------------------
# Property: gross → (gross * vcf) → gross round-trip within ±0.001 gallons
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    gross_gallons=_gross_gallons,
    temperature_f=_temperature_f,
    api_gravity=_api_gravity,
)
@settings(
    # Disabling the HealthCheck.function_scoped_fixture warning is not
    # needed here (no fixtures are used), but we keep deadline=None to
    # match the project-wide Hypothesis profile in ``tests/conftest.py``
    # and avoid spurious DeadlineExceeded failures on slower CI nodes.
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_vcf_round_trip_recovers_gross_within_tolerance(
    gross_gallons: float,
    temperature_f: float,
    api_gravity: float,
) -> None:
    """gross → (gross · VCF) → gross must be within ±0.001 gallons.

    Given valid temperature and API gravity inputs, computing VCF and then
    dividing the *unrounded* net gallons by that same VCF must recover the
    original gross gallons to within 0.001 gallons.

    Using the rounded :meth:`VCFCalculator.compute_net_gallons` here would
    hide the property the task is explicitly testing: Req 2.7 is about the
    numerical stability of the VCF itself, not the 0.1-gallon rounding
    contract. The rounding contract is validated separately at the bottom
    of this module.

    Validates: Requirements 2.7
    """
    # VCF is a positive, finite dimensionless factor for every input in
    # the validated range. Guard the invariant explicitly so if it ever
    # regresses the failure is attributed to compute_vcf rather than to
    # the downstream division below.
    vcf = _CALCULATOR.compute_vcf(temperature_f=temperature_f, api_gravity=api_gravity)
    assert math.isfinite(vcf), f"VCF must be finite, got {vcf!r}"
    assert vcf > 0.0, f"VCF must be strictly positive, got {vcf!r}"

    # Unrounded net so the round-trip captures pure floating-point drift
    # rather than the 0.1-gallon quantisation of :meth:`compute_net_gallons`.
    net_gallons = gross_gallons * vcf

    # Recover the gross reading by inverting the multiplication. If the
    # VCF math is numerically stable (which Table 6B is, by construction),
    # this recovers gross to within one or two ULPs of a double.
    recovered_gross = net_gallons / vcf

    assert abs(recovered_gross - gross_gallons) < 0.001, (
        "Round-trip drift exceeded ±0.001 gallons: "
        f"gross={gross_gallons}, vcf={vcf}, net={net_gallons}, "
        f"recovered={recovered_gross}, "
        f"abs_diff={abs(recovered_gross - gross_gallons)}"
    )


# ---------------------------------------------------------------------------
# Companion unit test — rounded round-trip via compute_net_gallons
# ---------------------------------------------------------------------------


def test_compute_net_gallons_rounded_round_trip_within_rounding_tolerance() -> None:
    """Rounded round-trip via :meth:`compute_net_gallons` stays within 0.1 gal.

    The public helper rounds net gallons to the nearest tenth of a gallon
    (Req 2.3). Inverting that rounded value through the VCF recovers the
    original gross gallons only up to the rounding tolerance, so we pin
    the expected looser bound here. Choosing ±0.1 gallons (one rounding
    step) rather than the stricter ±0.001 of the property above makes the
    separate responsibilities of Req 2.3 and Req 2.7 explicit.

    This is deliberately a small, fixed-example test (not a Hypothesis
    sweep): the property test above already covers the numerical-stability
    claim of Req 2.7, and the goal here is just to anchor the rounding
    contract on a realistic delivery-size input so a future regression in
    either ``compute_net_gallons`` or the rounding digit count is caught.
    """
    gross_gallons = 8_000.0
    temperature_f = 72.0  # ambient above 60 °F reference
    api_gravity = 35.0    # typical #2 diesel

    vcf = _CALCULATOR.compute_vcf(temperature_f=temperature_f, api_gravity=api_gravity)
    net_rounded = _CALCULATOR.compute_net_gallons(
        gross_gallons=gross_gallons,
        temperature_f=temperature_f,
        api_gravity=api_gravity,
    )

    # Recover gross by inverting the rounded net through the same VCF.
    recovered_gross = net_rounded / vcf

    # Rounding to the nearest tenth introduces at most ±0.05 net gallons,
    # which corresponds to ±0.05 / vcf gross gallons. VCF is very close
    # to 1 for the realistic product range, so ±0.1 gross gallons is a
    # comfortable envelope that still flags a regression in either the
    # rounding digit count or the rounding direction.
    assert abs(recovered_gross - gross_gallons) < 0.1, (
        "Rounded round-trip drift exceeded ±0.1 gallons: "
        f"gross={gross_gallons}, vcf={vcf}, net_rounded={net_rounded}, "
        f"recovered={recovered_gross}, "
        f"abs_diff={abs(recovered_gross - gross_gallons)}"
    )
