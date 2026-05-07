"""
Unit tests for :mod:`fuel.storm_mode_models`.

Covers Capability 9 / Task 10.1 of the fuel-ops hardening spec:

* :class:`WeatherAlert` — required-field enforcement, time-window check,
  affected_zip_codes normalization, severity / status enum validation.
* :class:`StormModeOverride` — required-field enforcement, action enum
  validation, expires_at nullability.
* :class:`KeepFullCustomer` — default values match Requirement 9.2.1
  (``keep_full_priority_boost=0.25``), range constraints on
  ``minimum_low_water_pct`` and ``keep_full_priority_boost``.
* :class:`CustomerProfile` — defaults for the Storm_Mode extensions
  (criticality_tier='standard', is_generator_fuel=False,
  requires_continuous_service=False, keep_full disabled), tolerance of
  extra fields (``extra="ignore"``) so future Phase-5 fields do not
  break the Phase-10 surface.

Validates: Requirements 9.1.1, 9.1.2, 9.2.1, 9.2.2.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from fuel.storm_mode_models import (
    CustomerProfile,
    CustomerProfileStormFields,
    KeepFullCustomer,
    StormModeOverride,
    WeatherAlert,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_alert_kwargs(**overrides) -> dict:
    """Return a minimal-but-valid kwargs dict for a :class:`WeatherAlert`."""
    base = {
        "alert_id": "noaa-winter-001",
        "tenant_id": "tenant-a",
        "region_code": "NY",
        "alert_type": "winter_storm_warning",
        "severity": "severe",
        "expected_start_at": _now(),
        "expected_end_at": _now() + timedelta(hours=12),
        "affected_zip_codes": ["10001", "10002"],
        "source": "noaa",
        "ingested_at": _now(),
    }
    base.update(overrides)
    return base


def _valid_override_kwargs(**overrides) -> dict:
    base = {
        "override_id": "ovr-001",
        "tenant_id": "tenant-a",
        "action": "activate",
        "reason": "Incoming ice storm; NOAA feed still catching up.",
        "actor_id": "dispatcher-42",
        "expires_at": _now() + timedelta(hours=6),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# WeatherAlert
# ---------------------------------------------------------------------------


class TestWeatherAlert:
    def test_valid_instance_round_trips(self):
        alert = WeatherAlert(**_valid_alert_kwargs())
        dumped = alert.model_dump(mode="json")
        parsed = WeatherAlert(**dumped)
        assert parsed.alert_id == "noaa-winter-001"
        assert parsed.severity == "severe"
        assert parsed.activation_status == "forecast"  # default
        assert parsed.affected_zip_codes == ["10001", "10002"]

    def test_blank_required_string_rejected(self):
        with pytest.raises(ValidationError):
            WeatherAlert(**_valid_alert_kwargs(alert_id="   "))
        with pytest.raises(ValidationError):
            WeatherAlert(**_valid_alert_kwargs(tenant_id=""))

    def test_end_before_start_rejected(self):
        start = _now()
        kwargs = _valid_alert_kwargs(
            expected_start_at=start,
            expected_end_at=start - timedelta(hours=1),
        )
        with pytest.raises(ValidationError):
            WeatherAlert(**kwargs)

    def test_end_equal_to_start_allowed(self):
        start = _now()
        alert = WeatherAlert(
            **_valid_alert_kwargs(expected_start_at=start, expected_end_at=start)
        )
        assert alert.expected_end_at == alert.expected_start_at

    def test_optional_end_at_nullable(self):
        alert = WeatherAlert(**_valid_alert_kwargs(expected_end_at=None))
        assert alert.expected_end_at is None

    def test_affected_zip_codes_stripped(self):
        alert = WeatherAlert(
            **_valid_alert_kwargs(affected_zip_codes=[" 10001 ", "10002"])
        )
        assert alert.affected_zip_codes == ["10001", "10002"]

    def test_blank_zip_code_rejected(self):
        with pytest.raises(ValidationError):
            WeatherAlert(**_valid_alert_kwargs(affected_zip_codes=["10001", "  "]))

    def test_invalid_severity_rejected(self):
        with pytest.raises(ValidationError):
            WeatherAlert(**_valid_alert_kwargs(severity="catastrophic"))

    def test_invalid_source_rejected(self):
        with pytest.raises(ValidationError):
            WeatherAlert(**_valid_alert_kwargs(source="twitter"))

    def test_unknown_activation_status_rejected(self):
        with pytest.raises(ValidationError):
            WeatherAlert(**_valid_alert_kwargs(activation_status="pending"))

    def test_extra_field_rejected(self):
        kwargs = _valid_alert_kwargs()
        kwargs["impact_score"] = 0.9
        with pytest.raises(ValidationError):
            WeatherAlert(**kwargs)

    def test_default_affected_zip_codes_empty(self):
        kwargs = _valid_alert_kwargs()
        kwargs.pop("affected_zip_codes")
        alert = WeatherAlert(**kwargs)
        assert alert.affected_zip_codes == []


# ---------------------------------------------------------------------------
# StormModeOverride
# ---------------------------------------------------------------------------


class TestStormModeOverride:
    def test_valid_instance_round_trips(self):
        override = StormModeOverride(**_valid_override_kwargs())
        parsed = StormModeOverride(**override.model_dump(mode="json"))
        assert parsed.action == "activate"
        assert parsed.actor_id == "dispatcher-42"

    def test_blank_reason_rejected(self):
        with pytest.raises(ValidationError):
            StormModeOverride(**_valid_override_kwargs(reason=""))

    def test_blank_actor_rejected(self):
        with pytest.raises(ValidationError):
            StormModeOverride(**_valid_override_kwargs(actor_id="   "))

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            StormModeOverride(**_valid_override_kwargs(action="pause"))

    @pytest.mark.parametrize(
        "action", ["activate", "deactivate", "snooze", "clear"]
    )
    def test_all_valid_actions_accepted(self, action):
        override = StormModeOverride(**_valid_override_kwargs(action=action))
        assert override.action == action

    def test_expires_at_nullable(self):
        override = StormModeOverride(**_valid_override_kwargs(expires_at=None))
        assert override.expires_at is None

    def test_extra_field_rejected(self):
        kwargs = _valid_override_kwargs()
        kwargs["source_alert_id"] = "noaa-001"
        with pytest.raises(ValidationError):
            StormModeOverride(**kwargs)


# ---------------------------------------------------------------------------
# KeepFullCustomer
# ---------------------------------------------------------------------------


class TestKeepFullCustomer:
    def test_defaults_match_requirement(self):
        """Requirement 9.2.1 default boost is 0.25."""
        kf = KeepFullCustomer()
        assert kf.keep_full_enabled is False
        assert kf.minimum_low_water_pct == 30.0
        assert kf.keep_full_priority_boost == 0.25

    def test_boost_range_enforced_low(self):
        with pytest.raises(ValidationError):
            KeepFullCustomer(keep_full_priority_boost=-0.01)

    def test_boost_range_enforced_high(self):
        with pytest.raises(ValidationError):
            KeepFullCustomer(keep_full_priority_boost=1.01)

    def test_min_water_pct_range_enforced(self):
        with pytest.raises(ValidationError):
            KeepFullCustomer(minimum_low_water_pct=-0.01)
        with pytest.raises(ValidationError):
            KeepFullCustomer(minimum_low_water_pct=100.01)

    def test_boundary_values_accepted(self):
        kf_low = KeepFullCustomer(
            keep_full_priority_boost=0.0, minimum_low_water_pct=0.0
        )
        kf_high = KeepFullCustomer(
            keep_full_priority_boost=1.0, minimum_low_water_pct=100.0
        )
        assert kf_low.keep_full_priority_boost == 0.0
        assert kf_high.minimum_low_water_pct == 100.0

    def test_enabled_true(self):
        kf = KeepFullCustomer(keep_full_enabled=True)
        assert kf.keep_full_enabled is True

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            KeepFullCustomer(keep_full_enabled=True, unknown="x")


# ---------------------------------------------------------------------------
# CustomerProfileStormFields
# ---------------------------------------------------------------------------


class TestCustomerProfileStormFields:
    def test_defaults(self):
        fields = CustomerProfileStormFields()
        assert fields.criticality_tier == "standard"
        assert fields.is_generator_fuel is False
        assert fields.requires_continuous_service is False
        assert fields.keep_full.keep_full_enabled is False

    def test_accepts_all_criticality_tiers(self):
        for tier in [
            "keep_full_residential",
            "medical",
            "data_center",
            "industrial_critical",
            "commercial",
            "standard",
        ]:
            fields = CustomerProfileStormFields(criticality_tier=tier)
            assert fields.criticality_tier == tier

    def test_rejects_unknown_tier(self):
        with pytest.raises(ValidationError):
            CustomerProfileStormFields(criticality_tier="vip")


# ---------------------------------------------------------------------------
# CustomerProfile
# ---------------------------------------------------------------------------


class TestCustomerProfile:
    def test_minimal_valid(self):
        profile = CustomerProfile(customer_id="cust-1", tenant_id="tenant-a")
        assert profile.criticality_tier == "standard"
        assert profile.is_generator_fuel is False
        assert profile.requires_continuous_service is False
        assert profile.keep_full.keep_full_enabled is False
        assert profile.zip_code is None

    def test_zip_code_stripped_to_none(self):
        profile = CustomerProfile(
            customer_id="cust-1", tenant_id="tenant-a", zip_code="   "
        )
        assert profile.zip_code is None

    def test_zip_code_preserved(self):
        profile = CustomerProfile(
            customer_id="cust-1", tenant_id="tenant-a", zip_code=" 10001 "
        )
        assert profile.zip_code == "10001"

    def test_blank_customer_id_rejected(self):
        with pytest.raises(ValidationError):
            CustomerProfile(customer_id="   ", tenant_id="tenant-a")

    def test_extra_fields_ignored_not_rejected(self):
        """``extra='ignore'`` so future additions do not break Phase-10."""
        profile = CustomerProfile(
            customer_id="cust-1",
            tenant_id="tenant-a",
            future_phase_field="ignored",
            another_unknown=42,
        )
        # Unknown fields are dropped, not persisted.
        dumped = profile.model_dump()
        assert "future_phase_field" not in dumped
        assert "another_unknown" not in dumped
        # Known fields still present.
        assert dumped["customer_id"] == "cust-1"

    def test_generator_and_continuous_service_toggles(self):
        profile = CustomerProfile(
            customer_id="cust-1",
            tenant_id="tenant-a",
            is_generator_fuel=True,
            requires_continuous_service=True,
        )
        assert profile.is_generator_fuel is True
        assert profile.requires_continuous_service is True

    def test_nested_keep_full_boost_override(self):
        profile = CustomerProfile(
            customer_id="cust-1",
            tenant_id="tenant-a",
            keep_full=KeepFullCustomer(
                keep_full_enabled=True,
                minimum_low_water_pct=25.0,
                keep_full_priority_boost=0.4,
            ),
        )
        assert profile.keep_full.keep_full_enabled is True
        assert profile.keep_full.minimum_low_water_pct == 25.0
        assert profile.keep_full.keep_full_priority_boost == 0.4
