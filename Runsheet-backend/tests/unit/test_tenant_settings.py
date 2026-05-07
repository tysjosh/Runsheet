"""
Unit tests for TenantSettingsService — per-tenant Region + measurement_units.

Covers:
* Default values for new tenants (``US`` + gallons/miles).
* Default inference for NG tenants (liters/km) via
  ``default_measurement_units_for_region``.
* Redis read path fall-open behavior for missing keys, missing clients,
  invalid JSON, and non-dict payloads.
* Write path validation and Redis key layout.
* ``set_region`` preserving explicit unit overrides while swapping the
  canonical defaults for the other region.

Validates: Requirements 6.1.5, 6.3.1.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock

from services.tenant_settings import (
    MeasurementUnits,
    TenantSettings,
    TenantSettingsService,
    VALID_DISTANCE_UNITS,
    VALID_REGIONS,
    VALID_VOLUME_UNITS,
    default_measurement_units_for_region,
    default_tenant_settings,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_tenant_settings_is_us_imperial(self):
        """Req 6.1.5: new tenants default to Region=US + gallons/miles."""
        settings = default_tenant_settings()
        assert settings.region == "US"
        assert settings.measurement_units.volume == "gal"
        assert settings.measurement_units.distance == "mi"

    def test_default_units_for_us(self):
        units = default_measurement_units_for_region("US")
        assert units == MeasurementUnits(volume="gal", distance="mi")

    def test_default_units_for_ng(self):
        """Req 6.1.5: pre-pivot (NG) tenants default to liters/km."""
        units = default_measurement_units_for_region("NG")
        assert units == MeasurementUnits(volume="l", distance="km")

    def test_default_units_case_insensitive(self):
        assert default_measurement_units_for_region("ng") == MeasurementUnits(
            volume="l", distance="km"
        )
        assert default_measurement_units_for_region("us") == MeasurementUnits(
            volume="gal", distance="mi"
        )

    def test_default_units_unknown_region_falls_back_to_us(self):
        """Unknown region strings degrade to US/imperial rather than raising."""
        assert default_measurement_units_for_region("XX") == MeasurementUnits(
            volume="gal", distance="mi"
        )
        assert default_measurement_units_for_region("") == MeasurementUnits(
            volume="gal", distance="mi"
        )

    def test_valid_sets(self):
        assert VALID_REGIONS == frozenset({"US", "NG"})
        assert VALID_VOLUME_UNITS == frozenset({"gal", "l"})
        assert VALID_DISTANCE_UNITS == frozenset({"mi", "km"})


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_get_without_redis_returns_defaults(self):
        service = TenantSettingsService(redis_client=None)
        settings = await service.get("tenant-1")
        assert settings == default_tenant_settings()

    @pytest.mark.asyncio
    async def test_get_with_empty_tenant_id_returns_defaults(self):
        service = TenantSettingsService(redis_client=AsyncMock())
        settings = await service.get("")
        assert settings == default_tenant_settings()

    @pytest.mark.asyncio
    async def test_get_missing_key_returns_defaults(self):
        redis = AsyncMock()
        redis.get.return_value = None
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        redis.get.assert_awaited_once_with("tenant:tenant-1:settings")
        assert settings == default_tenant_settings()

    @pytest.mark.asyncio
    async def test_get_valid_us_payload(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {"region": "US", "measurement_units": {"volume": "gal", "distance": "mi"}}
        )
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        assert settings.region == "US"
        assert settings.measurement_units.volume == "gal"
        assert settings.measurement_units.distance == "mi"

    @pytest.mark.asyncio
    async def test_get_valid_ng_payload(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {"region": "NG", "measurement_units": {"volume": "l", "distance": "km"}}
        )
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-ng")

        assert settings.region == "NG"
        assert settings.measurement_units.volume == "l"
        assert settings.measurement_units.distance == "km"

    @pytest.mark.asyncio
    async def test_get_accepts_bytes_payload(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {"region": "US", "measurement_units": {"volume": "gal", "distance": "mi"}}
        ).encode()
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        assert settings.region == "US"

    @pytest.mark.asyncio
    async def test_get_invalid_json_falls_back(self):
        redis = AsyncMock()
        redis.get.return_value = "not-json"
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        assert settings == default_tenant_settings()

    @pytest.mark.asyncio
    async def test_get_non_dict_payload_falls_back(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(["not", "a", "dict"])
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        assert settings == default_tenant_settings()

    @pytest.mark.asyncio
    async def test_get_unknown_region_uses_us_default(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps({"region": "DE"})
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        assert settings.region == "US"
        assert settings.measurement_units == MeasurementUnits(
            volume="gal", distance="mi"
        )

    @pytest.mark.asyncio
    async def test_get_partial_units_fills_from_region_default(self):
        """Missing unit fields backfill from the region default, not raise."""
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {"region": "NG", "measurement_units": {"volume": "l"}}
        )
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        assert settings.region == "NG"
        assert settings.measurement_units.volume == "l"
        assert settings.measurement_units.distance == "km"

    @pytest.mark.asyncio
    async def test_get_region_convenience(self):
        service = TenantSettingsService(redis_client=None)
        assert await service.get_region("tenant-x") == "US"

    @pytest.mark.asyncio
    async def test_get_measurement_units_convenience(self):
        service = TenantSettingsService(redis_client=None)
        units = await service.get_measurement_units("tenant-x")
        assert units == MeasurementUnits(volume="gal", distance="mi")


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


class TestSet:
    @pytest.mark.asyncio
    async def test_set_requires_redis(self):
        service = TenantSettingsService(redis_client=None)
        with pytest.raises(RuntimeError):
            await service.set("tenant-1", default_tenant_settings())

    @pytest.mark.asyncio
    async def test_set_requires_tenant_id(self):
        service = TenantSettingsService(redis_client=AsyncMock())
        with pytest.raises(ValueError):
            await service.set("", default_tenant_settings())

    @pytest.mark.asyncio
    async def test_set_persists_expected_payload(self):
        redis = AsyncMock()
        service = TenantSettingsService(redis_client=redis)

        settings = TenantSettings(
            region="NG",
            measurement_units=MeasurementUnits(volume="l", distance="km"),
        )
        await service.set("tenant-ng", settings)

        redis.set.assert_awaited_once()
        key, payload = redis.set.call_args.args
        assert key == "tenant:tenant-ng:settings"
        assert json.loads(payload) == {
            "region": "NG",
            "measurement_units": {"volume": "l", "distance": "km"},
            "default_depot_id": None,
        }

    @pytest.mark.asyncio
    async def test_set_rejects_invalid_region(self):
        redis = AsyncMock()
        service = TenantSettingsService(redis_client=redis)

        bad = TenantSettings(region="XX", measurement_units=MeasurementUnits())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await service.set("tenant-1", bad)
        redis.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_rejects_invalid_volume(self):
        redis = AsyncMock()
        service = TenantSettingsService(redis_client=redis)

        bad = TenantSettings(
            region="US",
            measurement_units=MeasurementUnits(volume="barrels", distance="mi"),  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError):
            await service.set("tenant-1", bad)

    @pytest.mark.asyncio
    async def test_set_rejects_invalid_distance(self):
        redis = AsyncMock()
        service = TenantSettingsService(redis_client=redis)

        bad = TenantSettings(
            region="US",
            measurement_units=MeasurementUnits(volume="gal", distance="ft"),  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError):
            await service.set("tenant-1", bad)


class TestSetRegion:
    @pytest.mark.asyncio
    async def test_set_region_swaps_default_units(self):
        """Flipping from US to NG without explicit overrides swaps to metric."""
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {"region": "US", "measurement_units": {"volume": "gal", "distance": "mi"}}
        )
        service = TenantSettingsService(redis_client=redis)

        updated = await service.set_region("tenant-1", "NG")

        assert updated.region == "NG"
        assert updated.measurement_units == MeasurementUnits(volume="l", distance="km")
        redis.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_region_preserves_explicit_overrides(self):
        """Explicit mixed units survive a region swap."""
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {"region": "US", "measurement_units": {"volume": "l", "distance": "mi"}}
        )
        service = TenantSettingsService(redis_client=redis)

        updated = await service.set_region("tenant-1", "NG")

        assert updated.region == "NG"
        # volume=l, distance=mi is neither the US default (gal/mi) nor the NG
        # default (l/km), so the caller's explicit choice is preserved.
        assert updated.measurement_units == MeasurementUnits(
            volume="l", distance="mi"
        )

    @pytest.mark.asyncio
    async def test_set_region_rejects_invalid_region(self):
        redis = AsyncMock()
        redis.get.return_value = None
        service = TenantSettingsService(redis_client=redis)

        with pytest.raises(ValueError):
            await service.set_region("tenant-1", "DE")

    @pytest.mark.asyncio
    async def test_set_region_case_insensitive(self):
        redis = AsyncMock()
        redis.get.return_value = None
        service = TenantSettingsService(redis_client=redis)

        updated = await service.set_region("tenant-1", "ng")

        assert updated.region == "NG"
        assert updated.measurement_units == MeasurementUnits(volume="l", distance="km")


# ---------------------------------------------------------------------------
# default_depot_id — Requirement 2.2.1 (tenant-configurable depots)
# ---------------------------------------------------------------------------


class TestDefaultDepotId:
    def test_default_tenant_settings_has_no_depot(self):
        """A fresh tenant has no configured default_depot_id.

        Route planning treats this as ``no_depot_configured`` per Req 2.2.4
        rather than inventing a fallback coordinate.
        """
        settings = default_tenant_settings()
        assert settings.default_depot_id is None

    def test_to_dict_includes_default_depot_id(self):
        settings = TenantSettings(
            region="US",
            measurement_units=MeasurementUnits(volume="gal", distance="mi"),
            default_depot_id="depot_abc",
        )
        assert settings.to_dict() == {
            "region": "US",
            "measurement_units": {"volume": "gal", "distance": "mi"},
            "default_depot_id": "depot_abc",
        }

    @pytest.mark.asyncio
    async def test_get_reads_configured_default_depot_id(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {
                "region": "US",
                "measurement_units": {"volume": "gal", "distance": "mi"},
                "default_depot_id": "depot_xyz",
            }
        )
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        assert settings.default_depot_id == "depot_xyz"

    @pytest.mark.asyncio
    async def test_get_default_depot_id_convenience(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {
                "region": "US",
                "measurement_units": {"volume": "gal", "distance": "mi"},
                "default_depot_id": "depot_xyz",
            }
        )
        service = TenantSettingsService(redis_client=redis)

        assert await service.get_default_depot_id("tenant-1") == "depot_xyz"

    @pytest.mark.asyncio
    async def test_get_default_depot_id_returns_none_when_unset(self):
        service = TenantSettingsService(redis_client=None)
        assert await service.get_default_depot_id("tenant-x") is None

    @pytest.mark.asyncio
    async def test_get_coerces_non_string_depot_id_to_none(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {
                "region": "US",
                "measurement_units": {"volume": "gal", "distance": "mi"},
                "default_depot_id": 42,  # stale non-string payload
            }
        )
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")

        # Fall-open semantics: bogus values do not raise.
        assert settings.default_depot_id is None

    @pytest.mark.asyncio
    async def test_get_treats_blank_depot_id_as_none(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {
                "region": "US",
                "measurement_units": {"volume": "gal", "distance": "mi"},
                "default_depot_id": "   ",
            }
        )
        service = TenantSettingsService(redis_client=redis)

        settings = await service.get("tenant-1")
        assert settings.default_depot_id is None

    @pytest.mark.asyncio
    async def test_set_rejects_blank_default_depot_id(self):
        redis = AsyncMock()
        service = TenantSettingsService(redis_client=redis)

        bad = TenantSettings(
            region="US",
            measurement_units=MeasurementUnits(),
            default_depot_id="   ",
        )
        with pytest.raises(ValueError):
            await service.set("tenant-1", bad)

    @pytest.mark.asyncio
    async def test_set_default_depot_id_writes_and_returns(self):
        redis = AsyncMock()
        redis.get.return_value = None
        service = TenantSettingsService(redis_client=redis)

        updated = await service.set_default_depot_id("tenant-1", "depot_xyz")

        assert updated.default_depot_id == "depot_xyz"
        assert updated.region == "US"  # unchanged region default
        redis.set.assert_awaited_once()
        _, payload = redis.set.call_args.args
        assert json.loads(payload)["default_depot_id"] == "depot_xyz"

    @pytest.mark.asyncio
    async def test_set_default_depot_id_accepts_none_to_clear(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {
                "region": "US",
                "measurement_units": {"volume": "gal", "distance": "mi"},
                "default_depot_id": "depot_xyz",
            }
        )
        service = TenantSettingsService(redis_client=redis)

        updated = await service.set_default_depot_id("tenant-1", None)

        assert updated.default_depot_id is None
        _, payload = redis.set.call_args.args
        assert json.loads(payload)["default_depot_id"] is None

    @pytest.mark.asyncio
    async def test_set_default_depot_id_rejects_blank(self):
        redis = AsyncMock()
        service = TenantSettingsService(redis_client=redis)

        with pytest.raises(ValueError):
            await service.set_default_depot_id("tenant-1", "   ")
        redis.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_default_depot_id_preserves_units_and_region(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {
                "region": "NG",
                "measurement_units": {"volume": "l", "distance": "km"},
            }
        )
        service = TenantSettingsService(redis_client=redis)

        updated = await service.set_default_depot_id("tenant-ng", "depot_lagos")

        assert updated.region == "NG"
        assert updated.measurement_units == MeasurementUnits(volume="l", distance="km")
        assert updated.default_depot_id == "depot_lagos"

    @pytest.mark.asyncio
    async def test_set_region_preserves_default_depot_id(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {
                "region": "US",
                "measurement_units": {"volume": "gal", "distance": "mi"},
                "default_depot_id": "depot_abc",
            }
        )
        service = TenantSettingsService(redis_client=redis)

        updated = await service.set_region("tenant-1", "NG")

        assert updated.region == "NG"
        assert updated.default_depot_id == "depot_abc"
