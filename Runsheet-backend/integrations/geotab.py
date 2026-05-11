"""
Geotab Connector — GPS/ELD reference integration (Req 5.4.*).

Implements the Geotab side of the pluggable integration framework
introduced in Capability 5 / Phase 9 of the fuel-ops hardening spec.
Geotab is an ELD / fleet telematics provider whose devices report
truck position, speed, engine state, odometer, and driver HOS status.

The connector:

* ``connect(credentials)`` — performs an ``Authenticate`` call against
  Geotab's JSON-RPC-style API, persists the resulting credentials +
  ``session_id`` envelope into the Tenant_Credentials_Vault, and
  returns a :class:`integrations.connector_base.ConnectionResult`
  pointing at the opaque vault reference.

* ``sync_pull(since)`` — polls the Geotab ``Get`` endpoint for
  ``DeviceStatusInfo`` records (the vendor-recommended entity that
  bundles latitude, longitude, speed, ignition state, and driver
  into a single snapshot) and, in parallel, the latest
  ``DutyStatusLog`` per device so each persisted telemetry row carries
  the driver's HOS status. Each reading is persisted to the
  ``truck_telemetry`` ES index (Req 5.4.3) and, when younger than
  :data:`DEFAULT_FRESHNESS_SECONDS` (300s, Req 5.4.4), updates the
  matching ``trucks`` record's ``current_location`` +
  ``current_location_at`` fields.

* ``sync_push(payload)`` — no-op. Geotab is a read-only telematics
  provider in this platform. Returns a success :class:`SyncRun` with
  ``{"skipped_noop": 1}`` so the Integration_Scheduler can still log a
  tick without tripping error accounting (mirrors the Veeder-Root
  contract).

* ``disconnect()`` — removes the credentials envelope from the vault.
  Idempotent.

Cross-cutting invariants enforced here:

* **Session-token renewal on invalid session (Req 5.4.5).** The Geotab
  API returns an ``InvalidUserException`` (typically wrapped in an
  HTTP 403) when the cached ``session_id`` expires. Every call routes
  through :meth:`_call_with_reauth` which, on the first failure,
  re-runs ``Authenticate`` once and retries. A second failure bubbles
  out as :class:`GeotabSessionExpired` so the scheduler flips the
  instance to ``status="error"`` with ``last_error="session_expired"``.

* **Credentials stay in the vault.** ``connect()`` never logs or echoes
  plaintext credentials; only the opaque ``credentials_ref`` leaves
  the connector (Requirement 5.1.8). Logs reference the ``session_id``
  / ``credentials_ref`` only.

* **Lazy ``mygeotab`` import.** The ``mygeotab`` SDK is imported only
  inside the HTTP/SDK call path so unit tests (and bootstrap imports)
  never pay the dependency cost. When the library is not installed
  the connector falls back to a direct HTTPS ``POST`` to
  ``{server}/apiv1`` using the documented JSON-RPC-like envelope. The
  SDK's API is synchronous; we run every SDK call under
  :func:`asyncio.to_thread` to keep the event loop live.

* **Device-to-truck mapping.** ``instance.config["device_map"]`` is a
  ``{"<geotab_device_id>": "<truck_id>"}`` dict. Devices not present
  in the map land in ``truck_telemetry`` with ``truck_id=None`` so
  unmapped devices remain visible in diagnostic dashboards (Req
  5.4.3, 5.4.4).

* **Per-reading error isolation.** A single malformed / unmappable
  record is logged and counted (``skipped_invalid`` / ``skipped_stale``)
  but never aborts the whole run.

Default cron schedule: ``* * * * *`` (every minute, Req 5.4.5). The
value is set on :class:`IntegrationInstance.schedule_cron` by the
admin UI or the tenant bootstrap helper — this module does not mutate
cron state. The default is surfaced as :data:`DEFAULT_SCHEDULE_CRON`
and in the provider-catalog description so the Marketplace UI shows
it verbatim.

Validates: Requirements 5.4.1, 5.4.2, 5.4.3, 5.4.4, 5.4.5.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Tuple
from uuid import uuid4

import httpx

from fuel.services.fuel_ops_es_mappings import TRUCK_TELEMETRY_INDEX
from integrations.connector_base import (
    ConnectionResult,
    IntegrationConnector,
    SyncRun,
)
from integrations.provider_catalog import (
    ProviderCatalogEntry,
    register_provider,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default Geotab server. Vendor documentation recommends hitting
#: ``my.geotab.com`` for auto-routing; per-tenant overrides live on
#: ``instance.config["server"]`` / the credentials envelope.
DEFAULT_SERVER: str = "my.geotab.com"

#: Default HTTPS timeout on every API call. The Geotab API is mostly
#: < 2s at the p99; we pad generously to absorb long-running ``Get``
#: calls on dense fleets without stalling the scheduler.
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 15.0

#: Vault key used to persist the credentials envelope. Scoped by
#: tenant + provider so a single vault lookup resolves the whole
#: envelope.
VAULT_CREDENTIAL_KEY: str = "geotab_session"

#: Default 60-second cron per Requirement 5.4.5. The schedule is owned
#: by the :class:`IntegrationInstance.schedule_cron` field; this
#: constant is surfaced through the provider catalog's ``description``
#: and the admin-UI prefill so operators see the canonical default.
DEFAULT_SCHEDULE_CRON: str = "* * * * *"

#: Telemetry freshness threshold in seconds per Requirement 5.4.4.
#: Truck records' ``current_location`` is updated only when the latest
#: telemetry reading is younger than this — older readings still land
#: in ``truck_telemetry`` but are treated as stale for routing.
DEFAULT_FRESHNESS_SECONDS: int = 300

#: Trucks ES index — the Geotab connector updates ``current_location``
#: and ``current_location_at`` fields on matching documents when the
#: reading is fresh.
TRUCKS_INDEX: str = "trucks"

#: Canonical ``last_error`` value the scheduler surfaces through the
#: admin UI when the re-auth + retry cycle still fails (Req 5.4.5).
#: The string is user-facing; keep it stable so UI copy does not drift.
SESSION_EXPIRED_REASON: str = "session_expired"

#: ``IntegrationInstance.config`` key carrying the device-to-truck
#: mapping. Shape: ``{"<geotab_device_id>": "<truck_id>"}``. Devices
#: not in the map still land in ``truck_telemetry`` with
#: ``truck_id=None`` for audit visibility.
_CONFIG_DEVICE_MAP_KEY: str = "device_map"

#: ``IntegrationInstance.config`` key allowing per-instance override
#: of the vendor server. Optional; defaults to the credentials-envelope
#: value.
_CONFIG_SERVER_KEY: str = "server"

#: Credential fields required by ``connect``.
_CRED_USERNAME_KEY: str = "username"
_CRED_PASSWORD_KEY: str = "password"
_CRED_DATABASE_KEY: str = "database"
_CRED_SERVER_KEY: str = "server"

#: Envelope fields stamped by :meth:`connect` after a successful
#: authenticate.
_ENV_SESSION_ID_KEY: str = "session_id"
_ENV_SESSION_EXPIRES_AT_KEY: str = "session_expires_at"

#: Vendor error code that means "the session is no longer valid".
#: Geotab wraps this inside the JSON ``error.errors[0].name`` field on
#: the ``apiv1`` envelope; we also accept the string appearing in a
#: bare ``error.message`` as a safety net for older servers.
INVALID_USER_EXCEPTION: str = "InvalidUserException"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GeotabSessionExpired(RuntimeError):
    """Raised after a re-auth + retry cycle still returns InvalidUserException.

    The scheduler catches this and flips the owning instance to
    ``status="error"`` with ``last_error="session_expired"`` (Req
    5.4.5).
    """

    def __init__(self, tenant_id: str, instance_id: str) -> None:
        super().__init__(
            f"Geotab session expired for tenant={tenant_id} "
            f"instance={instance_id}; re-authorize via the Marketplace UI"
        )
        self.tenant_id = tenant_id
        self.instance_id = instance_id


class GeotabConfigError(RuntimeError):
    """Raised when the per-instance ``config`` / ``credentials`` is invalid.

    The scheduler catches this as a terminal failure for the current
    tick and surfaces ``last_error`` on the owning instance so the
    operator sees a concrete "missing field" message in the admin UI.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    """Return an ISO-8601 UTC timestamp with ``Z`` suffix."""

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> Optional[datetime]:
    """Coerce a Geotab timestamp to :class:`datetime` (UTC)."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _safe_float(value: Any) -> Optional[float]:
    """Coerce ``value`` to ``float`` or return ``None`` when not numeric."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_invalid_user_exception(payload: Any) -> bool:
    """Return True when the Geotab response body signals invalid session.

    Geotab's JSON-RPC-like envelope embeds the error tuple at
    ``error.errors[0].name`` with the vendor string
    ``InvalidUserException``. Some older servers instead put the
    string into ``error.message``; we accept both to keep the session
    renewal logic robust across versions.
    """

    if payload is None:
        return False
    if isinstance(payload, Mapping):
        err = payload.get("error")
        if err is None:
            return False
        if isinstance(err, Mapping):
            errors = err.get("errors") or []
            if isinstance(errors, list):
                for entry in errors:
                    if isinstance(entry, Mapping):
                        name = entry.get("name") or ""
                        if INVALID_USER_EXCEPTION in str(name):
                            return True
            message = err.get("message")
            if isinstance(message, str) and INVALID_USER_EXCEPTION in message:
                return True
        elif isinstance(err, str) and INVALID_USER_EXCEPTION in err:
            return True
    elif isinstance(payload, str) and INVALID_USER_EXCEPTION in payload:
        return True
    return False


def _haversine_miles(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Compute the great-circle distance between two GPS points in miles.

    Uses the haversine formula. This is a fallback for computing segment
    miles when odometer data is unavailable.

    Args:
        lat1: Latitude of point 1 in decimal degrees.
        lon1: Longitude of point 1 in decimal degrees.
        lat2: Latitude of point 2 in decimal degrees.
        lon2: Longitude of point 2 in decimal degrees.

    Returns:
        Distance in miles.
    """
    import math as _math

    R_MILES = 3958.8  # Earth radius in miles

    lat1_rad = _math.radians(lat1)
    lat2_rad = _math.radians(lat2)
    dlat = _math.radians(lat2 - lat1)
    dlon = _math.radians(lon2 - lon1)

    a = (
        _math.sin(dlat / 2) ** 2
        + _math.cos(lat1_rad) * _math.cos(lat2_rad) * _math.sin(dlon / 2) ** 2
    )
    c = 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1 - a))

    return R_MILES * c


# ---------------------------------------------------------------------------
# Provider-catalog entry (Task 9.10 convenience)
# ---------------------------------------------------------------------------


def build_catalog_entry() -> ProviderCatalogEntry:
    """Return the :class:`ProviderCatalogEntry` for this connector.

    Task 9.10 will call :func:`register_catalog_entry` from the
    integrations bootstrap; this helper is also used directly by
    ``/api/integrations/providers`` tests that want to assert on the
    shape without triggering registration side-effects.
    """

    description = (
        "Poll Geotab ELDs for truck position, speed, engine state, "
        "odometer, and HOS status; updates trucks.current_location "
        "when the latest reading is fresh (<300s). Default schedule: "
        "every minute."
    )
    return ProviderCatalogEntry(
        provider_name="geotab",
        category="gps_eld",
        description=description,
        required_credential_fields=[
            _CRED_USERNAME_KEY,
            _CRED_PASSWORD_KEY,
            _CRED_DATABASE_KEY,
            _CRED_SERVER_KEY,
        ],
        doc_url="https://geotab.github.io/sdk/software/api/reference/",
        auth_mode="basic",
    )


def register_catalog_entry() -> ProviderCatalogEntry:
    """Register the connector with the shared provider catalog.

    Task 9.10 wires every connector into the catalog at bootstrap
    time; this helper is kept here (rather than inline at module
    import time) so a test that imports this module does not
    auto-register with the global catalog.
    """

    return register_provider(build_catalog_entry())


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _extract_device_id(record: Mapping[str, Any]) -> Optional[str]:
    """Pull the Geotab device id out of a ``DeviceStatusInfo`` row.

    The Geotab API returns the owning device as either an embedded
    object (``{"id": "...", "name": "..."}``) or, on compact responses,
    a bare string. We accept both.
    """

    device = record.get("device")
    if isinstance(device, Mapping):
        dev_id = device.get("id")
        if isinstance(dev_id, str) and dev_id.strip():
            return dev_id.strip()
    elif isinstance(device, str) and device.strip():
        return device.strip()
    # Some SDK responses flatten to deviceId.
    flat = record.get("deviceId")
    if isinstance(flat, str) and flat.strip():
        return flat.strip()
    return None


def _extract_driver_id(record: Mapping[str, Any]) -> Optional[str]:
    """Pull the active driver id out of a ``DeviceStatusInfo`` row."""

    driver = record.get("driver")
    if isinstance(driver, Mapping):
        drv_id = driver.get("id")
        if isinstance(drv_id, str) and drv_id.strip() and drv_id.strip().lower() != "unknowndriverid":
            return drv_id.strip()
    elif isinstance(driver, str):
        value = driver.strip()
        if value and value.lower() != "unknowndriverid":
            return value
    return None


def _normalize_device_status(
    record: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Convert a Geotab ``DeviceStatusInfo`` row into our reading shape.

    Returns ``None`` when the row is missing the minimum viable fields
    (device id + latitude + longitude). Speed is converted from the
    Geotab default of km/h (float); the field is ``speed`` for
    ``DeviceStatusInfo`` and some servers emit ``Speed`` — accept both.
    """

    device_id = _extract_device_id(record)
    if not device_id:
        return None
    lat = _safe_float(record.get("latitude") or record.get("Latitude"))
    lon = _safe_float(record.get("longitude") or record.get("Longitude"))
    if lat is None or lon is None:
        return None
    speed_kph = _safe_float(
        record.get("speed")
        if record.get("speed") is not None
        else record.get("Speed")
    )
    engine_on_raw = record.get("isDeviceCommunicating")
    if engine_on_raw is None:
        engine_on_raw = record.get("isIgnitionOn")
    if engine_on_raw is None:
        engine_on_raw = record.get("engineOn")
    engine_on: Optional[bool]
    if isinstance(engine_on_raw, bool):
        engine_on = engine_on_raw
    elif isinstance(engine_on_raw, str):
        engine_on = engine_on_raw.strip().lower() in {"true", "1", "yes"}
    else:
        engine_on = None
    odometer_km = _safe_float(
        record.get("odometer")
        if record.get("odometer") is not None
        else record.get("Odometer")
    )
    recorded_at = _parse_iso(
        record.get("dateTime")
        or record.get("DateTime")
        or record.get("recordedAt")
    )
    return {
        "device_id": device_id,
        "driver_id": _extract_driver_id(record),
        "latitude": lat,
        "longitude": lon,
        "speed_kph": speed_kph,
        "engine_on": engine_on,
        "odometer_km": odometer_km,
        "hos_status": None,
        "recorded_at": recorded_at or _utcnow(),
    }


def _normalize_duty_status(
    record: Mapping[str, Any],
) -> Optional[Tuple[str, str]]:
    """Return ``(device_id, hos_status)`` for a DutyStatusLog record.

    Geotab uses the vendor strings ``D``/``OnDuty``/``Driving``/``SB``/
    ``OffDuty``/``PersonalConveyance``/``YardMove``. We pass the raw
    status through — the consumer can normalize further downstream.
    """

    device_id = _extract_device_id(record)
    if not device_id:
        return None
    status = record.get("status") or record.get("Status")
    if not isinstance(status, str) or not status.strip():
        return None
    return device_id, status.strip()


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class GeotabConnector(IntegrationConnector):
    """Geotab ELD adapter (gps_eld category).

    Args:
        tenant_id: Owning tenant.
        instance_id: Owning :class:`IntegrationInstance` id — stamped
            on every :class:`SyncRun` the connector returns.
        instance_config: The owning ``IntegrationInstance.config``
            dict. Carries the ``device_map`` and optional ``server``
            override.
        credentials_vault: Required; the shared
            :class:`services.credentials_vault.TenantCredentialsVault`.
        credentials_ref: Existing vault reference; ``None`` means this
            instance has not completed ``connect()`` yet.
        es_service: Required for persistence. Must expose
            :meth:`index_document` and :meth:`update_document`.
        http_client: Optional injected :class:`httpx.AsyncClient`.
            When ``None`` HTTP-fallback calls reuse a lazily created
            instance-owned client. Tests inject a mock here.
        sdk_call: Optional zero-arg-free callable used to invoke the
            Geotab SDK (``MyGeotabAPI``-style). When ``None`` the
            connector probes for :mod:`mygeotab` and falls back to a
            direct HTTPS POST against ``{server}/apiv1`` when the
            library is not installed. Tests inject a scripted callable
            here.
        freshness_seconds: Override the 300-second freshness threshold
            used to gate ``trucks.current_location`` updates.
        http_timeout_seconds: Per-call HTTPS timeout.
        truck_telemetry_index / trucks_index: Override the target ES
            indices. Defaults match production.
        clock: Zero-arg callable returning the current UTC datetime;
            injected for deterministic freshness tests.
    """

    category: ClassVar[str] = "gps_eld"
    provider_name: ClassVar[str] = "geotab"

    def __init__(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        instance_config: Mapping[str, Any],
        credentials_vault: Any,
        credentials_ref: Optional[str] = None,
        es_service: Any = None,
        http_client: Optional[httpx.AsyncClient] = None,
        sdk_call: Optional[Any] = None,
        freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
        http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        truck_telemetry_index: str = TRUCK_TELEMETRY_INDEX,
        trucks_index: str = TRUCKS_INDEX,
        clock: Any = _utcnow,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError("instance_id must be a non-empty string")
        if credentials_vault is None:
            raise ValueError("credentials_vault is required")
        if freshness_seconds <= 0:
            raise ValueError("freshness_seconds must be positive")
        if http_timeout_seconds <= 0:
            raise ValueError("http_timeout_seconds must be positive")

        self._tenant_id = tenant_id
        self._instance_id = instance_id
        self._config: Dict[str, Any] = dict(instance_config or {})
        self._vault = credentials_vault
        self._credentials_ref = credentials_ref
        self._es = es_service
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._sdk_call = sdk_call
        self._freshness_seconds = int(freshness_seconds)
        self._http_timeout = float(http_timeout_seconds)
        self._truck_telemetry_index = truck_telemetry_index
        self._trucks_index = trucks_index
        self._clock = clock

        # In-memory cache of the credentials envelope. Populated on
        # demand the first time sync_pull runs and refreshed after
        # every successful re-auth.
        self._cached_credentials: Optional[Dict[str, Any]] = None

        # IFTA boundary detection hook (Task 12.3 / Req 7.1).
        # When set, sync_pull processes GPS readings through the
        # StateBoundaryDetector and calls IFTAReporter.record_trip_segment()
        # on detected state boundary crossings. Optional — when None,
        # sync_pull operates normally without IFTA processing.
        self._ifta_reporter: Optional[Any] = None
        self._state_boundary_detector: Optional[Any] = None

        # Per-truck state tracking for IFTA boundary detection.
        # Maps truck_id → {"last_state": str, "last_odometer_km": float,
        #                   "last_lat": float, "last_lon": float}
        self._ifta_truck_state: Dict[str, Dict[str, Any]] = {}


    # ------------------------------------------------------------------
    # IFTA Boundary Detection Hook (Task 12.3 / Req 7.1)
    # ------------------------------------------------------------------

    def set_ifta_reporter(
        self,
        ifta_reporter: Any,
        state_boundary_detector: Any,
    ) -> None:
        """Inject the IFTA reporter and state boundary detector.

        When both are set, ``sync_pull`` will process GPS readings
        through the StateBoundaryDetector and call
        ``IFTAReporter.record_trip_segment()`` on detected state
        boundary crossings.

        This hook is optional — if not configured, sync_pull operates
        normally without IFTA processing.

        Args:
            ifta_reporter: An :class:`IFTAReporter` instance with an
                async ``record_trip_segment()`` method.
            state_boundary_detector: A :class:`StateBoundaryDetector`
                instance with a ``get_state(lat, lon)`` method.

        Validates: Requirement 7.1
        """
        self._ifta_reporter = ifta_reporter
        self._state_boundary_detector = state_boundary_detector
        logger.info(
            "GeotabConnector: IFTA reporter hook configured for "
            "tenant=%s instance=%s",
            self._tenant_id,
            self._instance_id,
        )

    async def _process_ifta_boundary_check(
        self,
        reading: Mapping[str, Any],
        truck_id: str,
        recorded_at: datetime,
    ) -> None:
        """Check for state boundary crossings and record IFTA trip segments.

        Called from ``_process_reading`` for each GPS reading that has a
        mapped truck_id. Compares the current GPS state against the last
        known state for this truck. When a crossing is detected, computes
        the miles driven in the exited state using odometer data (preferred)
        or haversine distance between GPS points (fallback), then calls
        ``IFTAReporter.record_trip_segment()``.

        Per-truck state is tracked in ``_ifta_truck_state`` which maps
        truck_id to the last known state, odometer, and coordinates.

        Args:
            reading: Normalized telemetry reading dict with latitude,
                longitude, odometer_km, and recorded_at fields.
            truck_id: The mapped truck identifier.
            recorded_at: Timestamp of the GPS reading.

        Validates: Requirement 7.1
        """
        if self._ifta_reporter is None or self._state_boundary_detector is None:
            return

        lat = _safe_float(reading.get("latitude"))
        lon = _safe_float(reading.get("longitude"))
        if lat is None or lon is None:
            return

        # Determine current state from GPS coordinates
        current_state = self._state_boundary_detector.get_state(lat, lon)
        if current_state is None:
            return

        odometer_km = _safe_float(reading.get("odometer_km"))

        # Get or initialize per-truck state
        truck_state = self._ifta_truck_state.get(truck_id)

        if truck_state is None:
            # First reading for this truck — initialize state tracking
            self._ifta_truck_state[truck_id] = {
                "last_state": current_state,
                "last_odometer_km": odometer_km,
                "last_lat": lat,
                "last_lon": lon,
                "last_timestamp": recorded_at,
            }
            return

        prev_state = truck_state.get("last_state")

        if prev_state is None or current_state == prev_state:
            # No crossing — update tracking state
            self._ifta_truck_state[truck_id] = {
                "last_state": current_state,
                "last_odometer_km": odometer_km,
                "last_lat": lat,
                "last_lon": lon,
                "last_timestamp": recorded_at,
            }
            return

        # State boundary crossing detected!
        # Compute miles driven in the exited state (from_state = prev_state)
        miles = self._compute_segment_miles(
            truck_state=truck_state,
            current_odometer_km=odometer_km,
            current_lat=lat,
            current_lon=lon,
        )

        # Record the trip segment via IFTAReporter
        try:
            await self._ifta_reporter.record_trip_segment(
                tenant_id=self._tenant_id,
                truck_id=truck_id,
                from_state=prev_state,
                to_state=current_state,
                miles=miles,
                timestamp=recorded_at,
                source="geotab",
            )
            logger.info(
                "GeotabConnector: IFTA boundary crossing recorded "
                "truck=%s %s→%s %.1f miles tenant=%s",
                truck_id,
                prev_state,
                current_state,
                miles,
                self._tenant_id,
            )
        except Exception as exc:
            # IFTA recording failures are non-fatal — log and continue
            logger.warning(
                "GeotabConnector: IFTA record_trip_segment failed "
                "truck=%s %s→%s tenant=%s: %s",
                truck_id,
                prev_state,
                current_state,
                self._tenant_id,
                exc,
            )

        # Update tracking state to the new state
        self._ifta_truck_state[truck_id] = {
            "last_state": current_state,
            "last_odometer_km": odometer_km,
            "last_lat": lat,
            "last_lon": lon,
            "last_timestamp": recorded_at,
        }

    def _compute_segment_miles(
        self,
        truck_state: Dict[str, Any],
        current_odometer_km: Optional[float],
        current_lat: float,
        current_lon: float,
    ) -> float:
        """Compute miles driven in a segment using odometer or haversine.

        Prefers odometer-based computation when both the previous and
        current odometer readings are available. Falls back to haversine
        distance between the last known GPS point and the current point.

        Args:
            truck_state: Previous tracking state for the truck.
            current_odometer_km: Current odometer reading in km (may be None).
            current_lat: Current latitude.
            current_lon: Current longitude.

        Returns:
            Miles driven in the segment. Returns 0.0 if computation
            is not possible.
        """
        prev_odometer_km = truck_state.get("last_odometer_km")

        # Prefer odometer-based distance
        if (
            prev_odometer_km is not None
            and current_odometer_km is not None
            and current_odometer_km >= prev_odometer_km
        ):
            distance_km = current_odometer_km - prev_odometer_km
            return round(distance_km * 0.621371, 1)  # km → miles

        # Fallback: haversine distance between GPS points
        prev_lat = truck_state.get("last_lat")
        prev_lon = truck_state.get("last_lon")
        if prev_lat is not None and prev_lon is not None:
            return round(
                _haversine_miles(prev_lat, prev_lon, current_lat, current_lon),
                1,
            )

        return 0.0

    # ------------------------------------------------------------------
    # IntegrationConnector API
    # ------------------------------------------------------------------

    async def connect(self, credentials: Mapping[str, Any]) -> ConnectionResult:
        """Validate credentials, authenticate, and persist the envelope.

        Expected ``credentials`` shape:

            {
                "username": "ops@example.com",
                "password": "...",
                "database": "example_co",
                "server": "my.geotab.com"
            }

        On success the connector:
            1. Calls ``Authenticate`` through the SDK (or HTTPS
               fallback) to obtain a ``session_id`` + server override.
            2. Persists ``{username, password, database, server,
               session_id, session_expires_at}`` into the vault.
            3. Returns a :class:`ConnectionResult` with
               ``status="connected"`` and the ``server`` + ``database``
               metadata fields the admin UI displays.

        Failures return ``status="error"`` with a human-readable
        ``message``. The connector never logs the password — the log
        message references the username + database only.
        """

        missing = [
            k
            for k in (
                _CRED_USERNAME_KEY,
                _CRED_PASSWORD_KEY,
                _CRED_DATABASE_KEY,
            )
            if not credentials.get(k)
        ]
        if missing:
            return ConnectionResult(
                status="error",
                message=(
                    f"missing required credential fields: {sorted(missing)}"
                ),
            )

        username = str(credentials[_CRED_USERNAME_KEY]).strip()
        password = str(credentials[_CRED_PASSWORD_KEY])
        database = str(credentials[_CRED_DATABASE_KEY]).strip()
        server = str(
            credentials.get(_CRED_SERVER_KEY) or DEFAULT_SERVER
        ).strip() or DEFAULT_SERVER

        try:
            auth_result = await self._authenticate(
                username=username,
                password=password,
                database=database,
                server=server,
            )
        except GeotabConfigError as exc:
            return ConnectionResult(status="error", message=str(exc))
        except Exception as exc:
            logger.warning(
                "GeotabConnector.connect: authenticate failed "
                "tenant=%s instance=%s database=%s: %s",
                self._tenant_id,
                self._instance_id,
                database,
                exc,
            )
            return ConnectionResult(
                status="error",
                message=f"authenticate failed: {exc}",
            )

        session_id = auth_result.get(_ENV_SESSION_ID_KEY)
        if not isinstance(session_id, str) or not session_id.strip():
            return ConnectionResult(
                status="error",
                message="authenticate returned empty session_id",
            )

        envelope: Dict[str, Any] = {
            _CRED_USERNAME_KEY: username,
            _CRED_PASSWORD_KEY: password,
            _CRED_DATABASE_KEY: database,
            _CRED_SERVER_KEY: str(auth_result.get("server") or server),
            _ENV_SESSION_ID_KEY: session_id.strip(),
            _ENV_SESSION_EXPIRES_AT_KEY: auth_result.get(
                _ENV_SESSION_EXPIRES_AT_KEY
            )
            or _iso(_utcnow()),
        }

        ref = await self._vault.put(
            tenant_id=self._tenant_id,
            key=VAULT_CREDENTIAL_KEY,
            plaintext=envelope,
            provider_name=self.provider_name,
        )
        self._credentials_ref = ref
        self._cached_credentials = dict(envelope)

        logger.info(
            "GeotabConnector.connect: stored credentials tenant=%s "
            "instance=%s database=%s server=%s credentials_ref=%s",
            self._tenant_id,
            self._instance_id,
            database,
            envelope[_CRED_SERVER_KEY],
            ref,
        )
        return ConnectionResult(
            status="connected",
            credentials_ref=ref,
            metadata={
                "database": database,
                "server": envelope[_CRED_SERVER_KEY],
            },
        )

    async def sync_pull(self, since: datetime) -> SyncRun:
        """Poll Geotab for device status + HOS logs and persist telemetry.

        Returns a terminal :class:`SyncRun`. On second auth failure the
        :class:`GeotabSessionExpired` path surfaces as an ``error``
        SyncRun with ``error_details="session_expired: ..."`` so the
        scheduler maps ``last_error`` verbatim (Req 5.4.5).
        """

        run_id = f"geotab_pull_{uuid4()}"
        started_at = _utcnow()
        counts: Dict[str, int] = {
            "readings_fetched": 0,
            "readings_persisted": 0,
            "trucks_updated": 0,
            "skipped_unmapped": 0,
            "skipped_stale": 0,
            "skipped_invalid": 0,
        }

        try:
            # 1) Fetch current device statuses.
            raw_statuses = await self._call_with_reauth(
                method="Get",
                params={"typeName": "DeviceStatusInfo"},
            )
            statuses = self._extract_result_list(raw_statuses)

            # 2) Fetch the most-recent DutyStatusLog entries so each
            #    telemetry row can carry the active driver HOS status.
            try:
                raw_duty = await self._call_with_reauth(
                    method="Get",
                    params={
                        "typeName": "DutyStatusLog",
                        "search": {
                            "fromDate": _iso(since),
                            "toDate": _iso(_utcnow()),
                        },
                        "resultsLimit": 1000,
                    },
                )
                duty_records = self._extract_result_list(raw_duty)
            except GeotabSessionExpired:
                # Re-raise — the caller treats this as a terminal
                # failure for the tick.
                raise
            except Exception as exc:
                logger.warning(
                    "GeotabConnector.sync_pull: DutyStatusLog fetch "
                    "failed tenant=%s: %s — proceeding without HOS",
                    self._tenant_id,
                    exc,
                )
                duty_records = []

            hos_by_device: Dict[str, str] = {}
            for entry in duty_records:
                if not isinstance(entry, Mapping):
                    continue
                parsed = _normalize_duty_status(entry)
                if parsed is None:
                    continue
                device_id, status = parsed
                hos_by_device[device_id] = status

            # 3) Normalize + persist.
            counts["readings_fetched"] = len(statuses)
            device_map = self._config.get(_CONFIG_DEVICE_MAP_KEY) or {}
            if not isinstance(device_map, Mapping):
                logger.warning(
                    "GeotabConnector.sync_pull: config.device_map is "
                    "not a mapping for tenant=%s instance=%s — "
                    "treating as empty",
                    self._tenant_id,
                    self._instance_id,
                )
                device_map = {}

            now = self._clock() if callable(self._clock) else _utcnow()
            if isinstance(now, (int, float)):
                now = datetime.fromtimestamp(float(now), tz=timezone.utc)

            for raw in statuses:
                if not isinstance(raw, Mapping):
                    counts["skipped_invalid"] += 1
                    continue
                reading = _normalize_device_status(raw)
                if reading is None:
                    counts["skipped_invalid"] += 1
                    continue
                reading["hos_status"] = hos_by_device.get(reading["device_id"])
                try:
                    await self._process_reading(
                        reading,
                        device_map=device_map,
                        now=now,
                        counts=counts,
                    )
                except Exception as exc:
                    counts["skipped_invalid"] += 1
                    logger.warning(
                        "GeotabConnector.sync_pull: failed to process "
                        "reading device=%s tenant=%s: %s",
                        reading.get("device_id"),
                        self._tenant_id,
                        exc,
                    )

        except GeotabSessionExpired as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason=SESSION_EXPIRED_REASON,
                exc=exc,
            )
        except GeotabConfigError as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason="config_error",
                exc=exc,
            )
        except asyncio.TimeoutError as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason="timeout",
                exc=exc,
            )
        except Exception as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason=None,
                exc=exc,
            )

        finished_at = _utcnow()
        if (
            counts["readings_fetched"] > 0
            and counts["readings_persisted"] < counts["readings_fetched"]
        ):
            status = "partial"
        else:
            status = "success"
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation="pull",
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            record_counts=counts,
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )

    async def sync_push(self, payload: Mapping[str, Any]) -> SyncRun:
        """Geotab is read-only; this call is a no-op.

        Returns a success :class:`SyncRun` with
        ``{"skipped_noop": 1}`` so the scheduler can persist the run
        uniformly without tripping error accounting (mirrors the
        Veeder-Root connector contract).
        """

        run_id = f"geotab_push_{uuid4()}"
        started_at = _utcnow()
        finished_at = _utcnow()
        logger.debug(
            "GeotabConnector.sync_push: no-op for tenant=%s "
            "instance=%s (payload ignored)",
            self._tenant_id,
            self._instance_id,
        )
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation="push",
            started_at=started_at,
            finished_at=finished_at,
            status="success",
            record_counts={"skipped_noop": 1},
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )

    async def disconnect(self) -> None:
        """Remove the credentials envelope from the vault.

        Idempotent. Vault failures are logged but never raised — the
        logical "disconnect" state is "the connector is not configured
        any more", and we consider the integration-instance deletion
        flow authoritative.
        """

        try:
            if not self._credentials_ref:
                return
            try:
                await self._vault.delete(self._tenant_id, self._credentials_ref)
            except Exception as exc:
                logger.warning(
                    "GeotabConnector.disconnect: vault delete failed "
                    "tenant=%s ref=%s: %s",
                    self._tenant_id,
                    self._credentials_ref,
                    exc,
                )
            self._credentials_ref = None
            self._cached_credentials = None
        finally:
            await self._close_owned_http_client()

    # ------------------------------------------------------------------
    # Auth + call orchestration
    # ------------------------------------------------------------------

    async def _authenticate(
        self,
        *,
        username: str,
        password: str,
        database: str,
        server: str,
    ) -> Dict[str, Any]:
        """Run the Geotab ``Authenticate`` call.

        Returns a dict with ``session_id``, ``server``, and (when the
        server supplies one) ``session_expires_at``. Prefers the
        ``mygeotab`` SDK when available; falls back to a direct HTTPS
        ``POST`` to ``{server}/apiv1`` otherwise.
        """

        # Injected SDK call for deterministic tests takes precedence.
        if self._sdk_call is not None:
            payload = await self._invoke_sdk(
                "Authenticate",
                {
                    "userName": username,
                    "password": password,
                    "database": database,
                },
                server=server,
                session_id=None,
            )
            return self._parse_authenticate_response(payload, fallback_server=server)

        # Attempt to use the real mygeotab SDK when installed; fall
        # back to HTTPS otherwise. The SDK is synchronous, so we wrap
        # the blocking call in asyncio.to_thread.
        try:
            import mygeotab  # type: ignore  # noqa: F401

            def _authenticate_sync() -> Dict[str, Any]:
                api = mygeotab.API(  # type: ignore[attr-defined]
                    username=username,
                    password=password,
                    database=database,
                    server=server,
                )
                api.authenticate()
                credentials = getattr(api, "credentials", None)
                if credentials is None:
                    return {}
                # The SDK exposes the session id as ``session_id`` /
                # ``sessionId`` depending on version. We normalize.
                return {
                    _ENV_SESSION_ID_KEY: (
                        getattr(credentials, "session_id", None)
                        or getattr(credentials, "sessionId", None)
                    ),
                    _CRED_SERVER_KEY: (
                        getattr(credentials, "server", None) or server
                    ),
                    _CRED_DATABASE_KEY: (
                        getattr(credentials, "database", None) or database
                    ),
                    _CRED_USERNAME_KEY: (
                        getattr(credentials, "username", None) or username
                    ),
                }

            result = await asyncio.to_thread(_authenticate_sync)
            if not result.get(_ENV_SESSION_ID_KEY):
                raise GeotabConfigError(
                    "mygeotab.authenticate returned no session_id"
                )
            return {
                _ENV_SESSION_ID_KEY: result[_ENV_SESSION_ID_KEY],
                _CRED_SERVER_KEY: result.get(_CRED_SERVER_KEY, server),
                _ENV_SESSION_EXPIRES_AT_KEY: _iso(_utcnow()),
            }
        except ImportError:
            pass
        except GeotabConfigError:
            raise
        except Exception as exc:
            raise GeotabConfigError(
                f"mygeotab authenticate failed: {exc}"
            ) from exc

        # HTTPS fallback: direct JSON-RPC-style POST.
        body = await self._post_apiv1(
            server=server,
            payload={
                "method": "Authenticate",
                "params": {
                    "userName": username,
                    "password": password,
                    "database": database,
                },
            },
        )
        return self._parse_authenticate_response(body, fallback_server=server)

    @staticmethod
    def _parse_authenticate_response(
        payload: Any,
        *,
        fallback_server: str,
    ) -> Dict[str, Any]:
        """Extract session + server from an Authenticate response body."""

        if not isinstance(payload, Mapping):
            raise GeotabConfigError(
                "authenticate returned non-object response"
            )
        if _is_invalid_user_exception(payload):
            raise GeotabConfigError(
                "authenticate rejected credentials (InvalidUserException)"
            )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            # Some SDK fallbacks return the envelope directly.
            result = payload
        credentials = result.get("credentials")
        if not isinstance(credentials, Mapping):
            # Try to accept a flat shape with session_id at the root.
            session_id = result.get(_ENV_SESSION_ID_KEY) or result.get("sessionId")
            if not session_id:
                raise GeotabConfigError(
                    "authenticate response missing credentials"
                )
            return {
                _ENV_SESSION_ID_KEY: str(session_id),
                _CRED_SERVER_KEY: str(
                    result.get(_CRED_SERVER_KEY) or fallback_server
                ),
                _ENV_SESSION_EXPIRES_AT_KEY: _iso(_utcnow()),
            }
        session_id = credentials.get("sessionId") or credentials.get(
            _ENV_SESSION_ID_KEY
        )
        if not session_id:
            raise GeotabConfigError(
                "authenticate response missing sessionId"
            )
        return {
            _ENV_SESSION_ID_KEY: str(session_id),
            _CRED_SERVER_KEY: str(
                result.get("path") or credentials.get(_CRED_SERVER_KEY) or fallback_server
            ),
            _ENV_SESSION_EXPIRES_AT_KEY: _iso(_utcnow()),
        }

    async def _call_with_reauth(
        self,
        *,
        method: str,
        params: Mapping[str, Any],
    ) -> Any:
        """Issue a Geotab API call with session-renewal on InvalidUserException.

        Flow:
            1. Load the credentials envelope; inject session + server.
            2. Invoke the SDK (or HTTPS fallback).
            3. If the response signals ``InvalidUserException``,
               re-run ``Authenticate`` once and retry.
            4. If the second call still fails, raise
               :class:`GeotabSessionExpired`.
        """

        creds = await self._load_credentials()
        try:
            response = await self._invoke_call(
                method=method,
                params=params,
                credentials=creds,
            )
        except _InvalidSession:
            response = None

        if response is not None and not _is_invalid_user_exception(response):
            return response

        # Re-auth once (Req 5.4.5).
        logger.info(
            "GeotabConnector: session invalid on %s for tenant=%s — "
            "attempting re-authentication",
            method,
            self._tenant_id,
        )
        try:
            refreshed = await self._authenticate(
                username=str(creds.get(_CRED_USERNAME_KEY) or ""),
                password=str(creds.get(_CRED_PASSWORD_KEY) or ""),
                database=str(creds.get(_CRED_DATABASE_KEY) or ""),
                server=str(
                    creds.get(_CRED_SERVER_KEY)
                    or self._config.get(_CONFIG_SERVER_KEY)
                    or DEFAULT_SERVER
                ),
            )
        except Exception as exc:
            logger.warning(
                "GeotabConnector: re-authentication failed tenant=%s: %s",
                self._tenant_id,
                exc,
            )
            raise GeotabSessionExpired(
                self._tenant_id, self._instance_id
            ) from exc

        rotated = dict(creds)
        rotated[_ENV_SESSION_ID_KEY] = refreshed[_ENV_SESSION_ID_KEY]
        rotated[_CRED_SERVER_KEY] = refreshed.get(
            _CRED_SERVER_KEY, rotated.get(_CRED_SERVER_KEY)
        )
        rotated[_ENV_SESSION_EXPIRES_AT_KEY] = refreshed.get(
            _ENV_SESSION_EXPIRES_AT_KEY, _iso(_utcnow())
        )
        await self._persist_rotated_credentials(rotated)

        try:
            response = await self._invoke_call(
                method=method,
                params=params,
                credentials=rotated,
            )
        except _InvalidSession as exc:
            raise GeotabSessionExpired(
                self._tenant_id, self._instance_id
            ) from exc
        if _is_invalid_user_exception(response):
            raise GeotabSessionExpired(self._tenant_id, self._instance_id)
        return response

    async def _invoke_call(
        self,
        *,
        method: str,
        params: Mapping[str, Any],
        credentials: Mapping[str, Any],
    ) -> Any:
        """Execute a Geotab API call using the SDK or HTTPS fallback."""

        server = str(
            credentials.get(_CRED_SERVER_KEY)
            or self._config.get(_CONFIG_SERVER_KEY)
            or DEFAULT_SERVER
        )
        session_id = credentials.get(_ENV_SESSION_ID_KEY)
        database = credentials.get(_CRED_DATABASE_KEY)
        username = credentials.get(_CRED_USERNAME_KEY)

        if self._sdk_call is not None:
            return await self._invoke_sdk(
                method,
                params,
                server=server,
                session_id=session_id,
                database=database,
                username=username,
            )

        # mygeotab-backed path. We re-use the cached session across
        # calls by passing it through ``credentials``.
        try:
            import mygeotab  # type: ignore  # noqa: F401

            def _run_sync() -> Any:
                api = mygeotab.API(  # type: ignore[attr-defined]
                    username=str(username or ""),
                    session_id=str(session_id or ""),
                    database=str(database or ""),
                    server=server,
                )
                return api.call(method, **dict(params))

            try:
                return await asyncio.to_thread(_run_sync)
            except Exception as exc:  # pragma: no cover - SDK branch
                if INVALID_USER_EXCEPTION in str(exc):
                    raise _InvalidSession() from exc
                raise
        except ImportError:
            pass

        # HTTPS fallback.
        envelope = {
            "method": method,
            "params": {
                **dict(params),
                "credentials": {
                    _ENV_SESSION_ID_KEY: session_id,
                    _CRED_DATABASE_KEY: database,
                    _CRED_USERNAME_KEY: username,
                },
            },
        }
        body = await self._post_apiv1(server=server, payload=envelope)
        return body

    async def _invoke_sdk(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        server: Optional[str] = None,
        session_id: Optional[str] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Any:
        """Invoke the injected ``sdk_call`` under :func:`asyncio.to_thread`.

        ``sdk_call`` is used by tests to short-circuit both the real
        SDK and the HTTPS fallback. It may be either sync or async; we
        detect coroutines and await them directly.
        """

        if self._sdk_call is None:  # pragma: no cover - defensive
            raise GeotabConfigError("sdk_call not configured")

        def _invoke() -> Any:
            return self._sdk_call(
                method=method,
                params=dict(params),
                server=server,
                session_id=session_id,
                database=database,
                username=username,
            )

        try:
            result = _invoke()
        except Exception as exc:
            if INVALID_USER_EXCEPTION in str(exc):
                raise _InvalidSession() from exc
            raise

        if asyncio.iscoroutine(result):
            try:
                return await result
            except Exception as exc:
                if INVALID_USER_EXCEPTION in str(exc):
                    raise _InvalidSession() from exc
                raise
        return result

    async def _post_apiv1(
        self,
        *,
        server: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """HTTPS fallback when the mygeotab SDK is not installed."""

        url = self._build_apiv1_url(server)
        client, owned = await self._get_http_client()
        try:
            try:
                response = await asyncio.wait_for(
                    client.post(
                        url,
                        json=dict(payload),
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        timeout=self._http_timeout,
                    ),
                    timeout=self._http_timeout + 1.0,
                )
            except asyncio.TimeoutError:
                raise
            if response.status_code == 403:
                # Geotab wraps invalid session in a 403 with an
                # InvalidUserException body; surface that as a
                # recoverable session error.
                try:
                    body = response.json() or {}
                except (ValueError, json.JSONDecodeError):
                    body = {}
                if _is_invalid_user_exception(body):
                    raise _InvalidSession()
                raise httpx.HTTPStatusError(
                    "HTTP 403",
                    request=getattr(response, "request", None)
                    or httpx.Request("POST", url),
                    response=response,
                )
            response.raise_for_status()
            try:
                return response.json() or {}
            except (ValueError, json.JSONDecodeError):
                return {}
        finally:
            if owned:
                await client.aclose()

    @staticmethod
    def _build_apiv1_url(server: str) -> str:
        trimmed = server.strip().rstrip("/")
        if trimmed.startswith("http://") or trimmed.startswith("https://"):
            return f"{trimmed}/apiv1"
        return f"https://{trimmed}/apiv1"

    async def _get_http_client(self) -> Tuple[httpx.AsyncClient, bool]:
        """Return ``(client, owned_here)``.

        When the connector was constructed with an injected
        ``http_client`` we reuse it and leave ``aclose`` to the caller;
        otherwise we lazily create one instance-owned client.
        """

        if self._http_client is not None:
            return self._http_client, False
        self._http_client = httpx.AsyncClient(timeout=self._http_timeout)
        return self._http_client, False

    async def _close_owned_http_client(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def aclose(self) -> None:
        await self._close_owned_http_client()

    @staticmethod
    def _extract_result_list(payload: Any) -> List[Any]:
        """Normalize a ``Get`` response into a list of result objects.

        The SDK returns a bare list; the HTTPS envelope wraps it under
        ``result``. We accept both.
        """

        if isinstance(payload, list):
            return list(payload)
        if isinstance(payload, Mapping):
            result = payload.get("result")
            if isinstance(result, list):
                return list(result)
        return []

    # ------------------------------------------------------------------
    # Reading processing
    # ------------------------------------------------------------------

    async def _process_reading(
        self,
        reading: Mapping[str, Any],
        *,
        device_map: Mapping[str, Any],
        now: datetime,
        counts: Dict[str, int],
    ) -> None:
        """Persist a single reading and apply freshness-gated side effects."""

        device_id = reading["device_id"]
        truck_id_raw = device_map.get(device_id) or device_map.get(str(device_id))
        truck_id: Optional[str] = None
        if isinstance(truck_id_raw, str) and truck_id_raw.strip():
            truck_id = truck_id_raw.strip()
        elif isinstance(truck_id_raw, Mapping):
            # Accept richer mappings as well so the config can evolve
            # without breaking the connector.
            maybe_id = truck_id_raw.get("truck_id") or truck_id_raw.get("id")
            if isinstance(maybe_id, str) and maybe_id.strip():
                truck_id = maybe_id.strip()
        if truck_id is None:
            counts["skipped_unmapped"] += 1

        recorded_at = reading.get("recorded_at") or _utcnow()
        if not isinstance(recorded_at, datetime):
            recorded_at = _utcnow()
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)

        persisted = await self._persist_telemetry(
            reading=reading,
            truck_id=truck_id,
            recorded_at=recorded_at,
        )
        if persisted:
            counts["readings_persisted"] += 1

        # IFTA boundary detection hook (Task 12.3 / Req 7.1).
        # Process GPS readings through the StateBoundaryDetector when
        # the IFTA reporter is configured and the reading has a mapped
        # truck_id. Non-fatal — errors are logged but never abort the
        # sync_pull run.
        if truck_id is not None and self._ifta_reporter is not None:
            try:
                await self._process_ifta_boundary_check(
                    reading=reading,
                    truck_id=truck_id,
                    recorded_at=recorded_at,
                )
            except Exception as exc:
                logger.warning(
                    "GeotabConnector: IFTA boundary check failed "
                    "truck=%s tenant=%s: %s",
                    truck_id,
                    self._tenant_id,
                    exc,
                )

        # Freshness-gated truck update.
        if truck_id is None:
            return
        age_seconds = (now - recorded_at).total_seconds()
        if age_seconds > self._freshness_seconds:
            counts["skipped_stale"] += 1
            return
        updated = await self._apply_truck_location(
            truck_id=truck_id,
            reading=reading,
            recorded_at=recorded_at,
        )
        if updated:
            counts["trucks_updated"] += 1

    async def _persist_telemetry(
        self,
        *,
        reading: Mapping[str, Any],
        truck_id: Optional[str],
        recorded_at: datetime,
    ) -> bool:
        """Write a single reading to the ``truck_telemetry`` index."""

        if self._es is None:
            logger.debug(
                "GeotabConnector: no es_service configured; skipping "
                "truck_telemetry persistence for tenant=%s",
                self._tenant_id,
            )
            return False

        device_id = reading.get("device_id")
        telemetry_id = (
            f"telem_{self._instance_id}_{device_id}_{int(recorded_at.timestamp())}"
        )
        lat = _safe_float(reading.get("latitude"))
        lon = _safe_float(reading.get("longitude"))
        doc: Dict[str, Any] = {
            "telemetry_id": telemetry_id,
            "tenant_id": self._tenant_id,
            "truck_id": truck_id,
            "driver_id": reading.get("driver_id"),
            "location": (
                {"lat": lat, "lon": lon}
                if lat is not None and lon is not None
                else None
            ),
            "location_lat": lat,
            "location_lon": lon,
            "speed_kph": _safe_float(reading.get("speed_kph")),
            "engine_on": (
                bool(reading.get("engine_on"))
                if reading.get("engine_on") is not None
                else None
            ),
            "odometer_km": _safe_float(reading.get("odometer_km")),
            "hos_status": reading.get("hos_status"),
            "recorded_at": _iso(recorded_at),
            "retrieved_at": _iso(_utcnow()),
            "created_at": _iso(_utcnow()),
            "updated_at": _iso(_utcnow()),
        }
        try:
            await self._es.index_document(
                self._truck_telemetry_index, telemetry_id, doc
            )
            return True
        except Exception as exc:
            logger.warning(
                "GeotabConnector: truck_telemetry index write failed "
                "tenant=%s device=%s: %s",
                self._tenant_id,
                device_id,
                exc,
            )
            return False

    async def _apply_truck_location(
        self,
        *,
        truck_id: str,
        reading: Mapping[str, Any],
        recorded_at: datetime,
    ) -> bool:
        """Update ``trucks.current_location`` + ``current_location_at``."""

        if self._es is None:
            return False
        lat = _safe_float(reading.get("latitude"))
        lon = _safe_float(reading.get("longitude"))
        if lat is None or lon is None:
            return False
        partial = {
            "current_location": {
                "coordinates": {"lat": lat, "lon": lon},
            },
            "current_location_at": _iso(recorded_at),
            "updated_at": _iso(_utcnow()),
        }
        try:
            await self._es.update_document(
                self._trucks_index, truck_id, partial
            )
            return True
        except Exception as exc:
            logger.warning(
                "GeotabConnector: trucks update failed tenant=%s "
                "truck=%s: %s",
                self._tenant_id,
                truck_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    async def _load_credentials(self) -> Dict[str, Any]:
        """Return the cached credentials envelope; hit the vault on miss."""

        if self._cached_credentials is not None:
            return self._cached_credentials
        if not self._credentials_ref:
            raise GeotabConfigError(
                "GeotabConnector: no credentials_ref — call connect() first"
            )
        envelope = await self._vault.get(
            self._tenant_id, self._credentials_ref
        )
        if not isinstance(envelope, Mapping):
            raise GeotabConfigError(
                "GeotabConnector: vault returned non-mapping envelope"
            )
        self._cached_credentials = dict(envelope)
        return self._cached_credentials

    async def _persist_rotated_credentials(
        self, envelope: Mapping[str, Any]
    ) -> None:
        """Persist the rotated envelope back into the vault.

        Best-effort: a vault failure is logged but not raised — the
        rotated session is still usable from the in-memory cache for
        the rest of the current tick.
        """

        self._cached_credentials = dict(envelope)
        try:
            new_ref = await self._vault.put(
                tenant_id=self._tenant_id,
                key=VAULT_CREDENTIAL_KEY,
                plaintext=dict(envelope),
                provider_name=self.provider_name,
            )
        except Exception as exc:
            logger.warning(
                "GeotabConnector: failed to persist rotated "
                "credentials to vault for tenant=%s: %s",
                self._tenant_id,
                exc,
            )
            return
        if not new_ref:
            return
        old_ref = self._credentials_ref
        self._credentials_ref = new_ref
        if old_ref and old_ref != new_ref:
            try:
                await self._vault.delete(self._tenant_id, old_ref)
            except Exception:  # pragma: no cover - defensive
                pass

    # ------------------------------------------------------------------
    # SyncRun helpers
    # ------------------------------------------------------------------

    def _error_run(
        self,
        *,
        run_id: str,
        operation: str,
        started_at: datetime,
        record_counts: Dict[str, int],
        reason: Optional[str],
        exc: BaseException,
    ) -> SyncRun:
        """Build an error :class:`SyncRun` with structured error details."""

        finished_at = _utcnow()
        message = str(exc) or exc.__class__.__name__
        error_details = f"{reason}: {message}" if reason else message
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation=operation,  # type: ignore[arg-type]
            started_at=started_at,
            finished_at=finished_at,
            status="error",
            record_counts=record_counts,
            error_details=error_details[:1000],
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )


# ---------------------------------------------------------------------------
# Internal sentinel
# ---------------------------------------------------------------------------


class _InvalidSession(Exception):
    """Internal marker raised when an API call needs a fresh session.

    Never crosses the module boundary — :meth:`_call_with_reauth` traps
    it and either retries with a fresh session or raises
    :class:`GeotabSessionExpired`.
    """


__all__ = [
    "DEFAULT_FRESHNESS_SECONDS",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_SCHEDULE_CRON",
    "DEFAULT_SERVER",
    "GeotabConfigError",
    "GeotabConnector",
    "GeotabSessionExpired",
    "INVALID_USER_EXCEPTION",
    "SESSION_EXPIRED_REASON",
    "TRUCKS_INDEX",
    "VAULT_CREDENTIAL_KEY",
    "build_catalog_entry",
    "register_catalog_entry",
]
