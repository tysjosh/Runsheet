"""
Truck start-position resolver — telemetry-first with depot fallback.

Task 9.7 / Requirement 5.4.6: the Route_Planning_Agent must use the
truck's live telemetry lat/lon as the route start position when the
latest ``truck_telemetry`` reading is less than 300 seconds old and
belongs to the same tenant; otherwise fall back to the truck's
assigned depot coordinates (resolved through the configured depot
resolution chain).

This module keeps that resolver as a standalone async helper so it
can be reused by the Route_Planning_Agent (current consumer) and by
any future surface (manual replan REST endpoint, simulation harness)
without coupling to the agent lifecycle. The helper is intentionally
minimal: it does not own depot resolution — callers pass a
``depot_resolver`` callable that encapsulates the tenant-specific
``truck.assigned_depot_id → tenant.default_depot_id`` chain.

Tenant isolation is enforced at two points:
    1. The ``truck_telemetry`` query ANDs ``term {"tenant_id": ...}``
       on top of the truck filter.
    2. Every returned source document is re-validated against the
       caller's ``tenant_id`` before its coordinates are trusted, so
       a mis-labelled index document never leaks into another
       tenant's route plan.

Validates: Requirement 5.4.6.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Optional,
    Tuple,
    Union,
)

from fuel.services.fuel_ops_es_mappings import TRUCK_TELEMETRY_INDEX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum age, in seconds, at which a ``truck_telemetry`` reading is
#: considered fresh enough to use as a route start position
#: (Requirement 5.4.4 / 5.4.6). Mirrors the Geotab connector's own
#: ``DEFAULT_FRESHNESS_SECONDS`` so both sides of the freshness
#: contract share one constant per module without cross-imports.
TELEMETRY_FRESHNESS_SECONDS: int = 300

#: Literal tag stamped on :class:`TruckStartPosition.source` when the
#: coordinates came from a fresh ``truck_telemetry`` reading.
SOURCE_TELEMETRY: str = "telemetry"

#: Literal tag stamped on :class:`TruckStartPosition.source` when the
#: coordinates came from the depot-resolution fallback.
SOURCE_DEPOT: str = "depot"

#: Valid bounds for WGS84 latitude (degrees).
_LAT_MIN: float = -90.0
_LAT_MAX: float = 90.0

#: Valid bounds for WGS84 longitude (degrees).
_LON_MIN: float = -180.0
_LON_MAX: float = 180.0


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


StartPositionSource = Literal["telemetry", "depot"]

#: Signature for the caller-provided depot resolver. Given the owning
#: ``tenant_id`` and a ``truck`` mapping (which at minimum carries a
#: ``truck_id`` plus any ``assigned_depot_id`` the caller has already
#: looked up), the resolver returns ``(lat, lon)`` for the assigned
#: depot — or ``None`` when no depot is configured for the tenant.
#:
#: The resolver may be synchronous or asynchronous; the helper awaits
#: the return value when it is a coroutine.
DepotResolverResult = Optional[Tuple[float, float]]
DepotResolver = Callable[
    [str, Mapping[str, Any]],
    Union[DepotResolverResult, Awaitable[DepotResolverResult]],
]


@dataclass(frozen=True)
class TruckStartPosition:
    """Resolved start coordinates plus the provenance annotation.

    ``source`` is ``"telemetry"`` when the coords came from a fresh
    ``truck_telemetry`` document belonging to the caller's tenant, and
    ``"depot"`` when the helper fell back to the caller-supplied depot
    resolver.
    """

    lat: float
    lon: float
    source: StartPositionSource


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NoDepotConfiguredError(RuntimeError):
    """Raised when neither a fresh telemetry reading nor a depot is available.

    The Route_Planning_Agent (and any REST caller that maps this to
    HTTP 400 ``no_depot_configured``) uses this to surface Requirement
    2.2.4 unchanged: a tenant with no configured depot cannot run the
    solver. Callers translate this exception into their transport's
    idiomatic 400 response (e.g. ``fastapi.HTTPException`` in the REST
    layer).
    """

    def __init__(self, tenant_id: str, truck_id: Optional[str] = None) -> None:
        detail = (
            f"no_depot_configured: tenant={tenant_id!r} has no "
            f"fresh truck_telemetry and no depot configured for "
            f"truck={truck_id!r}"
        )
        super().__init__(detail)
        self.tenant_id = tenant_id
        self.truck_id = truck_id
        # Surfaces the stable reason string consumers (e.g. the REST
        # layer) use to build a machine-readable error body without
        # re-parsing the message.
        self.reason_code = "no_depot_configured"


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


async def resolve_truck_start_position(
    *,
    tenant_id: str,
    truck: Mapping[str, Any],
    depot_resolver: DepotResolver,
    es_service: Any,
    now: Optional[datetime] = None,
    freshness_seconds: int = TELEMETRY_FRESHNESS_SECONDS,
    telemetry_index: str = TRUCK_TELEMETRY_INDEX,
) -> TruckStartPosition:
    """Return the truck's route start position with a provenance tag.

    Resolution order (Requirement 5.4.6):

    1. Query ``truck_telemetry`` for the latest document matching
       both ``tenant_id`` and ``truck_id`` sorted by ``recorded_at``
       desc with ``size=1``. When a document exists and
       ``now - recorded_at < freshness_seconds`` we return the
       telemetry coordinates with ``source="telemetry"``.
    2. Otherwise call ``depot_resolver(tenant_id, truck)``; when it
       returns ``(lat, lon)`` we return those coordinates with
       ``source="depot"``.
    3. When the depot resolver returns ``None`` (or raises) we raise
       :class:`NoDepotConfiguredError` so the caller can surface the
       HTTP 400 ``no_depot_configured`` response unchanged.

    Tenant isolation defense-in-depth: the ES query filters by
    ``tenant_id`` AND every returned source is re-checked against the
    argument before its coordinates are trusted, so an index document
    with a drifted ``tenant_id`` never crosses the helper's boundary.

    Args:
        tenant_id: Owning tenant. Required.
        truck: Mapping carrying at least ``truck_id``. May carry
            additional fields the depot resolver consumes (e.g.
            ``assigned_depot_id``) — they are forwarded as-is.
        depot_resolver: Callable that resolves the truck's depot
            coordinates. Signature:
            ``(tenant_id, truck) -> Optional[Tuple[float, float]]``
            (sync or async). Returning ``None`` (or raising) triggers
            the 400 error path.
        es_service: Elasticsearch service exposing
            :meth:`search_documents`.
        now: Optional clock override for deterministic tests. Defaults
            to :func:`datetime.now(timezone.utc)`.
        freshness_seconds: Override the 300-second freshness threshold.
        telemetry_index: Override the target ES index name. Defaults to
            :data:`TRUCK_TELEMETRY_INDEX`.

    Returns:
        :class:`TruckStartPosition` with ``lat``, ``lon``, and
        ``source`` populated.

    Raises:
        ValueError: ``tenant_id`` or ``truck["truck_id"]`` is blank.
        NoDepotConfiguredError: neither fresh telemetry nor depot
            coordinates are available.
    """

    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    if not isinstance(truck, Mapping):
        raise TypeError(
            f"truck must be a mapping, got {type(truck).__name__}"
        )

    truck_id_raw = truck.get("truck_id")
    truck_id = (
        truck_id_raw.strip()
        if isinstance(truck_id_raw, str)
        else ""
    )
    if not truck_id:
        raise ValueError("truck['truck_id'] must be a non-empty string")

    if freshness_seconds <= 0:
        raise ValueError("freshness_seconds must be positive")

    reference_now = now or datetime.now(timezone.utc)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=timezone.utc)

    # --- (1) Try truck_telemetry ---------------------------------------
    telemetry = await _try_fresh_telemetry(
        tenant_id=tenant_id,
        truck_id=truck_id,
        es_service=es_service,
        now=reference_now,
        freshness_seconds=freshness_seconds,
        telemetry_index=telemetry_index,
    )
    if telemetry is not None:
        lat, lon = telemetry
        return TruckStartPosition(lat=lat, lon=lon, source=SOURCE_TELEMETRY)

    # --- (2) Depot fallback --------------------------------------------
    depot_coords = await _invoke_depot_resolver(
        depot_resolver=depot_resolver,
        tenant_id=tenant_id,
        truck=truck,
    )
    if depot_coords is not None:
        lat, lon = depot_coords
        if _coords_within_bounds(lat, lon):
            return TruckStartPosition(lat=lat, lon=lon, source=SOURCE_DEPOT)
        logger.warning(
            "resolve_truck_start_position: depot_resolver returned "
            "out-of-bounds coordinates (lat=%r, lon=%r) for tenant=%s "
            "truck=%s; treating as no_depot_configured",
            lat,
            lon,
            tenant_id,
            truck_id,
        )

    # --- (3) Neither telemetry nor depot available ---------------------
    raise NoDepotConfiguredError(tenant_id=tenant_id, truck_id=truck_id)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _try_fresh_telemetry(
    *,
    tenant_id: str,
    truck_id: str,
    es_service: Any,
    now: datetime,
    freshness_seconds: int,
    telemetry_index: str,
) -> Optional[Tuple[float, float]]:
    """Return (lat, lon) from the latest fresh telemetry row, or None."""

    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"truck_id": truck_id}},
                ],
            },
        },
        "sort": [{"recorded_at": {"order": "desc"}}],
        "size": 1,
    }

    try:
        resp = await es_service.search_documents(telemetry_index, query, 1)
    except Exception as exc:  # pragma: no cover - defensive
        # A transient ES failure must not block route planning. We
        # degrade to the depot fallback so the solver still runs.
        logger.warning(
            "resolve_truck_start_position: truck_telemetry query "
            "failed for tenant=%s truck=%s: %s — falling back to depot",
            tenant_id,
            truck_id,
            exc,
        )
        return None

    source = _first_source(resp)
    if source is None:
        return None

    # Defense-in-depth tenant check: any drifted document with the
    # wrong ``tenant_id`` is ignored entirely.
    doc_tenant = source.get("tenant_id")
    if doc_tenant != tenant_id:
        logger.warning(
            "resolve_truck_start_position: dropping truck_telemetry row "
            "with tenant_id=%r (expected %r) for truck=%s",
            doc_tenant,
            tenant_id,
            truck_id,
        )
        return None

    # Defense-in-depth truck check (the query already filtered, but a
    # mis-labelled document should not silently change the start pos).
    doc_truck = source.get("truck_id")
    if doc_truck != truck_id:
        logger.warning(
            "resolve_truck_start_position: dropping truck_telemetry row "
            "with truck_id=%r (expected %r) for tenant=%s",
            doc_truck,
            truck_id,
            tenant_id,
        )
        return None

    recorded_at = _parse_recorded_at(source.get("recorded_at"))
    if recorded_at is None:
        logger.debug(
            "resolve_truck_start_position: truck_telemetry row missing "
            "recorded_at for tenant=%s truck=%s; treating as stale",
            tenant_id,
            truck_id,
        )
        return None

    age = now - recorded_at
    if age >= timedelta(seconds=freshness_seconds):
        logger.debug(
            "resolve_truck_start_position: stale telemetry age=%.1fs >= %ds "
            "for tenant=%s truck=%s — falling back to depot",
            age.total_seconds(),
            freshness_seconds,
            tenant_id,
            truck_id,
        )
        return None

    lat, lon = _extract_coords(source)
    if lat is None or lon is None:
        return None
    if not _coords_within_bounds(lat, lon):
        logger.warning(
            "resolve_truck_start_position: truck_telemetry row for "
            "tenant=%s truck=%s has out-of-bounds coords (lat=%r, lon=%r); "
            "ignoring",
            tenant_id,
            truck_id,
            lat,
            lon,
        )
        return None

    return lat, lon


async def _invoke_depot_resolver(
    *,
    depot_resolver: DepotResolver,
    tenant_id: str,
    truck: Mapping[str, Any],
) -> Optional[Tuple[float, float]]:
    """Invoke a sync-or-async depot resolver and normalize the result."""

    if depot_resolver is None:
        return None

    try:
        result = depot_resolver(tenant_id, truck)
    except Exception as exc:
        logger.warning(
            "resolve_truck_start_position: depot_resolver raised for "
            "tenant=%s truck=%s: %s",
            tenant_id,
            truck.get("truck_id"),
            exc,
        )
        return None

    # Accept both sync returns and awaitable returns. Checking
    # ``hasattr(..., "__await__")`` covers both coroutines and
    # awaitable objects (e.g. asyncio.Futures) without importing
    # ``asyncio.iscoroutine`` — which only accepts coroutines.
    if hasattr(result, "__await__"):
        try:
            result = await result
        except Exception as exc:
            logger.warning(
                "resolve_truck_start_position: awaited depot_resolver "
                "raised for tenant=%s truck=%s: %s",
                tenant_id,
                truck.get("truck_id"),
                exc,
            )
            return None

    if result is None:
        return None

    try:
        lat, lon = result  # type: ignore[misc]
    except (TypeError, ValueError):
        logger.warning(
            "resolve_truck_start_position: depot_resolver returned "
            "non-(lat, lon) value %r for tenant=%s truck=%s",
            result,
            tenant_id,
            truck.get("truck_id"),
        )
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        logger.warning(
            "resolve_truck_start_position: depot_resolver returned "
            "non-numeric coords (lat=%r, lon=%r) for tenant=%s truck=%s",
            lat,
            lon,
            tenant_id,
            truck.get("truck_id"),
        )
        return None


def _first_source(resp: Any) -> Optional[Mapping[str, Any]]:
    """Return the first ``_source`` dict from an ES search response, or None."""

    if not isinstance(resp, Mapping):
        return None
    hits_outer = resp.get("hits")
    if not isinstance(hits_outer, Mapping):
        return None
    hits = hits_outer.get("hits") or []
    if not hits:
        return None
    first = hits[0]
    if not isinstance(first, Mapping):
        return None
    source = first.get("_source")
    if isinstance(source, Mapping):
        return source
    return None


def _parse_recorded_at(value: Any) -> Optional[datetime]:
    """Parse a Geotab-style ``recorded_at`` timestamp into aware UTC."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
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


def _extract_coords(
    source: Mapping[str, Any],
) -> Tuple[Optional[float], Optional[float]]:
    """Read lat/lon from either ``location_lat``/``location_lon`` or ``location``."""

    lat = _safe_float(source.get("location_lat"))
    lon = _safe_float(source.get("location_lon"))
    if lat is not None and lon is not None:
        return lat, lon

    loc = source.get("location")
    if isinstance(loc, Mapping):
        lat2 = _safe_float(loc.get("lat"))
        lon2 = _safe_float(loc.get("lon"))
        if lat2 is not None and lon2 is not None:
            return lat2, lon2

    return None, None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coords_within_bounds(lat: float, lon: float) -> bool:
    return _LAT_MIN <= lat <= _LAT_MAX and _LON_MIN <= lon <= _LON_MAX


__all__ = [
    "DepotResolver",
    "NoDepotConfiguredError",
    "SOURCE_DEPOT",
    "SOURCE_TELEMETRY",
    "StartPositionSource",
    "TELEMETRY_FRESHNESS_SECONDS",
    "TruckStartPosition",
    "resolve_truck_start_position",
]
