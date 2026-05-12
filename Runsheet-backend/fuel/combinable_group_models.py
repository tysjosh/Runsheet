"""
Combinable_Group domain model, group computation, and tenant-scoped repository.

Capability 3 / Requirement 3.2 of the fuel-ops hardening spec adds a
combinable-deliveries recommender: for each prioritization run, group
nearby stops that can ride the same truck trip. This module is the source
of truth for:

* :class:`CombinableGroupEntry` — the minimal input shape the
  :class:`DeliveryPrioritizationAgent` (Task 5.5) hands in: a pending
  stop at a station or customer tank with a fuel grade, projected
  gallons, and a location.
* :class:`CombinableGroupMember` — the nested-object sub-record that
  mirrors the ``members`` nested mapping on ``mvp_combinable_groups``
  (see :mod:`fuel.services.fuel_ops_es_mappings`).
* :class:`CombinableGroup` — the Pydantic model a run produces per
  connected component.
* :func:`compute_combinable_groups` — the pure, stateless function that
  implements Req 3.2.1 and Req 3.2.2. Two entries are combinable when
  their Haversine distance is ≤ ``radius_miles`` (default 2.0) AND their
  canonical fuel products fit on the same truck. Groups are the
  connected components of the pairwise-combinable graph, constructed via
  Union-Find. Singleton components are dropped so callers only see
  actionable groups.
* :func:`fuel_grades_compatible` — the product-compatibility predicate.
  Two fuel grades are considered compatible for a truck compartment set
  when the default contamination matrix (see
  :mod:`fuel.services.compatibility_matrix`) does not ``blocked`` the
  pair in either direction. That matches the spec's "compatible with
  the same truck compartment set" phrasing (Req 3.2.1) since a
  multi-compartment truck can carry two products iff no hard
  contamination rule forbids the transition in either direction — a
  ``requires_cleaning`` rule still permits the pair to ride the same
  run so long as a cleaning event happens between loads. This matches
  the design's ``fuel_grades_compatible`` placeholder with a concrete,
  auditable rule and is exposed separately so tests and the agent can
  override it without forking :func:`compute_combinable_groups`.
* :class:`CombinableGroupRepository` — async tenant-scoped persistence
  for the ``mvp_combinable_groups`` index.

Tenant isolation is enforced on every write and read path:
    1. Every ES query includes a ``term`` clause on ``tenant_id``.
    2. Every returned document is re-validated against the caller's
       ``tenant_id`` before it crosses the repository boundary.

Note on distance: the reused helper
:func:`driver.services.geo_utils.haversine_distance_meters` returns
meters; :func:`_haversine_miles` converts using
:data:`services.unit_conversion.MI_TO_KM` so there is exactly one
source of truth for the mile→kilometer factor.

Validates: Requirements 3.2.1, 3.2.2, 3.2.3.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from driver.services.geo_utils import haversine_distance_meters
from fuel.services.compatibility_matrix import (
    DEFAULT_COMPATIBILITY_RULES,
    RULE_BLOCKED,
)
from fuel.services.fuel_ops_es_mappings import MVP_COMBINABLE_GROUPS_INDEX
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)
from services.unit_conversion import MI_TO_KM

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


#: The destination entry flavor. Drives which of ``station_id`` /
#: ``customer_tank_id`` is populated in the output ``members`` nested
#: record per Req 3.2.2.
DestinationType = Literal["station", "customer_tank"]

#: Earth-based conversion: 1 mile in meters, derived from MI_TO_KM so the
#: conversion factor is defined in exactly one place (unit_conversion).
_METERS_PER_MILE: float = MI_TO_KM * 1000.0


# ---------------------------------------------------------------------------
# Compatibility predicate (exported)
# ---------------------------------------------------------------------------


def fuel_grades_compatible(
    product_code_a: str,
    product_code_b: str,
) -> bool:
    """Return True if two fuel-grade codes fit on the same truck run.

    Implements the design's ``fuel_grades_compatible`` placeholder with
    a concrete, auditable rule backed by the contamination matrix:

    * Identical canonical codes are always compatible.
    * Distinct codes are compatible iff neither direction of the pair
      is ``blocked`` in :data:`fuel.services.compatibility_matrix.DEFAULT_COMPATIBILITY_RULES`.
      A ``requires_cleaning`` rule still counts as compatible for the
      purposes of combining a truck run — Compartment_Loading_Agent will
      insert a cleaning step before the second load.
    * Unknown / un-canonicalizable product codes short-circuit to
      ``False`` so a corrupt upstream record never accidentally links
      two stops that should not be grouped.

    Legacy Nigerian aliases (AGO/PMS/ATK/LPG) are accepted on input
    because :func:`canonicalize` resolves them before lookup.

    Args:
        product_code_a: Canonical product code or legacy alias for the
            first entry.
        product_code_b: Same for the second entry.

    Returns:
        True if the pair is combinable under the contamination matrix.
    """

    try:
        left = canonicalize(product_code_a)
        right = canonicalize(product_code_b)
    except (UnknownFuelProductError, TypeError):
        return False
    if left == right:
        return True
    forward = DEFAULT_COMPATIBILITY_RULES.get((left, right))
    reverse = DEFAULT_COMPATIBILITY_RULES.get((right, left))
    if forward == RULE_BLOCKED or reverse == RULE_BLOCKED:
        return False
    return True


# ---------------------------------------------------------------------------
# Input entry
# ---------------------------------------------------------------------------


class CombinableGroupEntry(BaseModel):
    """Input record the caller hands to :func:`compute_combinable_groups`.

    One entry corresponds to one pending delivery stop. Either
    ``station_id`` or ``customer_tank_id`` is populated depending on
    ``destination_type`` — the model validates this so callers cannot
    accidentally create a mixed-shape entry the ES nested mapping would
    reject.

    The ``fuel_grade`` field accepts either a canonical US product code
    (``PROPANE``) or a legacy alias (``LPG``) and stores the canonical
    form so downstream group members carry a consistent code.
    """

    model_config = ConfigDict(extra="forbid")

    destination_type: DestinationType = Field(
        ...,
        description="Whether this entry is a retail station or a customer tank.",
    )
    destination_id: str = Field(
        ...,
        min_length=1,
        description="Primary id (``station_id`` or ``customer_tank_id``). Required.",
    )
    station_id: Optional[str] = Field(
        None,
        description="Populated iff ``destination_type == 'station'``.",
    )
    customer_tank_id: Optional[str] = Field(
        None,
        description="Populated iff ``destination_type == 'customer_tank'``.",
    )
    fuel_grade: str = Field(
        ...,
        min_length=1,
        description=(
            "Legacy fuel-grade string or canonical US product code. "
            "Stored as the canonical US product_code (PROPANE, DIESEL_2, "
            "...). Legacy aliases (AGO, PMS, ATK, LPG) are resolved at "
            "construction time."
        ),
    )
    estimated_gallons: float = Field(
        ...,
        ge=0.0,
        description="Projected drop size in US gallons.",
    )
    location_lat: float = Field(..., ge=-90.0, le=90.0)
    location_lon: float = Field(..., ge=-180.0, le=180.0)

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("fuel_grade", mode="before")
    @classmethod
    def _canonicalize_fuel_grade(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        return canonicalize(value)

    @field_validator("destination_id", "station_id", "customer_tank_id")
    @classmethod
    def _strip_ids(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("id field must not be blank")
        return stripped

    @model_validator(mode="after")
    def _check_destination_shape(self) -> "CombinableGroupEntry":
        """Align ``station_id`` / ``customer_tank_id`` with ``destination_type``.

        Cross-shape entries (e.g. ``destination_type='station'`` but a
        ``customer_tank_id`` populated) are rejected outright. Missing
        type-specific ids are filled in from ``destination_id`` so a
        minimal caller only has to supply the type and primary id.
        """

        if self.destination_type == "station":
            if self.customer_tank_id is not None:
                raise ValueError(
                    "station entry must not carry a customer_tank_id"
                )
            if self.station_id is None:
                # Convenient: mirror destination_id.
                object.__setattr__(self, "station_id", self.destination_id)
            elif self.station_id != self.destination_id:
                raise ValueError(
                    "station_id must match destination_id when both are set"
                )
        else:  # customer_tank
            if self.station_id is not None:
                raise ValueError(
                    "customer_tank entry must not carry a station_id"
                )
            if self.customer_tank_id is None:
                object.__setattr__(
                    self, "customer_tank_id", self.destination_id
                )
            elif self.customer_tank_id != self.destination_id:
                raise ValueError(
                    "customer_tank_id must match destination_id when both are set"
                )
        return self


# ---------------------------------------------------------------------------
# Output sub-record + group model
# ---------------------------------------------------------------------------


class CombinableGroupMember(BaseModel):
    """One member inside a :class:`CombinableGroup`.

    Shape mirrors the ``members`` nested mapping on ``mvp_combinable_groups``
    1:1 so ``model_dump()`` payloads can be indexed directly.
    """

    model_config = ConfigDict(extra="forbid")

    destination_type: DestinationType
    destination_id: str
    station_id: Optional[str] = None
    customer_tank_id: Optional[str] = None
    fuel_grade: str
    product_code: str
    estimated_gallons: float = Field(ge=0.0)
    #: geo_point, stored as {"lat": ..., "lon": ...} per ES convention.
    location: Dict[str, float]


class CombinableGroup(BaseModel):
    """A connected component of the pairwise-combinable graph.

    Fields mirror the ``mvp_combinable_groups`` ES mapping (Task 1.1)
    exactly so ``model_dump()`` payloads can be indexed directly.

    ``fuel_grades`` is the set of distinct canonical product codes across
    all members (Req 3.2.2). ``estimated_combined_gallons`` is the sum of
    every member's ``estimated_gallons``.
    """

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(..., min_length=1)
    tenant_id: str = Field(
        default="",
        description=(
            "Owning tenant. The pure :func:`compute_combinable_groups` "
            "helper may emit groups with an empty ``tenant_id``; the "
            ":class:`CombinableGroupRepository` stamps it on persist. "
            "Repositories reject blank tenant ids at their own boundary."
        ),
    )
    run_id: str = Field(default="", description="Prioritization run identifier.")
    members: List[CombinableGroupMember] = Field(..., min_length=2)
    fuel_grades: List[str] = Field(..., min_length=1)
    estimated_combined_gallons: float = Field(ge=0.0)
    centroid: Dict[str, float]
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @property
    def group_centroid_lat(self) -> float:
        """Req 3.2.2 spelling: centroid latitude."""

        return self.centroid["lat"]

    @property
    def group_centroid_lon(self) -> float:
        """Req 3.2.2 spelling: centroid longitude."""

        return self.centroid["lon"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CrossTenantAccessError(PermissionError):
    """Raised when a write/delete targets a group owned by another tenant.

    Subclass of :class:`PermissionError` so middleware that maps
    exceptions to HTTP 403 does the right thing automatically.
    """

    def __init__(
        self,
        tenant_id: str,
        group_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        message = (
            f"combinable_group {group_id!r} does not belong to tenant {tenant_id!r}"
        )
        super().__init__(message)
        self.tenant_id = tenant_id
        self.group_id = group_id
        self.owning_tenant_id = owning_tenant_id


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------


class _UnionFind:
    """Tiny disjoint-set with path compression + union-by-rank.

    Kept module-private because the exposed public surface
    (:func:`compute_combinable_groups`) is what callers actually need.
    """

    __slots__ = ("_parent", "_rank")

    def __init__(self, size: int) -> None:
        if size < 0:
            raise ValueError("size must be non-negative")
        self._parent: List[int] = list(range(size))
        self._rank: List[int] = [0] * size

    def find(self, x: int) -> int:
        # Path compression via iterative walk; avoids Python recursion
        # limits when inputs grow (the solver caps stop counts at 100
        # per Req 2.3.1 but the helper should not assume that).
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Union by rank keeps the forest shallow.
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1


# ---------------------------------------------------------------------------
# Public group-computation function
# ---------------------------------------------------------------------------


def compute_combinable_groups(
    entries: Sequence[CombinableGroupEntry | Dict[str, Any]],
    radius_miles: float = 2.0,
    *,
    tenant_id: Optional[str] = None,
    run_id: str = "",
    compatibility: Callable[[str, str], bool] = fuel_grades_compatible,
) -> List[CombinableGroup]:
    """Return the combinable groups for a prioritization run.

    Implements Req 3.2.1 and Req 3.2.2:

    * Two entries are combinable when their Haversine distance is ≤
      ``radius_miles`` (default 2.0) AND their canonical product codes
      satisfy ``compatibility`` (default: same FuelCategory).
    * Groups are the connected components of that pairwise-combinable
      graph with at least 2 members. Singletons are dropped — a group of
      one is not actionable.
    * Each group carries a ``group_id``, the list of members with
      ``destination_type`` / ``station_id`` / ``customer_tank_id``
      populated per entry type, the full set of distinct canonical
      fuel grades, the summed ``estimated_combined_gallons``, and the
      arithmetic-mean ``centroid``.

    The function is pure: it does not touch ES, Redis, or the clock
    (beyond the default ``generated_at`` timestamp assigned by the
    :class:`CombinableGroup` model — callers that need deterministic
    timestamps can override it post-hoc).

    Args:
        entries: Sequence of :class:`CombinableGroupEntry` or dicts that
            coerce into one. Dicts are validated on the way in so a
            malformed entry surfaces a :class:`ValidationError` rather
            than silently degrading group quality.
        radius_miles: Proximity threshold per Req 3.2.1. Must be
            positive. 2.0 is the spec default; tenants can override at
            the agent layer.
        tenant_id: Required to stamp group records for persistence. When
            omitted the returned groups carry ``""`` and the caller must
            fill it in before indexing; the repository rejects blank
            tenant ids.
        run_id: Prioritization run id. Persisted verbatim so the
            ``/api/fuel/mvp/combinable-groups`` endpoint can filter by
            it (Req 3.2.4).
        compatibility: Callable ``(product_code_a, product_code_b) -> bool``
            that decides whether two fuel grades ride the same truck.
            Exposed so tests and tenant overrides can swap the rule
            without reimplementing the Union-Find loop. Default:
            :func:`fuel_grades_compatible`.

    Returns:
        List of :class:`CombinableGroup`, one per connected component of
        size ≥ 2. Members inside each group retain the input ordering;
        groups themselves are ordered by the first-seen index of their
        lowest-index member (stable, deterministic).

    Raises:
        ValueError: if ``radius_miles`` is non-positive.
    """

    if radius_miles <= 0:
        raise ValueError(
            f"radius_miles must be positive, got {radius_miles}"
        )

    coerced = _coerce_entries(entries)
    if len(coerced) < 2:
        # A run with fewer than two entries cannot produce any group.
        return []

    uf = _UnionFind(len(coerced))
    radius_meters = radius_miles * _METERS_PER_MILE

    # Pairwise O(n²) pass. The solver caps the run at 100 stops so the
    # 100 × 100 / 2 ≈ 5k inner iterations are negligible. If a later
    # capability lifts that cap, swap in a spatial index (e.g., BallTree
    # with haversine metric) behind the same signature.
    for i in range(len(coerced)):
        for j in range(i + 1, len(coerced)):
            a, b = coerced[i], coerced[j]
            if not compatibility(a.fuel_grade, b.fuel_grade):
                continue
            dist_m = haversine_distance_meters(
                a.location_lat, a.location_lon, b.location_lat, b.location_lon
            )
            if dist_m <= radius_meters:
                uf.union(i, j)

    # Bucket entries by their disjoint-set root. Preserve first-seen
    # order so callers comparing output across runs see deterministic
    # group ordering.
    buckets: Dict[int, List[int]] = {}
    root_first_seen: Dict[int, int] = {}
    for idx in range(len(coerced)):
        root = uf.find(idx)
        if root not in buckets:
            buckets[root] = []
            root_first_seen[root] = idx
        buckets[root].append(idx)

    # Ordered roots by first-seen index.
    ordered_roots = sorted(buckets.keys(), key=lambda r: root_first_seen[r])

    groups: List[CombinableGroup] = []
    for root in ordered_roots:
        member_indices = buckets[root]
        if len(member_indices) < 2:
            continue
        members = [coerced[i] for i in member_indices]
        groups.append(
            _build_group(
                members=members,
                tenant_id=tenant_id or "",
                run_id=run_id,
            )
        )
    return groups


def _coerce_entries(
    entries: Sequence[CombinableGroupEntry | Dict[str, Any]],
) -> List[CombinableGroupEntry]:
    """Validate every input entry and return a fresh, typed list."""

    out: List[CombinableGroupEntry] = []
    for idx, raw in enumerate(entries):
        if isinstance(raw, CombinableGroupEntry):
            out.append(raw)
        elif isinstance(raw, dict):
            out.append(CombinableGroupEntry(**raw))
        else:
            raise TypeError(
                "entries[%d] must be a CombinableGroupEntry or dict, got %s"
                % (idx, type(raw).__name__)
            )
    return out


def _build_group(
    members: Sequence[CombinableGroupEntry],
    *,
    tenant_id: str,
    run_id: str,
) -> CombinableGroup:
    """Assemble a :class:`CombinableGroup` from a cluster of entries."""

    nested_members: List[CombinableGroupMember] = []
    fuel_grade_order: List[str] = []
    fuel_grade_seen: set[str] = set()
    total_gallons = 0.0
    lat_sum = 0.0
    lon_sum = 0.0

    for entry in members:
        nested_members.append(
            CombinableGroupMember(
                destination_type=entry.destination_type,
                destination_id=entry.destination_id,
                station_id=entry.station_id,
                customer_tank_id=entry.customer_tank_id,
                # Canonicalization already happened on the entry; we
                # surface both ``fuel_grade`` and ``product_code`` to
                # match the ES nested mapping exactly.
                fuel_grade=entry.fuel_grade,
                product_code=entry.fuel_grade,
                estimated_gallons=entry.estimated_gallons,
                location={"lat": entry.location_lat, "lon": entry.location_lon},
            )
        )
        if entry.fuel_grade not in fuel_grade_seen:
            fuel_grade_seen.add(entry.fuel_grade)
            fuel_grade_order.append(entry.fuel_grade)
        total_gallons += entry.estimated_gallons
        lat_sum += entry.location_lat
        lon_sum += entry.location_lon

    count = len(members)
    centroid = {"lat": lat_sum / count, "lon": lon_sum / count}

    return CombinableGroup(
        group_id=f"group_{uuid4()}",
        tenant_id=tenant_id,
        run_id=run_id,
        members=nested_members,
        fuel_grades=fuel_grade_order,
        estimated_combined_gallons=total_gallons,
        centroid=centroid,
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class CombinableGroupRepository:
    """Tenant-scoped persistence for the ``mvp_combinable_groups`` index.

    Supports:

    * ``persist_groups(tenant_id, groups)`` — batch-index a run's output
      with stamped timestamps, rejecting cross-tenant payloads before
      any ES write.
    * ``list_for_tenant(tenant_id, ...)`` — paginated read with optional
      ``run_id``, ``fuel_grade``, and ``min_members`` filters
      (Req 3.2.4 — the endpoint layer calls through to this).
    * ``get(tenant_id, group_id)`` — single-record fetch, returns
      ``None`` when missing or cross-tenant (no existence leak).

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock mirroring
    :class:`services.elasticsearch_service.ElasticsearchService`.
    """

    DEFAULT_LIST_SIZE: int = 500

    def __init__(
        self,
        es_service: Any,
        *,
        index_name: str = MVP_COMBINABLE_GROUPS_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if not index_name:
            raise ValueError("index_name must not be empty")
        self._es = es_service
        self._index = index_name

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def persist_groups(
        self,
        tenant_id: str,
        groups: Iterable[CombinableGroup],
    ) -> List[CombinableGroup]:
        """Index ``groups`` for ``tenant_id`` and return the stamped models.

        Every group has its ``tenant_id`` verified against the caller's
        ``tenant_id`` (repos never trust the caller to pre-stamp
        correctly) and its ``created_at`` / ``updated_at`` filled in.
        Persistence failures for a single group abort the batch so the
        caller sees a clean error — the alternative of partial-batch
        success would make the ``/api/fuel/mvp/combinable-groups``
        endpoint's pagination semantics confusing.
        """

        self._require_tenant(tenant_id)
        now = _utcnow_iso()
        persisted: List[CombinableGroup] = []

        for group in groups:
            stamped = self._stamp_for_persist(tenant_id, group, now)
            doc = stamped.model_dump(mode="json", exclude_none=False)
            await self._es.index_document(self._index, stamped.group_id, doc)
            persisted.append(stamped)
        return persisted

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, group_id: str
    ) -> Optional[CombinableGroup]:
        """Return the group or ``None`` if missing / cross-tenant."""

        self._require_tenant(tenant_id)
        if not group_id or not group_id.strip():
            raise ValueError("group_id must be a non-empty string")

        source = await self._fetch_source(group_id)
        if source is None:
            return None
        if source.get("tenant_id") != tenant_id:
            logger.info(
                "CombinableGroupRepository.get: suppressing cross-tenant hit "
                "for group=%s (owner=%s, requester=%s)",
                group_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None
        return _safe_model_load(source)

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        run_id: Optional[str] = None,
        fuel_grade: Optional[str] = None,
        min_members: Optional[int] = None,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[CombinableGroup]:
        """List groups for ``tenant_id`` with optional filters.

        Filters map to the REST surface described by Req 3.2.4:

        * ``run_id`` — exact match on the stamped run identifier.
        * ``fuel_grade`` — matches groups whose ``fuel_grades`` array
          contains the canonicalized value. Legacy aliases are accepted.
        * ``min_members`` — applied client-side after loading because
          the ES nested member count is not stored as a scalar field.

        Records whose ``tenant_id`` does not match the caller are
        dropped (with a warning) so mis-labelled documents never cross
        the repository boundary.
        """

        self._require_tenant(tenant_id)
        if size <= 0:
            raise ValueError("size must be a positive integer")
        if min_members is not None and min_members < 0:
            raise ValueError("min_members must be >= 0 when provided")

        must: List[Dict[str, Any]] = [{"term": {"tenant_id": tenant_id}}]
        if run_id:
            must.append({"term": {"run_id": run_id}})
        if fuel_grade:
            try:
                canonical_fuel = canonicalize(fuel_grade)
            except UnknownFuelProductError:
                # Unknown filter → empty result set without hitting ES.
                return []
            must.append({"term": {"fuel_grades": canonical_fuel}})

        query = {
            "query": {"bool": {"must": must}},
            "size": size,
        }
        resp = await self._es.search_documents(self._index, query, size)
        sources = _extract_sources(resp)

        out: List[CombinableGroup] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "CombinableGroupRepository.list_for_tenant: dropping "
                    "mvp_combinable_groups doc with mismatched tenant_id %s "
                    "(expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_model_load(source)
            if model is None:
                continue
            if min_members is not None and len(model.members) < min_members:
                continue
            out.append(model)
        return out

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, tenant_id: str, group_id: str) -> bool:
        """Delete a group. Returns ``True`` if the row was removed.

        Semantics mirror :class:`DepotRepository`:
            * Not-found → ``False``.
            * Cross-tenant → :class:`CrossTenantAccessError`.
            * Owned + deleted → ``True``.
        """

        self._require_tenant(tenant_id)
        if not group_id or not group_id.strip():
            raise ValueError("group_id must be a non-empty string")

        source = await self._fetch_source(group_id)
        if source is None:
            return False
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                group_id=group_id,
                owning_tenant_id=owner,
            )
        return bool(await self._es.delete_document(self._index, group_id))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _stamp_for_persist(
        tenant_id: str,
        group: CombinableGroup,
        now_iso: str,
    ) -> CombinableGroup:
        """Return a copy of ``group`` with ``tenant_id`` / timestamps filled."""

        if group.tenant_id and group.tenant_id != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                group_id=group.group_id,
                owning_tenant_id=group.tenant_id,
            )
        payload = group.model_dump(mode="python")
        payload["tenant_id"] = tenant_id
        if not payload.get("created_at"):
            payload["created_at"] = now_iso
        payload["updated_at"] = now_iso
        return CombinableGroup(**payload)

    async def _fetch_source(self, group_id: str) -> Optional[Dict[str, Any]]:
        query = {
            "query": {"term": {"group_id": group_id}},
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(self._index, query, 1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "CombinableGroupRepository._fetch_source: search failed for "
                "group=%s: %s",
                group_id,
                exc,
            )
            return None
        sources = _extract_sources(resp)
        return sources[0] if sources else None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return a timezone-aware UTC timestamp as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat()


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES-shaped response."""

    if not resp:
        return []
    # Handle both dict and ObjectApiResponse (which has .get() but isn't a dict)
    hits_outer = resp.get("hits") if hasattr(resp, 'get') else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if hasattr(hit, 'get') and hit.get("_source"):
            out.append(hit["_source"])
    return out


def _safe_model_load(source: Dict[str, Any]) -> Optional[CombinableGroup]:
    """Build a :class:`CombinableGroup` from a raw ES source, logging on failure.

    A source that fails Pydantic validation is logged at warning level
    and dropped so a single corrupt record does not kill an entire list
    response.
    """

    try:
        return CombinableGroup(**source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "CombinableGroupRepository: dropping mvp_combinable_groups doc "
            "that failed model validation (group_id=%s): %s",
            source.get("group_id"),
            exc,
        )
        return None


__all__ = [
    "DestinationType",
    "CombinableGroupEntry",
    "CombinableGroupMember",
    "CombinableGroup",
    "CombinableGroupRepository",
    "CrossTenantAccessError",
    "fuel_grades_compatible",
    "compute_combinable_groups",
]
