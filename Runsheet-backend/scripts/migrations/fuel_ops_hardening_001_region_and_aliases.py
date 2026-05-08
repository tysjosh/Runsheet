#!/usr/bin/env python3
"""
Fuel-ops hardening migration 001 — Region + default Depot backfill.

Purpose
-------
Task 12.4 of the ``fuel-ops-hardening`` spec (Requirements 2.2.6, 2.2.7,
6.1.5). When the platform pivoted from Nigerian retail stations to US
fuel-marketer operations the system picked up three new obligations:

(a) Every tenant carries a ``Region`` setting and the platform-default
    for *new* tenants is now ``"US"``. Tenants that existed **before**
    the pivot must remain on their historical region (``"NG"``) so their
    measurement units (liters + kilometers) and routing defaults continue
    to behave the same way until they explicitly flip.

(b) The Route_Planning_Agent no longer falls back to the hardcoded Lagos
    coordinate pair ``{6.5244, 3.3792}``. Instead it resolves a depot via
    ``truck.assigned_depot_id → tenant.default_depot_id → HTTP 400
    no_depot_configured``. Existing tenants therefore need at least one
    :class:`fuel.depot_models.Depot` in the ``depots`` index and their
    ``tenant_settings.default_depot_id`` pointing at it, otherwise the
    next route request is a hard error for them.

(c) Tenants that have never seen a ``fuel_stations`` document have no
    coordinates to seed a default Depot from. Those tenants are flagged
    with a ``depot_required`` Redis key so the admin UI can surface a
    one-off setup task (see ``runsheet/src/components/admin/DepotsPage.tsx``).

What the migration does per tenant
----------------------------------

1. **Region backfill** — if the tenant has no ``region`` persisted in
   its ``TenantSettings`` (i.e. the Redis key is absent or malformed),
   write ``region="NG"`` via
   :meth:`services.tenant_settings.TenantSettingsService.set_region`.
   Tenants that already have an explicit region are left alone.

2. **Default-depot backfill** — query the ``fuel_stations`` index for the
   tenant's stations, compute the **mode** of
   ``(latitude, longitude)`` coordinates rounded to five decimal places
   (≈1 m ties) and create a :class:`fuel.depot_models.Depot` at that
   coordinate via :class:`fuel.depot_models.DepotRepository`. Ties are
   broken deterministically by taking the lexicographically smallest
   coordinate so re-running the migration produces identical depot
   payloads (idempotency requirement).

3. **Tenant default wiring** — persist the new depot's id through
   :meth:`services.tenant_settings.TenantSettingsService.set_default_depot_id`
   so the Route_Planning_Agent's resolution chain resolves to the new
   depot on the very next request.

4. **depot_required flag** — tenants whose ``fuel_stations`` query
   returned zero documents are flagged in Redis under
   ``tenant:{tenant_id}:depot_required`` with a JSON payload of
   ``{"reason": "no_fuel_stations", "flagged_at": <ISO8601>}``. The
   admin UI already surfaces a banner when the depots list is empty
   (see ``runsheet/src/components/admin/DepotsPage.tsx``); this key
   serves as durable audit metadata for operators.

Idempotency
-----------
Every write is guarded by a read. Region is not overwritten if the
tenant already has an explicit region. A new default Depot is only
created if the tenant has no existing depot at (approximately) the
same coordinate — matched by rounding ``location_lat`` / ``location_lon``
to five decimal places before comparison. ``default_depot_id`` is only
written when it differs from the current value. The ``depot_required``
Redis key is set with ``NX`` semantics so repeated runs don't clobber
an operator-supplied reason.

Usage
-----

Dry-run (default in this codebase; logs every intended write without
touching ES / Redis)::

    python -m scripts.migrations.fuel_ops_hardening_001_region_and_aliases --dry-run

Full run against a staging tenant::

    python -m scripts.migrations.fuel_ops_hardening_001_region_and_aliases \\
        --tenant-id acme-staging

Full run over every tenant discovered in the ``fuel_stations`` +
``depots`` indices (this is what Task 12.10 kicks off against the
staging cluster)::

    python -m scripts.migrations.fuel_ops_hardening_001_region_and_aliases

Exit codes
----------
``0`` on success, ``1`` on any tenant-level error (the script continues
past per-tenant failures and surfaces a non-zero exit only at the end
so partial progress is still committed).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone as _dt_timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

# Ensure the project root is on sys.path so ``config.settings`` and the
# fuel/services modules resolve when the script is executed either as
# ``python -m scripts.migrations.fuel_ops_hardening_001_region_and_aliases``
# or as a standalone ``python scripts/migrations/...``.
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fuel_ops_hardening_001")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Index holding legacy retail fuel stations (source of tenant coordinates).
FUEL_STATIONS_INDEX = "fuel_stations"

#: Index holding the (new) tenant-scoped Depot documents written by this
#: migration. Matches :data:`fuel.services.fuel_ops_es_mappings.DEPOTS_INDEX`.
DEPOTS_INDEX = "depots"

#: Redis key pattern for the ``depot_required`` setup task flag.
DEPOT_REQUIRED_KEY_PATTERN = "tenant:{tenant_id}:depot_required"

#: Coordinate rounding precision used to bucket station coordinates into a
#: discrete mode. 5 decimals ≈ 1.1 meters at the equator, which is tighter
#: than any GPS noise we see in fuel_stations seeds.
COORDINATE_ROUND_DECIMALS = 5

#: Default region stamped on tenants that predate the US pivot.
DEFAULT_LEGACY_REGION = "NG"

#: ES page size for scanning ``fuel_stations``. The scan helper pages
#: through via ``search_after`` rather than relying on ``scroll`` so the
#: script runs cleanly against serverless Elasticsearch as well.
FUEL_STATIONS_PAGE_SIZE = 500


# ---------------------------------------------------------------------------
# Result reporting dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TenantMigrationResult:
    """Per-tenant migration outcome for logging + test assertions."""

    tenant_id: str
    region_backfilled: bool = False
    region_already_set: bool = False
    depot_created: bool = False
    depot_id: Optional[str] = None
    depot_coordinate: Optional[Tuple[float, float]] = None
    default_depot_wired: bool = False
    existing_default_depot_preserved: bool = False
    depot_required_flagged: bool = False
    station_count: int = 0
    errors: List[str] = field(default_factory=list)

    def as_log_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "region_backfilled": self.region_backfilled,
            "region_already_set": self.region_already_set,
            "depot_created": self.depot_created,
            "depot_id": self.depot_id,
            "depot_coordinate": self.depot_coordinate,
            "default_depot_wired": self.default_depot_wired,
            "existing_default_depot_preserved": self.existing_default_depot_preserved,
            "depot_required_flagged": self.depot_required_flagged,
            "station_count": self.station_count,
            "errors": list(self.errors),
        }


@dataclass
class MigrationSummary:
    """Aggregate outcome for a full migration run."""

    tenants_processed: int = 0
    regions_backfilled: int = 0
    depots_created: int = 0
    depots_required_flagged: int = 0
    tenants_with_errors: int = 0

    def record(self, result: TenantMigrationResult) -> None:
        self.tenants_processed += 1
        if result.region_backfilled:
            self.regions_backfilled += 1
        if result.depot_created:
            self.depots_created += 1
        if result.depot_required_flagged:
            self.depots_required_flagged += 1
        if result.errors:
            self.tenants_with_errors += 1


# ---------------------------------------------------------------------------
# Coordinate-mode helper (pure, unit-testable)
# ---------------------------------------------------------------------------


def compute_modal_coordinate(
    coordinates: Iterable[Tuple[Optional[float], Optional[float]]],
    *,
    round_decimals: int = COORDINATE_ROUND_DECIMALS,
) -> Optional[Tuple[float, float]]:
    """Return the most frequent ``(lat, lon)`` coordinate, or ``None``.

    The mode is computed over coordinates rounded to ``round_decimals``
    decimal places so GPS noise doesn't prevent coincident stations from
    clustering together. Invalid entries (``None`` components, NaNs, or
    values outside WGS84 bounds) are dropped before counting.

    Ties are broken deterministically by taking the lexicographically
    smallest ``(lat, lon)`` tuple. That guarantees two runs of the
    migration against the same station layout produce an identical
    coordinate — which is what makes the depot creation step idempotent.

    Args:
        coordinates: Iterable of ``(lat, lon)`` tuples. ``None`` or
            out-of-range components are silently ignored.
        round_decimals: Decimal places to round to before counting.
            Defaults to 5 (≈ 1.1 m at the equator).

    Returns:
        The modal ``(lat, lon)`` rounded to ``round_decimals`` places, or
        ``None`` if no valid coordinates were supplied.
    """

    counter: Counter[Tuple[float, float]] = Counter()
    for pair in coordinates:
        if not pair or len(pair) != 2:
            continue
        lat, lon = pair
        if lat is None or lon is None:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            continue
        if math.isnan(lat_f) or math.isnan(lon_f):
            continue
        if math.isinf(lat_f) or math.isinf(lon_f):
            continue
        if not (-90.0 <= lat_f <= 90.0):
            continue
        if not (-180.0 <= lon_f <= 180.0):
            continue
        rounded = (round(lat_f, round_decimals), round(lon_f, round_decimals))
        counter[rounded] += 1

    if not counter:
        return None

    # ``most_common(1)`` gives us *a* top entry but doesn't impose a
    # deterministic tie-break across equally-frequent coordinates. We
    # instead pick the max count first and then the lexicographically
    # smallest coordinate within that bucket so the migration's depot
    # placement is stable across re-runs.
    max_count = max(counter.values())
    candidates = [coord for coord, count in counter.items() if count == max_count]
    candidates.sort()
    return candidates[0]


# ---------------------------------------------------------------------------
# Elasticsearch helpers
# ---------------------------------------------------------------------------


async def _discover_tenant_ids(es_service: Any) -> List[str]:
    """Discover every distinct ``tenant_id`` across relevant indices.

    There is no canonical ``tenants`` index today — tenants are implicit
    in the documents they own. For the purposes of this migration we
    union the tenant_ids present on the two indices that matter:
    ``fuel_stations`` (source of depot coordinates) and ``depots``
    (so we don't skip a tenant that already has depots configured but is
    missing a region).
    """

    tenant_ids: set[str] = set()
    for index_name in (FUEL_STATIONS_INDEX, DEPOTS_INDEX):
        try:
            resp = await es_service.search_documents(
                index_name,
                {
                    "size": 0,
                    "aggs": {
                        "tenant_ids": {
                            "terms": {"field": "tenant_id", "size": 10_000},
                        }
                    },
                },
                0,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "discover_tenant_ids: %s aggregation failed: %s",
                index_name,
                exc,
            )
            continue

        buckets = (
            (resp or {}).get("aggregations", {}).get("tenant_ids", {}).get("buckets", [])
        )
        for bucket in buckets:
            raw = bucket.get("key")
            if isinstance(raw, str) and raw.strip():
                tenant_ids.add(raw.strip())

    return sorted(tenant_ids)


async def _load_station_coordinates(
    es_service: Any, tenant_id: str
) -> List[Tuple[float, float]]:
    """Return every ``(lat, lon)`` pair this tenant's stations report.

    The ``fuel_stations`` mapping stores coordinates in two forms:
    ``latitude``/``longitude`` as floats, and ``location`` as a
    ``geo_point``. We prefer the scalar fields because they were
    historically the source of truth; the ``geo_point`` mirror is a
    secondary projection. Documents that expose neither form are
    skipped silently so a single bad station doesn't skew the mode.
    """

    coordinates: List[Tuple[float, float]] = []

    # Paginate via ``from`` + ``size`` rather than ``scroll`` because
    # serverless ES (and several test doubles) don't implement scroll.
    offset = 0
    while True:
        query = {
            "query": {"term": {"tenant_id": tenant_id}},
            "_source": ["latitude", "longitude", "location"],
            "from": offset,
            "size": FUEL_STATIONS_PAGE_SIZE,
            "sort": [{"station_id": {"order": "asc"}}],
        }
        try:
            resp = await es_service.search_documents(
                FUEL_STATIONS_INDEX, query, FUEL_STATIONS_PAGE_SIZE
            )
        except Exception as exc:
            logger.warning(
                "load_station_coordinates: tenant=%s page=%d failed: %s",
                tenant_id,
                offset // FUEL_STATIONS_PAGE_SIZE,
                exc,
            )
            break

        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        if not hits:
            break

        for hit in hits:
            src = hit.get("_source") or {}
            lat = src.get("latitude")
            lon = src.get("longitude")
            if lat is None or lon is None:
                # Fall back to the ``geo_point`` mirror which may be
                # serialized as ``{"lat": ..., "lon": ...}`` or as the
                # string form ``"lat,lon"``.
                loc = src.get("location")
                if isinstance(loc, dict):
                    lat = loc.get("lat")
                    lon = loc.get("lon")
                elif isinstance(loc, str) and "," in loc:
                    parts = loc.split(",", 1)
                    try:
                        lat = float(parts[0].strip())
                        lon = float(parts[1].strip())
                    except ValueError:
                        lat = lon = None
            if lat is None or lon is None:
                continue
            try:
                coordinates.append((float(lat), float(lon)))
            except (TypeError, ValueError):
                continue

        if len(hits) < FUEL_STATIONS_PAGE_SIZE:
            break
        offset += FUEL_STATIONS_PAGE_SIZE

    return coordinates


async def _existing_default_depot_id(
    es_service: Any, tenant_id: str, lat: float, lon: float
) -> Optional[str]:
    """Return an existing depot_id at ``(lat, lon)`` for the tenant, if any.

    Enables idempotency — a second migration run against a tenant whose
    default depot was already created the first time finds the existing
    record and re-uses its id rather than minting a duplicate.
    """

    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"location_lat": round(lat, COORDINATE_ROUND_DECIMALS)}},
                    {"term": {"location_lon": round(lon, COORDINATE_ROUND_DECIMALS)}},
                ]
            }
        },
        "size": 1,
    }
    try:
        resp = await es_service.search_documents(DEPOTS_INDEX, query, 1)
    except Exception as exc:
        logger.warning(
            "existing_default_depot: tenant=%s lookup failed: %s",
            tenant_id,
            exc,
        )
        return None

    hits = ((resp or {}).get("hits") or {}).get("hits") or []
    if not hits:
        return None
    src = hits[0].get("_source") or {}
    depot_id = src.get("depot_id")
    return depot_id if isinstance(depot_id, str) and depot_id.strip() else None


# ---------------------------------------------------------------------------
# Per-tenant migration
# ---------------------------------------------------------------------------


async def _migrate_tenant(
    *,
    tenant_id: str,
    es_service: Any,
    depot_repository: Any,
    tenant_settings_service: Any,
    redis_client: Any,
    dry_run: bool,
) -> TenantMigrationResult:
    """Run the three-part migration for a single tenant.

    Parameters are passed explicitly rather than threaded through a
    ``self`` object so a test can substitute recording mocks for any
    subset of the dependencies without constructing the whole graph.
    """

    result = TenantMigrationResult(tenant_id=tenant_id)

    # --- Part (a) — Region backfill ---------------------------------
    try:
        current_settings = await tenant_settings_service.get(tenant_id)
    except Exception as exc:  # pragma: no cover - defensive
        current_settings = None
        result.errors.append(f"tenant_settings.get failed: {exc}")

    needs_region_backfill = False
    # ``TenantSettingsService.get`` always returns a TenantSettings
    # instance — even for tenants with no Redis entry — falling open to
    # ``region="US"``. We can't distinguish "never configured" from
    # "explicitly US" without probing the raw Redis key, so do that
    # directly so we only backfill truly-unset tenants.
    raw_settings_key = f"tenant:{tenant_id}:settings"
    try:
        raw = await redis_client.get(raw_settings_key) if redis_client else None
    except Exception as exc:  # pragma: no cover - defensive
        raw = None
        result.errors.append(f"redis.get settings failed: {exc}")
    if raw is None:
        needs_region_backfill = True
    else:
        result.region_already_set = True

    if needs_region_backfill:
        logger.info(
            "tenant=%s region=%s (backfill) dry_run=%s",
            tenant_id,
            DEFAULT_LEGACY_REGION,
            dry_run,
        )
        if not dry_run:
            try:
                await tenant_settings_service.set_region(
                    tenant_id, DEFAULT_LEGACY_REGION
                )
            except Exception as exc:
                result.errors.append(f"set_region failed: {exc}")
            else:
                result.region_backfilled = True
        else:
            # In dry-run mode we still report what *would* happen.
            result.region_backfilled = True

    # --- Part (b) & (c) — Depot creation / depot_required flag -------
    coordinates = await _load_station_coordinates(es_service, tenant_id)
    result.station_count = len(coordinates)

    if result.station_count == 0:
        # Part (c): no coordinates → flag for setup.
        logger.info(
            "tenant=%s depot_required=true (no fuel_stations) dry_run=%s",
            tenant_id,
            dry_run,
        )
        if not dry_run:
            try:
                payload = json.dumps(
                    {
                        "reason": "no_fuel_stations",
                        "flagged_at": _utcnow_iso(),
                        "migration": "fuel_ops_hardening_001",
                    }
                )
                # ``NX`` preserves an operator-supplied reason if they've
                # already flagged the tenant via another path.
                await redis_client.set(
                    DEPOT_REQUIRED_KEY_PATTERN.format(tenant_id=tenant_id),
                    payload,
                    nx=True,
                )
            except Exception as exc:
                result.errors.append(f"redis.set depot_required failed: {exc}")
            else:
                result.depot_required_flagged = True
        else:
            result.depot_required_flagged = True
        return result

    modal = compute_modal_coordinate(coordinates)
    if modal is None:
        # Every coordinate was invalid. Treat the same as "no stations"
        # from the admin UI's perspective — the tenant needs a manual
        # setup task.
        logger.warning(
            "tenant=%s has %d stations but no usable coordinates; flagging depot_required",
            tenant_id,
            result.station_count,
        )
        if not dry_run:
            try:
                payload = json.dumps(
                    {
                        "reason": "no_usable_station_coordinates",
                        "flagged_at": _utcnow_iso(),
                        "migration": "fuel_ops_hardening_001",
                        "station_count": result.station_count,
                    }
                )
                await redis_client.set(
                    DEPOT_REQUIRED_KEY_PATTERN.format(tenant_id=tenant_id),
                    payload,
                    nx=True,
                )
            except Exception as exc:
                result.errors.append(f"redis.set depot_required failed: {exc}")
            else:
                result.depot_required_flagged = True
        else:
            result.depot_required_flagged = True
        return result

    lat, lon = modal
    result.depot_coordinate = (lat, lon)

    # Idempotency — if a depot already sits at this coordinate for the
    # tenant we re-use its id. We intentionally do this even in
    # ``dry_run`` mode so the dry-run output honestly reports "would
    # re-use" vs. "would create".
    existing_depot_id = await _existing_default_depot_id(
        es_service, tenant_id, lat, lon
    )

    if existing_depot_id is None:
        logger.info(
            "tenant=%s depot_create at=(%.5f, %.5f) dry_run=%s",
            tenant_id,
            lat,
            lon,
            dry_run,
        )
        if not dry_run:
            try:
                depot = await depot_repository.create(
                    tenant_id,
                    {
                        "tenant_id": tenant_id,
                        "name": "Default Depot",
                        "address": "Migrated from fuel_stations (fuel_ops_hardening_001)",
                        "timezone": "UTC",
                        "location_lat": lat,
                        "location_lon": lon,
                        "fuel_types_supported": [],
                        "status": "active",
                    },
                )
            except Exception as exc:
                result.errors.append(f"depot_repository.create failed: {exc}")
                return result
            result.depot_created = True
            result.depot_id = depot.depot_id
        else:
            result.depot_created = True
            result.depot_id = f"<dry-run:depot-for-{tenant_id}>"
    else:
        result.depot_id = existing_depot_id
        logger.info(
            "tenant=%s depot_reuse depot_id=%s at=(%.5f, %.5f)",
            tenant_id,
            existing_depot_id,
            lat,
            lon,
        )

    # Wire the depot into tenant_settings.default_depot_id if not
    # already pointing at the same id. We treat an unrelated
    # existing default as "operator configured; do not overwrite"
    # to avoid stepping on manual depot choices.
    current_default: Optional[str] = (
        current_settings.default_depot_id if current_settings is not None else None
    )
    if current_default and result.depot_id and current_default != result.depot_id:
        logger.info(
            "tenant=%s preserving existing default_depot_id=%s (would-be=%s)",
            tenant_id,
            current_default,
            result.depot_id,
        )
        result.existing_default_depot_preserved = True
        return result

    if current_default == result.depot_id:
        # Already wired — nothing to do. Not an error.
        return result

    logger.info(
        "tenant=%s wire default_depot_id=%s dry_run=%s",
        tenant_id,
        result.depot_id,
        dry_run,
    )
    if not dry_run:
        try:
            await tenant_settings_service.set_default_depot_id(
                tenant_id, result.depot_id
            )
        except Exception as exc:
            result.errors.append(f"set_default_depot_id failed: {exc}")
            return result
    result.default_depot_wired = True
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_migration(
    *,
    tenant_id: Optional[str] = None,
    dry_run: bool = False,
    es_service: Optional[Any] = None,
    depot_repository: Optional[Any] = None,
    tenant_settings_service: Optional[Any] = None,
    redis_client: Optional[Any] = None,
) -> MigrationSummary:
    """Run the migration against one or many tenants.

    All four dependencies are injectable so tests and higher-level
    orchestrators can drive the migration with mocks or in-memory
    fakes. When any dependency is ``None`` the function wires the
    production default (Elasticsearch / Redis / DepotRepository /
    TenantSettingsService) lazily.

    Args:
        tenant_id: Restrict the run to this tenant when provided.
        dry_run: When ``True``, log every intended write and report the
            would-be outcome without touching ES or Redis.
        es_service: Optional :class:`services.elasticsearch_service.ElasticsearchService`.
        depot_repository: Optional :class:`fuel.depot_models.DepotRepository`.
        tenant_settings_service: Optional
            :class:`services.tenant_settings.TenantSettingsService`.
        redis_client: Optional async Redis client.

    Returns:
        :class:`MigrationSummary` with aggregate counters across all
        processed tenants.
    """

    # Lazy-import so the module loads cleanly in contexts (tests,
    # ``python -c`` smoke checks) where the ES/Redis/bootstrap graph
    # isn't needed or can't be constructed.
    if es_service is None:
        from services.elasticsearch_service import elasticsearch_service as _es  # noqa: WPS433
        es_service = _es

    if depot_repository is None:
        from fuel.depot_models import DepotRepository  # noqa: WPS433
        depot_repository = DepotRepository(es_service)

    if tenant_settings_service is None or redis_client is None:
        import redis.asyncio as _redis_async  # noqa: WPS433
        from config.settings import get_settings  # noqa: WPS433
        from services.tenant_settings import TenantSettingsService  # noqa: WPS433

        settings = get_settings()
        if redis_client is None:
            if not settings.redis_url:
                raise RuntimeError(
                    "redis_url is not configured; set REDIS_URL or pass "
                    "redis_client explicitly."
                )
            redis_client = _redis_async.from_url(
                settings.redis_url, decode_responses=False
            )
        if tenant_settings_service is None:
            tenant_settings_service = TenantSettingsService(
                redis_client=redis_client
            )

    if tenant_id:
        tenant_ids = [tenant_id]
    else:
        tenant_ids = await _discover_tenant_ids(es_service)

    if not tenant_ids:
        logger.info("No tenants discovered; nothing to migrate.")
        return MigrationSummary()

    logger.info(
        "fuel_ops_hardening_001: starting migration over %d tenant(s) dry_run=%s",
        len(tenant_ids),
        dry_run,
    )

    summary = MigrationSummary()
    for tid in tenant_ids:
        try:
            result = await _migrate_tenant(
                tenant_id=tid,
                es_service=es_service,
                depot_repository=depot_repository,
                tenant_settings_service=tenant_settings_service,
                redis_client=redis_client,
                dry_run=dry_run,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("tenant=%s migration crashed: %s", tid, exc)
            summary.tenants_processed += 1
            summary.tenants_with_errors += 1
            continue

        logger.info("tenant_result=%s", json.dumps(result.as_log_dict(), default=str))
        summary.record(result)

    logger.info(
        "fuel_ops_hardening_001: done tenants=%d regions_backfilled=%d "
        "depots_created=%d depots_required_flagged=%d errors=%d",
        summary.tenants_processed,
        summary.regions_backfilled,
        summary.depots_created,
        summary.depots_required_flagged,
        summary.tenants_with_errors,
    )
    return summary


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(_dt_timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuel_ops_hardening_001_region_and_aliases",
        description=(
            "Backfill Region='NG' on existing tenants, create a default "
            "Depot per tenant from the mode of fuel_stations coordinates, "
            "and flag stationless tenants with depot_required."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Log every intended write without touching Elasticsearch "
            "or Redis. Recommended for the first run in any environment."
        ),
    )
    parser.add_argument(
        "--tenant-id",
        dest="tenant_id",
        default=None,
        help="Restrict the run to this tenant instead of all discovered tenants.",
    )
    return parser


async def main(argv: Optional[List[str]] = None) -> int:
    """Script entrypoint — parses args and kicks off :func:`run_migration`."""

    parser = _build_argparser()
    args = parser.parse_args(argv)

    try:
        summary = await run_migration(
            tenant_id=args.tenant_id,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        logger.exception("fuel_ops_hardening_001 crashed: %s", exc)
        return 1

    return 0 if summary.tenants_with_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
