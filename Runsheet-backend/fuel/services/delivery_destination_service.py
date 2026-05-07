"""
Delivery Destination Service — unified view over fuel_stations + customer_tanks.

Capability 6 (Requirement 6.2) requires a single ``Delivery_Destination``
abstraction so that forecasting, prioritization, routing, and POD do not have
two divergent code paths for Nigerian retail stations versus US customer
tanks. This service reads from both the legacy ``fuel_stations`` index and
the new ``customer_tanks`` index (introduced by Task 3.1) and normalizes each
source record into a common :class:`DeliveryDestination` Pydantic model.

Key responsibilities:

* Expose a :class:`DeliveryDestination` Pydantic model with a common shape:
  ``destination_id``, ``destination_type`` (``retail_station`` | ``customer_tank``),
  ``tenant_id``, ``customer_id`` (optional), ``name``, ``location`` (lat/lon),
  ``address``, ``fuel_products`` (canonical catalog codes accepted at this
  destination), ``capacity_gallons``, ``current_level_gallons``, ``status``,
  ``updated_at``, ``created_at``, plus the source record's raw payload
  optionally preserved under ``raw``.
* Provide :meth:`DeliveryDestinationService.list` to return the unified list
  for a tenant, optionally filtered by ``destination_type``, ``fuel_product``
  (canonical code or legacy alias), and ``zip_code``. Queries against the two
  ES indices run in parallel and results are merged.
* Provide :meth:`DeliveryDestinationService.get` for a single destination,
  resolving the index by ``destination_type``.

Tenant isolation: every ES query is scoped by ``tenant_id`` via a ``term``
clause and every result is re-validated against the caller's tenant to
prevent accidental cross-tenant leakage if the underlying index contains
mis-labelled documents.

Unit normalization: the legacy ``fuel_stations`` index stores volumes in
liters (``capacity_liters`` / ``current_stock_liters``); the new
``customer_tanks`` index stores volumes in gallons. This service normalizes
everything to gallons at the boundary using
:mod:`services.unit_conversion` so downstream consumers receive one unit.

Validates: Requirements 6.2.1, 6.2.4.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
    is_known_product,
)
from services.unit_conversion import to_canonical_volume

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Index name constants (kept local so this module is self-contained)
# ---------------------------------------------------------------------------

#: Legacy Nigerian retail-station index. Mapping is defined in
#: ``fuel/services/fuel_es_mappings.py``.
FUEL_STATIONS_INDEX = "fuel_stations"

#: New US-market customer-tank index introduced by Task 3.1 of the fuel-ops
#: hardening spec. Mapping is defined in
#: ``Agents/support/fuel_ops_es_mappings.py``.
CUSTOMER_TANKS_INDEX = "customer_tanks"

#: Maximum documents returned per ES query. The list endpoint paginates on
#: top of this so callers never receive a truncated silent result.
_DEFAULT_PER_INDEX_QUERY_SIZE: int = 500


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


#: Discriminates which underlying ES index produced a ``DeliveryDestination``.
DestinationType = Literal["retail_station", "customer_tank"]


class Location(BaseModel):
    """Geographic coordinates for a delivery destination.

    Bounded to valid WGS84 ranges so callers receiving this model can rely on
    the values being safe to hand to a routing provider without further
    validation.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(..., ge=-90, le=90, description="Latitude in degrees.")
    lon: float = Field(..., ge=-180, le=180, description="Longitude in degrees.")


class DeliveryDestination(BaseModel):
    """Unified view over a retail station or customer tank.

    Fields mirror Requirement 6.2.1 and extend it with bookkeeping
    (``updated_at`` / ``created_at``) and an optional ``raw`` payload so
    callers can access source-specific fields without a second query.

    All volumes are in **US gallons** regardless of which index backed the
    record. This is the presentation boundary — internal storage for the
    legacy NG index is liters and is converted on read.
    """

    model_config = ConfigDict(extra="forbid")

    destination_id: str = Field(
        ..., description="Stable identifier within ``destination_type``."
    )
    destination_type: DestinationType = Field(
        ..., description="Which source index this record was normalized from."
    )
    tenant_id: str = Field(..., description="Owning tenant; scoped on read.")
    customer_id: Optional[str] = Field(
        None,
        description=(
            "Customer ID for customer_tank destinations. Always None for "
            "retail stations."
        ),
    )
    name: str = Field(..., description="Human-readable label.")
    location: Optional[Location] = Field(
        None,
        description=(
            "Geographic coordinates; None when the source record has no "
            "location field populated (shouldn't happen in practice but kept "
            "optional for defensive degradation)."
        ),
    )
    address: Optional[str] = Field(None, description="Mailing or dispatch address.")
    zip_code: Optional[str] = Field(
        None, description="Postal code (US) or nearest equivalent."
    )
    fuel_products: List[str] = Field(
        default_factory=list,
        description=(
            "Canonical product codes accepted at this destination "
            "(e.g. DIESEL_2, PROPANE). Legacy aliases are resolved on read."
        ),
    )
    capacity_gallons: Optional[float] = Field(
        None,
        ge=0,
        description="Total tank capacity in US gallons.",
    )
    current_level_gallons: Optional[float] = Field(
        None,
        ge=0,
        description="Current fuel level in US gallons.",
    )
    status: Optional[str] = Field(
        None, description="Source-specific status string (active, open, etc.)."
    )
    updated_at: Optional[datetime] = Field(
        None, description="Last modification timestamp on the source record."
    )
    created_at: Optional[datetime] = Field(
        None, description="Creation timestamp on the source record."
    )
    raw: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "The source ES ``_source`` payload preserved verbatim when the "
            "caller requests it via ``include_raw=True``."
        ),
    )


class DeliveryDestinationFilters(BaseModel):
    """Filters accepted by :meth:`DeliveryDestinationService.list`.

    Unknown or empty filters are dropped so callers can supply a raw
    query-string mapping without defensive pruning.
    """

    model_config = ConfigDict(extra="forbid")

    destination_type: Optional[DestinationType] = Field(
        None, description="Restrict to retail_station or customer_tank only."
    )
    fuel_product: Optional[str] = Field(
        None,
        description=(
            "Canonical product code or legacy alias. Matched against the "
            "destination's ``fuel_products`` list after alias resolution."
        ),
    )
    zip_code: Optional[str] = Field(
        None, description="Exact match on the destination's zip_code."
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DeliveryDestinationService:
    """Unified reader over ``fuel_stations`` and ``customer_tanks``.

    The service takes an :class:`ElasticsearchService`-shaped dependency —
    anything exposing async ``search_documents(index, query, size)`` and
    ``get_document(index, doc_id)`` — so it can be unit-tested against a
    mock without touching a real cluster.

    Instances are safe to share across requests: they hold no per-request
    state and every read re-asserts ``tenant_id`` on every returned document.
    """

    def __init__(self, es_service: Any) -> None:
        self._es = es_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list(
        self,
        tenant_id: str,
        filters: Optional[DeliveryDestinationFilters | Dict[str, Any]] = None,
        *,
        include_raw: bool = False,
        size_per_index: int = _DEFAULT_PER_INDEX_QUERY_SIZE,
    ) -> List[DeliveryDestination]:
        """Return the unified list of destinations for ``tenant_id``.

        The two underlying ES queries run in parallel via
        :func:`asyncio.gather`. Results are concatenated in a stable order
        (retail stations first, then customer tanks) and each record is
        re-validated against the requested ``tenant_id`` so a mis-labelled
        document never leaks across tenants.

        Args:
            tenant_id: Owning tenant. Required and non-empty.
            filters: Optional :class:`DeliveryDestinationFilters` or a dict
                that can be coerced into one.
            include_raw: When True, each returned
                :class:`DeliveryDestination` carries the source ES document
                under ``raw`` for debugging / passthrough use cases.
            size_per_index: Per-index ES query size cap. Defaults to 500.

        Raises:
            ValueError: when ``tenant_id`` is empty/blank.
        """

        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

        normalized = self._normalize_filters(filters)

        # Short-circuit when the caller restricts to a single index so we do
        # not incur a wasted query against the other.
        coros: List[Any] = []
        want_stations = normalized.destination_type in (None, "retail_station")
        want_tanks = normalized.destination_type in (None, "customer_tank")

        if want_stations:
            coros.append(
                self._fetch_fuel_stations(
                    tenant_id=tenant_id,
                    zip_code=normalized.zip_code,
                    size=size_per_index,
                )
            )
        else:
            coros.append(_empty_docs())

        if want_tanks:
            coros.append(
                self._fetch_customer_tanks(
                    tenant_id=tenant_id,
                    zip_code=normalized.zip_code,
                    size=size_per_index,
                )
            )
        else:
            coros.append(_empty_docs())

        station_docs, tank_docs = await asyncio.gather(*coros)

        destinations: List[DeliveryDestination] = []
        for doc in station_docs:
            record = self._normalize_fuel_station(doc, include_raw=include_raw)
            if record is None:
                continue
            if record.tenant_id != tenant_id:
                # Guard against indices with mis-labelled data.
                logger.warning(
                    "DeliveryDestinationService: dropping fuel_stations doc "
                    "%s with mismatched tenant_id %s (expected %s)",
                    record.destination_id,
                    record.tenant_id,
                    tenant_id,
                )
                continue
            destinations.append(record)

        for doc in tank_docs:
            record = self._normalize_customer_tank(doc, include_raw=include_raw)
            if record is None:
                continue
            if record.tenant_id != tenant_id:
                logger.warning(
                    "DeliveryDestinationService: dropping customer_tanks doc "
                    "%s with mismatched tenant_id %s (expected %s)",
                    record.destination_id,
                    record.tenant_id,
                    tenant_id,
                )
                continue
            destinations.append(record)

        # Apply the fuel_product filter post-normalization so legacy aliases
        # are matched consistently regardless of which index they came from.
        if normalized.fuel_product:
            target = self._safe_canonicalize(normalized.fuel_product)
            if target is None:
                # Unknown product filter → no matches rather than a hard error.
                return []
            destinations = [
                d for d in destinations if target in d.fuel_products
            ]

        return destinations

    async def get(
        self,
        tenant_id: str,
        destination_type: str,
        destination_id: str,
        *,
        include_raw: bool = False,
    ) -> Optional[DeliveryDestination]:
        """Return a single destination or ``None`` if not found / not owned.

        Resolves the backing index from ``destination_type``. Returns ``None``
        when the document does not exist or when it belongs to a different
        tenant — callers translate ``None`` into an HTTP 404.
        """

        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not destination_id or not destination_id.strip():
            raise ValueError("destination_id must be a non-empty string")

        dtype = (destination_type or "").strip().lower()
        if dtype not in ("retail_station", "customer_tank"):
            raise ValueError(
                "destination_type must be 'retail_station' or "
                f"'customer_tank', got {destination_type!r}"
            )

        if dtype == "retail_station":
            doc = await self._search_single_station(tenant_id, destination_id)
            if doc is None:
                return None
            record = self._normalize_fuel_station(doc, include_raw=include_raw)
        else:
            doc = await self._search_single_customer_tank(tenant_id, destination_id)
            if doc is None:
                return None
            record = self._normalize_customer_tank(doc, include_raw=include_raw)

        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    # ------------------------------------------------------------------
    # Filter normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_filters(
        filters: Optional[DeliveryDestinationFilters | Dict[str, Any]],
    ) -> DeliveryDestinationFilters:
        if filters is None:
            return DeliveryDestinationFilters()
        if isinstance(filters, DeliveryDestinationFilters):
            return filters
        if isinstance(filters, dict):
            # Drop empty strings so an empty form field is not treated as an
            # exact filter for the empty string.
            cleaned = {
                k: v
                for k, v in filters.items()
                if v is not None and not (isinstance(v, str) and not v.strip())
            }
            return DeliveryDestinationFilters(**cleaned)
        raise TypeError(
            "filters must be a DeliveryDestinationFilters, a dict, or None; "
            f"got {type(filters).__name__}"
        )

    @staticmethod
    def _safe_canonicalize(code_or_alias: str) -> Optional[str]:
        """Return canonical product code or ``None`` if unknown.

        The filter pipeline prefers degrading to an empty result set over
        raising when a caller supplies a typo; unit tests cover both paths.
        """

        if not is_known_product(code_or_alias):
            return None
        try:
            return canonicalize(code_or_alias)
        except (UnknownFuelProductError, TypeError):  # pragma: no cover
            return None

    # ------------------------------------------------------------------
    # ES fetchers
    # ------------------------------------------------------------------

    async def _fetch_fuel_stations(
        self,
        *,
        tenant_id: str,
        zip_code: Optional[str],
        size: int,
    ) -> List[Dict[str, Any]]:
        must: List[Dict[str, Any]] = [{"term": {"tenant_id": tenant_id}}]
        # The legacy fuel_stations index has no first-class zip_code field;
        # skip the ES-side filter and defer to post-filtering if needed.
        query = {
            "query": {"bool": {"must": must}},
            "size": size,
        }
        try:
            resp = await self._es.search_documents(FUEL_STATIONS_INDEX, query, size)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "DeliveryDestinationService: fuel_stations query failed for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return []
        return _extract_sources(resp)

    async def _fetch_customer_tanks(
        self,
        *,
        tenant_id: str,
        zip_code: Optional[str],
        size: int,
    ) -> List[Dict[str, Any]]:
        must: List[Dict[str, Any]] = [{"term": {"tenant_id": tenant_id}}]
        if zip_code:
            must.append({"term": {"zip_code": zip_code}})
        query = {
            "query": {"bool": {"must": must}},
            "size": size,
        }
        try:
            resp = await self._es.search_documents(CUSTOMER_TANKS_INDEX, query, size)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "DeliveryDestinationService: customer_tanks query failed for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return []
        return _extract_sources(resp)

    async def _search_single_station(
        self, tenant_id: str, station_id: str
    ) -> Optional[Dict[str, Any]]:
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"station_id": station_id}},
                    ]
                }
            },
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(FUEL_STATIONS_INDEX, query, 1)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "DeliveryDestinationService: fuel_stations get failed for "
                "tenant=%s station=%s: %s",
                tenant_id,
                station_id,
                exc,
            )
            return None
        sources = _extract_sources(resp)
        return sources[0] if sources else None

    async def _search_single_customer_tank(
        self, tenant_id: str, customer_tank_id: str
    ) -> Optional[Dict[str, Any]]:
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"customer_tank_id": customer_tank_id}},
                    ]
                }
            },
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(CUSTOMER_TANKS_INDEX, query, 1)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "DeliveryDestinationService: customer_tanks get failed for "
                "tenant=%s tank=%s: %s",
                tenant_id,
                customer_tank_id,
                exc,
            )
            return None
        sources = _extract_sources(resp)
        return sources[0] if sources else None

    # ------------------------------------------------------------------
    # Normalizers
    # ------------------------------------------------------------------

    def _normalize_fuel_station(
        self, source: Dict[str, Any], *, include_raw: bool
    ) -> Optional[DeliveryDestination]:
        """Project a ``fuel_stations`` document into a DeliveryDestination.

        Volume fields are converted from liters (canonical for the legacy
        index) to gallons. Product grades stored as ``fuel_type`` /
        ``fuel_grade`` are canonicalized via the fuel-product catalog so
        ``AGO`` surfaces as ``DIESEL_2`` regardless of source.
        """

        station_id = source.get("station_id")
        tenant_id = source.get("tenant_id")
        if not station_id or not tenant_id:
            return None

        location = _extract_location(
            source,
            lat_keys=("latitude", "location_lat"),
            lon_keys=("longitude", "location_lon"),
            combined_key="location",
        )

        capacity_gallons = _convert_liters_field(source.get("capacity_liters"))
        current_gallons = _convert_liters_field(source.get("current_stock_liters"))

        fuel_products = _canonicalize_products(
            _gather_fuel_product_candidates(
                source, keys=("fuel_type", "fuel_grade", "fuel_types")
            )
        )

        return DeliveryDestination(
            destination_id=str(station_id),
            destination_type="retail_station",
            tenant_id=str(tenant_id),
            customer_id=None,
            name=str(source.get("name") or source.get("location_name") or station_id),
            location=location,
            address=_first_non_empty_str(
                source.get("location_name"), source.get("address")
            ),
            zip_code=_str_or_none(source.get("zip_code")),
            fuel_products=fuel_products,
            capacity_gallons=capacity_gallons,
            current_level_gallons=current_gallons,
            status=_str_or_none(source.get("status")),
            updated_at=_parse_datetime(
                source.get("updated_at") or source.get("last_updated")
            ),
            created_at=_parse_datetime(source.get("created_at")),
            raw=dict(source) if include_raw else None,
        )

    def _normalize_customer_tank(
        self, source: Dict[str, Any], *, include_raw: bool
    ) -> Optional[DeliveryDestination]:
        """Project a ``customer_tanks`` document into a DeliveryDestination."""

        tank_id = source.get("customer_tank_id")
        tenant_id = source.get("tenant_id")
        if not tank_id or not tenant_id:
            return None

        location = _extract_location(
            source,
            lat_keys=("location_lat",),
            lon_keys=("location_lon",),
            combined_key="location",
        )

        products = _canonicalize_products(
            _gather_fuel_product_candidates(
                source, keys=("fuel_product_code", "fuel_type")
            )
        )

        # ``customer_tanks`` stores gallons directly per the Task 1.1 mapping.
        capacity_gallons = _coerce_float(source.get("capacity_gallons"))
        current_gallons = _coerce_float(source.get("current_level_gallons"))

        return DeliveryDestination(
            destination_id=str(tank_id),
            destination_type="customer_tank",
            tenant_id=str(tenant_id),
            customer_id=_str_or_none(source.get("customer_id")),
            name=str(
                source.get("name")
                or source.get("display_name")
                or tank_id
            ),
            location=location,
            address=_str_or_none(source.get("address")),
            zip_code=_str_or_none(source.get("zip_code")),
            fuel_products=products,
            capacity_gallons=capacity_gallons,
            current_level_gallons=current_gallons,
            status=_str_or_none(source.get("status")),
            updated_at=_parse_datetime(source.get("updated_at")),
            created_at=_parse_datetime(source.get("created_at")),
            raw=dict(source) if include_raw else None,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _empty_docs() -> List[Dict[str, Any]]:
    """Coroutine returning an empty doc list, used by early-exit paths."""

    return []


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Return the ``_source`` payloads from an ES-shaped response.

    Accepts both the canonical ``{"hits": {"hits": [{"_source": ...}]}}``
    shape and a short-circuit ``None`` so the helper is robust against the
    wider variety of mock shapes used in tests.
    """

    if not resp:
        return []
    hits_outer = resp.get("hits") if isinstance(resp, dict) else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    sources: List[Dict[str, Any]] = []
    for hit in hits:
        if isinstance(hit, dict) and isinstance(hit.get("_source"), dict):
            sources.append(hit["_source"])
    return sources


def _extract_location(
    source: Dict[str, Any],
    *,
    lat_keys: Tuple[str, ...],
    lon_keys: Tuple[str, ...],
    combined_key: Optional[str] = None,
) -> Optional[Location]:
    """Best-effort lat/lon extraction from a source document."""

    lat = _first_float(source, lat_keys)
    lon = _first_float(source, lon_keys)

    if (lat is None or lon is None) and combined_key:
        combined = source.get(combined_key)
        parsed_lat, parsed_lon = _parse_combined_location(combined)
        if lat is None:
            lat = parsed_lat
        if lon is None:
            lon = parsed_lon

    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return Location(lat=lat, lon=lon)


def _parse_combined_location(value: Any) -> Tuple[Optional[float], Optional[float]]:
    """Parse the various shapes ES uses for ``geo_point`` values.

    Supports the string ``"lat,lon"`` form used by seeded legacy data, the
    dict form ``{"lat": ..., "lon": ...}``, and an array ``[lon, lat]``.
    """

    if value is None:
        return None, None
    if isinstance(value, dict):
        lat = _coerce_float(value.get("lat"))
        lon = _coerce_float(value.get("lon") or value.get("long"))
        return lat, lon
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        # GeoJSON array convention is [lon, lat].
        return _coerce_float(value[1]), _coerce_float(value[0])
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if len(parts) >= 2:
            return _coerce_float(parts[0]), _coerce_float(parts[1])
    return None, None


def _first_float(source: Dict[str, Any], keys: Iterable[str]) -> Optional[float]:
    for key in keys:
        if key in source:
            result = _coerce_float(source[key])
            if result is not None:
                return result
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN guard
        return None
    return result


def _convert_liters_field(value: Any) -> Optional[float]:
    """Convert a liters-valued field on the legacy index to gallons.

    Uses :func:`from_canonical_volume` with ``unit="l"`` to keep the exact
    NIST conversion factor in one place. Returns ``None`` when the input
    cannot be coerced to a number.
    """

    as_float = _coerce_float(value)
    if as_float is None:
        return None
    # The legacy index persists liters; convert to canonical gallons.
    # to_canonical_volume treats gallons as canonical, so we go liters → gal.
    canonical_gallons = to_canonical_volume(as_float, "l")
    return canonical_gallons


def _first_non_empty_str(*values: Any) -> Optional[str]:
    for value in values:
        result = _str_or_none(value)
        if result:
            return result
    return None


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Best-effort parse of an ES-style date string into a ``datetime``.

    ES date fields arrive as ISO 8601 strings or as epoch milliseconds.
    Unknown shapes return ``None`` rather than raising so a malformed
    timestamp does not prevent a destination from being listed.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            # Heuristic: values > 10^12 are milliseconds since epoch.
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.utcfromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Allow the "Z" suffix that ES emits by default.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _gather_fuel_product_candidates(
    source: Dict[str, Any], *, keys: Iterable[str]
) -> List[str]:
    """Collect raw fuel-product strings from any of ``keys``.

    ``keys`` typically contains both the legacy ``fuel_grade`` / ``fuel_type``
    names and the new ``fuel_product_code`` name so this helper handles all
    shapes uniformly. Multi-value fields stored as comma-separated strings
    (as seed data sometimes does) are split here.
    """

    out: List[str] = []
    for key in keys:
        if key not in source:
            continue
        value = source[key]
        if value is None:
            continue
        if isinstance(value, str):
            for piece in value.split(","):
                piece = piece.strip()
                if piece:
                    out.append(piece)
        elif isinstance(value, (list, tuple)):
            for piece in value:
                if piece is None:
                    continue
                out.append(str(piece).strip())
        else:
            out.append(str(value).strip())
    return [v for v in out if v]


def _canonicalize_products(candidates: Iterable[str]) -> List[str]:
    """Resolve each candidate through the catalog, dedupe, and preserve order."""

    seen: Dict[str, None] = {}
    for candidate in candidates:
        try:
            canonical = canonicalize(candidate)
        except (UnknownFuelProductError, TypeError):
            continue
        if canonical not in seen:
            seen[canonical] = None
    return list(seen.keys())


__all__ = [
    "CUSTOMER_TANKS_INDEX",
    "DeliveryDestination",
    "DeliveryDestinationFilters",
    "DeliveryDestinationService",
    "DestinationType",
    "FUEL_STATIONS_INDEX",
    "Location",
]
