"""
Tenant Settings Service — per-tenant Region + measurement_units.

Stores a tenant's Region (``US`` or ``NG``) and its display/measurement units
(volume and distance) so that API responses, route-planning output, and driver
UI can present values in the tenant's preferred system while the platform
persists canonical units internally (see ``services/unit_conversion.py``).

Defaults (Requirement 6.1.5, 6.3.1):

* New tenants → Region = ``"US"``, measurement_units = ``{"volume": "gal",
  "distance": "mi"}``.
* Existing pre-pivot tenants → Region = ``"NG"``, measurement_units =
  ``{"volume": "l", "distance": "km"}``; the migration that backfills this for
  tenants that existed before the US pivot is handled by Task 12.4 of the
  fuel-ops-hardening spec. This service only supplies the schema plus the
  default-for-new-tenants behavior used by the tenant guard middleware.

Storage: Redis key ``tenant:{tenant_id}:settings`` with a JSON-encoded payload,
following the same pattern as ``TenantInventoryConfigService`` and
``AutonomyConfigService``. When Redis is unavailable or the key is missing,
reads fall open to US / imperial defaults so no request path breaks because
a tenant has not been explicitly provisioned yet.

Validates: Requirements 6.1.5, 6.3.1, 6.3.3 (unit semantics).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Final, Literal, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types + constants
# ---------------------------------------------------------------------------

Region = Literal["US", "NG"]
VolumeUnit = Literal["gal", "l"]
DistanceUnit = Literal["mi", "km"]

VALID_REGIONS: Final[frozenset[str]] = frozenset({"US", "NG"})
VALID_VOLUME_UNITS: Final[frozenset[str]] = frozenset({"gal", "l"})
VALID_DISTANCE_UNITS: Final[frozenset[str]] = frozenset({"mi", "km"})

#: Redis key pattern for a tenant's settings document.
TENANT_SETTINGS_KEY_PATTERN: Final[str] = "tenant:{tenant_id}:settings"


@dataclass
class MeasurementUnits:
    """Tenant-preferred display units for volume and distance.

    ``volume`` selects between gallons (``"gal"``) and liters (``"l"``).
    ``distance`` selects between miles (``"mi"``) and kilometers (``"km"``).
    """

    volume: VolumeUnit = "gal"
    distance: DistanceUnit = "mi"

    def to_dict(self) -> dict[str, str]:
        return {"volume": self.volume, "distance": self.distance}


@dataclass
class TenantSettings:
    """Per-tenant region, measurement units, and default depot.

    ``default_depot_id`` is the tenant-level fallback consulted by the
    Route_Planning_Agent when a truck has no ``assigned_depot_id``. It is
    managed through :class:`fuel.depot_models.DepotRepository` (the id
    must reference a depot owned by the same tenant) and is ``None`` for
    tenants that have not yet configured any depots — in which case
    Requirement 2.2.4 dictates the platform returns HTTP 400
    ``no_depot_configured`` rather than falling back to a hardcoded
    coordinate.

    Validates: Requirements 2.2.1, 6.1.5, 6.3.1.
    """

    region: Region = "US"
    measurement_units: MeasurementUnits = field(default_factory=MeasurementUnits)
    default_depot_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "measurement_units": self.measurement_units.to_dict(),
            "default_depot_id": self.default_depot_id,
        }


# ---------------------------------------------------------------------------
# Default helpers
# ---------------------------------------------------------------------------


def default_measurement_units_for_region(region: str) -> MeasurementUnits:
    """Return the platform-default measurement units for ``region``.

    * ``"US"`` → gallons + miles (imperial).
    * ``"NG"`` → liters + kilometers (metric).

    Unknown regions fall back to the US/imperial default rather than raising
    so the caller never gets a dead-letter request from a stale tenant record.
    """
    normalized = (region or "").strip().upper()
    if normalized == "NG":
        return MeasurementUnits(volume="l", distance="km")
    return MeasurementUnits(volume="gal", distance="mi")


def default_tenant_settings() -> TenantSettings:
    """Return the default TenantSettings for a brand-new tenant (US, imperial)."""
    return TenantSettings(
        region="US",
        measurement_units=default_measurement_units_for_region("US"),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TenantSettingsService:
    """Redis-backed per-tenant settings store.

    Mirrors the contract of ``TenantInventoryConfigService``: reads fall open
    to the default ``TenantSettings`` when Redis is unavailable, when the
    tenant has no record, or when the stored payload is malformed.

    This service is the single source of truth consulted by the tenant guard
    middleware so that every request's ``TenantContext`` carries the tenant's
    Region and measurement units.
    """

    def __init__(self, redis_client: Optional[Any] = None) -> None:
        self._redis = redis_client

    # -- Key helpers ------------------------------------------------------

    @staticmethod
    def _key(tenant_id: str) -> str:
        return TENANT_SETTINGS_KEY_PATTERN.format(tenant_id=tenant_id)

    # -- Reads -----------------------------------------------------------

    async def get(self, tenant_id: str) -> TenantSettings:
        """Return the tenant's settings, falling back to US/imperial defaults.

        Never raises. Logs a warning when Redis or payload parsing fails and
        returns the default :func:`default_tenant_settings` value so callers
        can always rely on a valid ``TenantSettings`` instance.
        """
        if not tenant_id:
            return default_tenant_settings()
        if self._redis is None:
            return default_tenant_settings()

        try:
            raw = await self._redis.get(self._key(tenant_id))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TenantSettingsService: Redis lookup failed for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return default_tenant_settings()

        if raw is None:
            return default_tenant_settings()

        try:
            text = raw.decode() if isinstance(raw, bytes) else raw
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            logger.warning(
                "TenantSettingsService: malformed settings JSON for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return default_tenant_settings()

        return self._settings_from_dict(data)

    async def get_region(self, tenant_id: str) -> str:
        """Convenience accessor returning just the Region."""
        settings = await self.get(tenant_id)
        return settings.region

    async def get_measurement_units(self, tenant_id: str) -> MeasurementUnits:
        """Convenience accessor returning just the measurement units."""
        settings = await self.get(tenant_id)
        return settings.measurement_units

    async def get_default_depot_id(self, tenant_id: str) -> Optional[str]:
        """Convenience accessor returning just the configured default depot.

        Returns ``None`` when the tenant has not configured a default
        depot (including the common first-run case before any depot has
        been created). Route planning callers treat a ``None`` result as
        ``no_depot_configured`` per Requirement 2.2.4.
        """
        settings = await self.get(tenant_id)
        return settings.default_depot_id

    # -- Writes ----------------------------------------------------------

    async def set(self, tenant_id: str, settings: TenantSettings) -> None:
        """Persist ``settings`` for ``tenant_id``.

        Raises ``RuntimeError`` if no Redis client is configured and
        ``ValueError`` if the settings object contains unsupported values.
        """
        if not tenant_id:
            raise ValueError("tenant_id must be a non-empty string")
        if self._redis is None:
            raise RuntimeError(
                "TenantSettingsService: cannot write settings — no Redis client configured"
            )
        self._validate(settings)
        payload = json.dumps(settings.to_dict(), sort_keys=True)
        await self._redis.set(self._key(tenant_id), payload)
        logger.info(
            "Tenant settings updated: tenant=%s region=%s units=%s default_depot_id=%s",
            tenant_id,
            settings.region,
            settings.measurement_units.to_dict(),
            settings.default_depot_id,
        )

    async def set_region(self, tenant_id: str, region: str) -> TenantSettings:
        """Update only the Region, keeping defaults consistent for units.

        If the tenant already has explicit measurement units configured those
        are preserved. Otherwise the region's default units are applied so a
        tenant flipping to ``NG`` automatically starts displaying liters + km.
        """
        current = await self.get(tenant_id)
        new_region = (region or "").strip().upper()
        if new_region not in VALID_REGIONS:
            raise ValueError(
                f"invalid region {region!r}; expected one of {sorted(VALID_REGIONS)}"
            )
        # Only reset units to the region default when the caller previously
        # had the other region's canonical defaults; explicit overrides stick.
        region_default = default_measurement_units_for_region(new_region)
        other_default = default_measurement_units_for_region(
            "NG" if new_region == "US" else "US"
        )
        if current.measurement_units == other_default:
            units = region_default
        else:
            units = current.measurement_units
        updated = TenantSettings(
            region=new_region,
            measurement_units=units,
            default_depot_id=current.default_depot_id,
        )
        await self.set(tenant_id, updated)
        return updated

    async def set_default_depot_id(
        self, tenant_id: str, depot_id: Optional[str]
    ) -> TenantSettings:
        """Update only the tenant's ``default_depot_id``.

        Passing ``None`` clears the current default so the tenant is
        flagged as ``no_depot_configured`` on the next Route_Plan request.
        The repository layer (:class:`fuel.depot_models.DepotRepository`)
        is responsible for verifying the depot belongs to the tenant
        before the caller invokes this method — this service stores
        whatever it is given because it intentionally does not reach into
        Elasticsearch.
        """
        current = await self.get(tenant_id)
        if depot_id is not None:
            if not isinstance(depot_id, str) or not depot_id.strip():
                raise ValueError(
                    "depot_id must be a non-empty string or None"
                )
            depot_id = depot_id.strip()
        updated = TenantSettings(
            region=current.region,
            measurement_units=current.measurement_units,
            default_depot_id=depot_id,
        )
        await self.set(tenant_id, updated)
        return updated

    # -- Internal helpers -----------------------------------------------

    @staticmethod
    def _settings_from_dict(data: Any) -> TenantSettings:
        if not isinstance(data, dict):
            return default_tenant_settings()

        raw_region = str(data.get("region", "US")).strip().upper()
        region: Region = "US" if raw_region not in VALID_REGIONS else raw_region  # type: ignore[assignment]

        units_data = data.get("measurement_units") or {}
        if not isinstance(units_data, dict):
            units_data = {}

        region_default = default_measurement_units_for_region(region)
        volume_raw = str(units_data.get("volume", region_default.volume)).strip().lower()
        distance_raw = str(
            units_data.get("distance", region_default.distance)
        ).strip().lower()

        volume: VolumeUnit = (
            volume_raw if volume_raw in VALID_VOLUME_UNITS else region_default.volume  # type: ignore[assignment]
        )
        distance: DistanceUnit = (
            distance_raw
            if distance_raw in VALID_DISTANCE_UNITS
            else region_default.distance  # type: ignore[assignment]
        )
        # ``default_depot_id`` is optional. We coerce non-strings to ``None``
        # rather than raising so a stale payload with the wrong type simply
        # loses its configured default and the tenant is prompted to
        # reconfigure — matching the rest of the read-path's fall-open
        # behavior.
        raw_depot = data.get("default_depot_id")
        depot_id: Optional[str] = None
        if isinstance(raw_depot, str):
            stripped = raw_depot.strip()
            depot_id = stripped or None
        return TenantSettings(
            region=region,
            measurement_units=MeasurementUnits(volume=volume, distance=distance),
            default_depot_id=depot_id,
        )

    @staticmethod
    def _validate(settings: TenantSettings) -> None:
        if settings.region not in VALID_REGIONS:
            raise ValueError(
                f"invalid region {settings.region!r}; expected one of {sorted(VALID_REGIONS)}"
            )
        if settings.measurement_units.volume not in VALID_VOLUME_UNITS:
            raise ValueError(
                f"invalid volume unit {settings.measurement_units.volume!r}; "
                f"expected one of {sorted(VALID_VOLUME_UNITS)}"
            )
        if settings.measurement_units.distance not in VALID_DISTANCE_UNITS:
            raise ValueError(
                f"invalid distance unit {settings.measurement_units.distance!r}; "
                f"expected one of {sorted(VALID_DISTANCE_UNITS)}"
            )
        if settings.default_depot_id is not None:
            if (
                not isinstance(settings.default_depot_id, str)
                or not settings.default_depot_id.strip()
            ):
                raise ValueError(
                    "default_depot_id must be a non-empty string or None"
                )


__all__ = [
    "DistanceUnit",
    "MeasurementUnits",
    "Region",
    "TENANT_SETTINGS_KEY_PATTERN",
    "TenantSettings",
    "TenantSettingsService",
    "VALID_DISTANCE_UNITS",
    "VALID_REGIONS",
    "VALID_VOLUME_UNITS",
    "VolumeUnit",
    "default_measurement_units_for_region",
    "default_tenant_settings",
]
