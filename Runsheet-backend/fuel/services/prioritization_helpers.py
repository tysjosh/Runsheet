"""
Delivery-prioritization helper functions.

Pure, stateless helpers consumed by the ``DeliveryPrioritizationAgent``.
Keeping them in a dedicated module lets unit tests and property-based tests
exercise the math without spinning up the agent, and keeps the agent body
focused on orchestration.

Currently exposed:

* :func:`compute_safe_to_delay` — given a tank forecast and an optional
  SLA buffer (hours), return the ``safe_to_delay_days`` tolerance and the
  ``safe_to_delay_bucket`` label a dispatcher can act on.
* :func:`compute_business_impact` — given a :class:`CustomerProfile`
  (Phase-5 extensions) and the tenant's observed maxima, return a
  normalized business-impact score in [0.0, 1.0] plus a reasons list
  naming which components were missing or dominated the score.
* :func:`compute_priority_clusters` — cluster priority entries by
  geographic proximity using ``sklearn.cluster.DBSCAN`` with the
  ``haversine`` metric. Returns per-entry assignments (with
  ``cluster_id`` / ``cluster_size`` / ``cluster_centroid``) plus the
  cluster-level aggregates the
  ``GET /api/fuel/mvp/priority-clusters`` endpoint serves.

Validates: Requirements 3.1.1, 3.1.2, 3.1.5, 3.3.1, 3.3.2, 3.3.5,
3.4.1, 3.4.2, 3.4.3, 3.4.4.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default SLA buffer applied when the tenant has none configured (Req 3.1.5).
DEFAULT_SLA_BUFFER_HOURS: float = 6.0

#: Literal type for the bucket label.
SafeToDelayBucket = Literal["none", "short", "medium", "long"]

#: Bucket boundaries per Requirement 3.1.2:
#:   - "none"   → days < 1
#:   - "short"  → 1 ≤ days ≤ 3
#:   - "medium" → 4 ≤ days ≤ 7
#:   - "long"   → days > 7
_SHORT_MIN_DAYS = 1
_MEDIUM_MIN_DAYS = 4
_LONG_MIN_DAYS = 8

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

ForecastLike = Union[Mapping[str, Any], Any]


def classify_safe_to_delay_bucket(safe_to_delay_days: int) -> SafeToDelayBucket:
    """Map an integer ``safe_to_delay_days`` to a bucket label.

    Buckets per Requirement 3.1.2:
        * ``none``   — fewer than 1 day of headroom
        * ``short``  — 1 to 3 days
        * ``medium`` — 4 to 7 days
        * ``long``   — more than 7 days
    """
    if safe_to_delay_days < _SHORT_MIN_DAYS:
        return "none"
    if safe_to_delay_days < _MEDIUM_MIN_DAYS:
        return "short"
    if safe_to_delay_days < _LONG_MIN_DAYS:
        return "medium"
    return "long"


def compute_safe_to_delay(
    forecast: ForecastLike,
    sla_buffer_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute the safe-to-delay tolerance for a forecast.

    Implements the formula from Requirement 3.1.1::

        safe_to_delay_days = max(
            0,
            floor((hours_to_runout_p90 − SLA_buffer_hours) / 24)
        )

    and the bucket mapping from Requirement 3.1.2. When the caller omits
    ``sla_buffer_hours`` the default of 6 hours defined by
    Requirement 3.1.5 is used.

    Args:
        forecast: A ``TankForecast`` (or any object/mapping exposing an
            ``hours_to_runout_p90`` attribute or key). The value must be
            a non-negative number; ``math.inf`` is accepted and yields the
            ``"long"`` bucket.
        sla_buffer_hours: Tenant-configured SLA buffer in hours. ``None``
            (the default) falls back to :data:`DEFAULT_SLA_BUFFER_HOURS`.
            Negative values are treated as ``0`` — a negative buffer would
            silently extend the safe-to-delay window past what the forecast
            actually supports, so we clamp it.

    Returns:
        A ``dict`` with two keys:
            * ``safe_to_delay_days`` (``int``): the floored day count,
              never negative.
            * ``safe_to_delay_bucket`` (``str``): one of
              ``none`` / ``short`` / ``medium`` / ``long``.

    Raises:
        TypeError: If ``forecast`` exposes no ``hours_to_runout_p90``
            attribute or key.
        ValueError: If ``hours_to_runout_p90`` is ``NaN`` or negative.
    """
    hours_to_runout_p90 = _read_hours_to_runout_p90(forecast)

    if sla_buffer_hours is None:
        buffer_hours = DEFAULT_SLA_BUFFER_HOURS
    else:
        buffer_hours = float(sla_buffer_hours)
        if buffer_hours < 0:
            # A negative buffer would inflate safe_to_delay_days beyond the
            # raw forecast, so treat as zero and log for observability.
            logger.warning(
                "compute_safe_to_delay received negative sla_buffer_hours=%s; "
                "clamping to 0",
                sla_buffer_hours,
            )
            buffer_hours = 0.0

    # Handle explicit infinity — a tank with no projected runout has
    # unbounded delay tolerance. math.floor(inf) would raise OverflowError.
    if math.isinf(hours_to_runout_p90):
        return {
            "safe_to_delay_days": math.inf,
            "safe_to_delay_bucket": "long",
        }

    raw_hours = hours_to_runout_p90 - buffer_hours
    raw_days = raw_hours / 24.0
    safe_to_delay_days = max(0, math.floor(raw_days))
    bucket = classify_safe_to_delay_bucket(safe_to_delay_days)
    return {
        "safe_to_delay_days": safe_to_delay_days,
        "safe_to_delay_bucket": bucket,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_hours_to_runout_p90(forecast: ForecastLike) -> float:
    """Extract ``hours_to_runout_p90`` from a forecast object or mapping.

    Supports both Pydantic models / dataclasses (attribute access) and
    plain dicts (key access) so callers don't need to normalize first.
    """
    value: Any
    if isinstance(forecast, Mapping):
        if "hours_to_runout_p90" not in forecast:
            raise TypeError(
                "forecast mapping is missing required key 'hours_to_runout_p90'"
            )
        value = forecast["hours_to_runout_p90"]
    else:
        try:
            value = getattr(forecast, "hours_to_runout_p90")
        except AttributeError as exc:
            raise TypeError(
                "forecast object does not expose 'hours_to_runout_p90' "
                "attribute or key"
            ) from exc

    if value is None:
        raise ValueError("hours_to_runout_p90 must not be None")

    try:
        hours = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"hours_to_runout_p90 must be a number, got {value!r}"
        ) from exc

    if math.isnan(hours):
        raise ValueError("hours_to_runout_p90 must not be NaN")
    if hours < 0:
        raise ValueError(
            f"hours_to_runout_p90 must be non-negative, got {hours}"
        )

    return hours


__all__ = [
    "DEFAULT_SLA_BUFFER_HOURS",
    "SafeToDelayBucket",
    "classify_safe_to_delay_bucket",
    "compute_safe_to_delay",
    "BUSINESS_IMPACT_WEIGHTS",
    "SLA_TIER_SCORES",
    "compute_business_impact",
]


# ---------------------------------------------------------------------------
# Business-impact scoring (Requirement 3.3.1, 3.3.2, 3.3.5)
# ---------------------------------------------------------------------------

#: Per-field weights used by :func:`compute_business_impact`. The three
#: monetary fields consume 0.9 of the unit-weight; the SLA-tier component
#: consumes the remaining 0.1. Matches design.md Capability 3:
#:     annual_revenue_usd              → 0.4
#:     contract_penalty_usd_per_day    → 0.3
#:     missed_delivery_cost_usd        → 0.2
#:     sla_tier (after tier-score map) → 0.1
BUSINESS_IMPACT_WEIGHTS: Dict[str, float] = {
    "annual_revenue_usd": 0.4,
    "contract_penalty_usd_per_day": 0.3,
    "missed_delivery_cost_usd": 0.2,
    "sla_tier": 0.1,
}

#: Numeric rank applied to each SLA tier before multiplying by the
#: ``sla_tier`` weight. Higher rank → higher contribution to the score.
SLA_TIER_SCORES: Dict[str, float] = {
    "platinum": 1.0,
    "gold": 0.75,
    "silver": 0.5,
    "bronze": 0.25,
}

#: Fallback tier score applied when the profile's ``sla_tier`` is ``None``
#: or an unrecognized value. Matches the bronze tier so an unconfigured
#: profile is treated as the lowest paid tier, not zero (see design.md).
_DEFAULT_SLA_TIER_SCORE: float = 0.25

#: Monetary fields that participate in the business-impact score; kept in a
#: tuple (not a dict) to preserve deterministic iteration order.
_MONETARY_FIELDS: Tuple[str, ...] = (
    "annual_revenue_usd",
    "contract_penalty_usd_per_day",
    "missed_delivery_cost_usd",
)

ProfileLike = Union[Mapping[str, Any], Any]


def compute_business_impact(
    profile: ProfileLike,
    tenant_max: Mapping[str, float],
) -> Tuple[float, List[str]]:
    """Compute the normalized business-impact score for a customer profile.

    Implements the formula from Requirement 3.3.2 / design.md Capability 3::

        monetary_component = Σ_{f ∈ monetary_fields}
            min(profile[f] / tenant_max[f], 1.0) * weight[f]
        sla_component      = sla_tier_score * 0.1
        score              = monetary_component + sla_component

    The score is bounded in [0.0, 1.0] by construction because each
    monetary term is clamped to 1.0 before weighting, the four weights
    sum to 1.0, and the SLA tier score is bounded in [0.25, 1.0].

    Args:
        profile: A :class:`fuel.storm_mode_models.CustomerProfile`
            instance (or any attribute-accessible object / mapping)
            exposing ``annual_revenue_usd``,
            ``contract_penalty_usd_per_day``, ``missed_delivery_cost_usd``
            and ``sla_tier``. Missing / ``None`` fields are treated as
            zero per Requirement 3.3.5.
        tenant_max: A mapping of ``{field_name: max_value}`` carrying the
            tenant's observed maxima for the three monetary fields. Each
            value must be strictly positive; a zero or missing maximum
            is treated as ``1.0`` so division never blows up, and the
            component's contribution is still clamped at ``weight``.

    Returns:
        A ``(score, reasons)`` tuple where:
            * ``score`` is a ``float`` in ``[0.0, 1.0]``.
            * ``reasons`` is a list of human-readable strings. Each
              ``"missing_profile_field:{field}"`` entry names a profile
              field that was absent or zero (Requirement 3.3.5). Each
              ``"dominant_component:{field}"`` entry names a component
              whose weighted contribution exceeded half the total score —
              useful for UI "why is this score high?" callouts.

    Raises:
        ValueError: If any supplied profile value is ``NaN``, negative,
            or any ``tenant_max`` value is ``NaN`` or negative.
    """
    reasons: List[str] = []
    components: Dict[str, float] = {}

    for field in _MONETARY_FIELDS:
        raw_value = _read_profile_number(profile, field)
        max_value = _read_tenant_max(tenant_max, field)
        if raw_value is None or raw_value == 0.0:
            reasons.append(f"missing_profile_field:{field}")
            components[field] = 0.0
            continue
        # Clamp the ratio at 1.0 so a profile above the tenant max does
        # not blow past its weight cap.
        ratio = min(raw_value / max_value, 1.0)
        components[field] = ratio * BUSINESS_IMPACT_WEIGHTS[field]

    sla_score, sla_missing = _resolve_sla_tier_score(profile)
    if sla_missing:
        reasons.append("missing_profile_field:sla_tier")
    components["sla_tier"] = sla_score * BUSINESS_IMPACT_WEIGHTS["sla_tier"]

    score = sum(components.values())
    # Guard against floating-point drift just above 1.0.
    if score > 1.0:
        score = 1.0

    # Highlight the dominant driver(s) for UI explainability. A component
    # is "dominant" when it contributes more than half the final score.
    if score > 0:
        for field, contribution in components.items():
            if contribution > score / 2.0:
                reasons.append(f"dominant_component:{field}")

    return score, reasons


# ---------------------------------------------------------------------------
# Business-impact internal helpers
# ---------------------------------------------------------------------------


def _read_profile_number(
    profile: ProfileLike, field: str
) -> Optional[float]:
    """Return ``profile[field]`` as a non-negative ``float`` or ``None``.

    Both mapping access and attribute access are supported so Pydantic
    models, dataclasses, and plain dicts all work without adapters.
    """
    if isinstance(profile, Mapping):
        value = profile.get(field)
    else:
        value = getattr(profile, field, None)

    if value is None:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"profile field {field!r} must be a number, got {value!r}"
        ) from exc
    if math.isnan(number):
        raise ValueError(f"profile field {field!r} must not be NaN")
    if number < 0:
        raise ValueError(
            f"profile field {field!r} must be non-negative, got {number}"
        )
    return number


def _read_tenant_max(
    tenant_max: Mapping[str, float], field: str
) -> float:
    """Return a strictly-positive tenant maximum for ``field``.

    Missing, zero, or non-positive maxima fall back to ``1.0`` so the
    ratio computation never divides by zero. A non-numeric / NaN maximum
    raises ``ValueError`` to surface misconfiguration loudly.
    """
    raw = tenant_max.get(field) if tenant_max is not None else None
    if raw is None:
        return 1.0
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"tenant_max[{field!r}] must be a number, got {raw!r}"
        ) from exc
    if math.isnan(number):
        raise ValueError(f"tenant_max[{field!r}] must not be NaN")
    if number < 0:
        raise ValueError(
            f"tenant_max[{field!r}] must be non-negative, got {number}"
        )
    if number == 0.0:
        # Zero means "no observed data yet"; treat as a unit max so the
        # component still contributes proportionally, up to its weight.
        return 1.0
    return number


def _resolve_sla_tier_score(profile: ProfileLike) -> Tuple[float, bool]:
    """Return ``(tier_score, missing)`` for a profile.

    ``missing`` is ``True`` when the profile exposes no ``sla_tier`` value
    or a value outside the recognized set. The fallback tier-score is the
    bronze score (0.25) per design.md so an unconfigured profile still
    contributes a small non-zero amount to the business-impact score.
    """
    if isinstance(profile, Mapping):
        value = profile.get("sla_tier")
    else:
        value = getattr(profile, "sla_tier", None)

    if value is None:
        return _DEFAULT_SLA_TIER_SCORE, True
    if not isinstance(value, str):
        raise ValueError(
            f"profile field 'sla_tier' must be a string, got {value!r}"
        )
    score = SLA_TIER_SCORES.get(value.lower())
    if score is None:
        return _DEFAULT_SLA_TIER_SCORE, True
    return score, False


# ---------------------------------------------------------------------------
# Priority-cluster computation (Requirement 3.4.1, 3.4.2, 3.4.3, 3.4.4)
# ---------------------------------------------------------------------------
#
# The Delivery_Prioritization_Agent groups upcoming deliveries by geography
# so dispatchers can batch-dispatch neighboring customers (Req 3.4). We use
# sklearn's ``DBSCAN`` with the ``haversine`` metric because:
#
#   * DBSCAN finds density-based clusters without a pre-specified ``k`` —
#     exactly what "which upcoming stops are within 3 miles of each
#     other?" needs.
#   * The haversine metric computes great-circle distance in radians so a
#     3-mile radius becomes ``3 / EARTH_RADIUS_MILES``.
#   * Points that fail the ``min_samples`` density constraint are labelled
#     ``-1`` (noise), which per Req 3.4.4 we surface as
#     ``cluster_id="noise"`` and exclude from the cluster-level
#     aggregates.
#
# The helpers here are pure and stateless: they accept any sequence of
# entries (Pydantic models, dataclasses, or plain dicts) carrying ``lat`` /
# ``lon`` (and optional ``priority_bucket`` / ``fuel_grade`` for the
# API-layer aggregation step) and return both per-entry assignments and
# the cluster-level summaries the ``GET /api/fuel/mvp/priority-clusters``
# endpoint serves.

from dataclasses import dataclass, field
from typing import Iterable, Sequence


#: Mean radius of Earth in statute miles. Defined here alongside the
#: clustering helper (rather than in ``services.unit_conversion``) because
#: it is a radius, not a unit-conversion factor, and lifting it here keeps
#: the haversine → radians conversion in one file.
EARTH_RADIUS_MILES: float = 3958.8

#: Cluster-id marker for DBSCAN noise points (Req 3.4.4). The raw sklearn
#: label is ``-1``; the spec mandates ``"noise"`` on the wire so downstream
#: readers don't have to interpret a magic integer.
NOISE_CLUSTER_ID: str = "noise"

#: Ordering used to pick the "highest-priority bucket represented" in a
#: cluster per Req 3.4.3. ``critical`` is most urgent (rank 0), ``low``
#: least (rank 3). Any unrecognized value ranks after every known bucket
#: so a malformed record never shadows a real critical/high row.
_BUCKET_RANK: Dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorityClusterAssignment:
    """The cluster-membership metadata for a single priority entry.

    Returned by :func:`compute_priority_clusters` one-per-input-entry so
    :class:`DeliveryPrioritizationAgent` (Task 5.5) can stamp
    ``cluster_id``, ``cluster_size``, and ``cluster_centroid`` onto the
    priority entry it writes to ``mvp_delivery_priorities`` (Req 3.4.2).

    ``cluster_id`` is either the literal string ``"noise"`` (DBSCAN
    label ``-1``, Req 3.4.4) or ``"cluster_<n>"`` where ``<n>`` is the
    dense cluster's zero-based index in the order DBSCAN emitted them.
    We surface ``cluster_size`` and ``cluster_centroid`` even for noise
    rows so downstream consumers can render a consistent schema:
    ``cluster_size`` is ``1`` and the centroid is the entry's own
    location.
    """

    cluster_id: str
    cluster_size: int
    cluster_centroid: Dict[str, float]  # {"lat": ..., "lon": ...}


@dataclass(frozen=True)
class PriorityCluster:
    """Aggregate view of a single DBSCAN cluster.

    Shape matches the ``GET /api/fuel/mvp/priority-clusters`` response
    row mandated by Req 3.4.3:

    * ``cluster_id`` — same ``"cluster_<n>"`` identifier carried on
      every member's :class:`PriorityClusterAssignment`.
    * ``centroid`` — arithmetic mean of member lat/lon.
    * ``member_count`` — number of priority entries in the cluster.
    * ``highest_priority_bucket`` — the most urgent bucket any member
      carries; ``None`` when no member supplied a recognizable bucket
      (so the API layer can still surface a cluster that came from
      records missing the field).
    * ``fuel_grades`` — sorted, de-duplicated list of canonical fuel
      product codes present in the cluster, empty when none of the
      members carried a ``fuel_grade`` attribute.

    Noise entries never produce a :class:`PriorityCluster`.
    """

    cluster_id: str
    centroid: Dict[str, float]
    member_count: int
    highest_priority_bucket: Optional[str]
    fuel_grades: List[str] = field(default_factory=list)


#: Input-entry shape: any object/dict exposing ``location_lat`` /
#: ``location_lon`` (or ``lat`` / ``lon``). ``priority_bucket`` and
#: ``fuel_grade`` are optional — they only drive the cluster-level
#: aggregates, not the DBSCAN labels themselves.
ClusterEntry = Union[Mapping[str, Any], Any]


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def compute_priority_clusters(
    entries: Sequence[ClusterEntry],
    eps_miles: float = 3.0,
    min_samples: int = 2,
) -> Tuple[List[PriorityClusterAssignment], List[PriorityCluster]]:
    """Cluster priority entries by geographic proximity (Req 3.4.1, 3.4.2, 3.4.3, 3.4.4).

    Runs ``sklearn.cluster.DBSCAN`` with the ``haversine`` metric over the
    entries' lat/lon coordinates. Two points belong to the same cluster
    when they fall within ``eps_miles`` great-circle distance of each
    other (Req 3.4.1). A point with fewer than ``min_samples`` dense
    neighbours is labelled noise (Req 3.4.4) — its assignment carries
    ``cluster_id="noise"`` and it is excluded from the cluster summaries.

    The function is deliberately pure. It does not touch ES, Redis, or
    Pydantic validators — the caller (DeliveryPrioritizationAgent for
    persistence, the REST endpoint for listing) stamps the returned
    metadata into whatever container it needs.

    Args:
        entries: Sequence of priority entries. Each entry must expose a
            latitude and a longitude via either attribute access
            (``location_lat``/``location_lon`` or ``lat``/``lon``) or
            mapping keys of the same names. Entries may also expose
            ``priority_bucket`` (string) and ``fuel_grade`` (string) —
            both drive the cluster-level aggregates but are optional.
            An empty sequence returns ``([], [])`` without touching
            sklearn.
        eps_miles: Proximity threshold per Req 3.4.1. Defaults to the
            tenant-configurable ``cluster_eps_miles`` default of ``3.0``.
            Must be strictly positive.
        min_samples: Minimum density for a dense cluster per Req 3.4.4.
            Defaults to the tenant-configurable
            ``cluster_min_samples`` default of ``2``. Must be ``>= 1``.

    Returns:
        A ``(assignments, clusters)`` tuple where:
            * ``assignments`` is a list aligned with ``entries`` — one
              :class:`PriorityClusterAssignment` per input, in input
              order. Noise rows carry ``cluster_id="noise"`` and report
              the point's own location as its centroid.
            * ``clusters`` is a list of :class:`PriorityCluster`
              summaries, one per dense cluster (noise excluded), in the
              order DBSCAN numbered them.

    Raises:
        ValueError: If ``eps_miles`` is non-positive or ``min_samples``
            is less than ``1``.
        TypeError: If any entry is missing a latitude/longitude.
    """

    if eps_miles <= 0:
        raise ValueError(
            f"eps_miles must be strictly positive, got {eps_miles!r}"
        )
    if min_samples < 1:
        raise ValueError(
            f"min_samples must be >= 1, got {min_samples!r}"
        )

    entries = list(entries)
    if not entries:
        return [], []

    coords: List[Tuple[float, float]] = []
    buckets: List[Optional[str]] = []
    fuel_grades: List[Optional[str]] = []
    for idx, entry in enumerate(entries):
        lat, lon = _read_lat_lon(entry, idx)
        coords.append((lat, lon))
        buckets.append(_read_optional_str(entry, "priority_bucket"))
        fuel_grades.append(_read_optional_str(entry, "fuel_grade"))

    # DBSCAN with ``min_samples=1`` would classify every point as its own
    # cluster (never noise). That is well-defined but defeats the purpose
    # of the Req 3.4.4 noise semantics, so we still run sklearn for
    # consistency but document the behaviour in the docstring.
    labels = _run_dbscan(coords, eps_miles=eps_miles, min_samples=min_samples)

    # ``labels`` uses sklearn's conventions: non-negative integers for
    # dense clusters (numbered from 0), ``-1`` for noise. We translate to
    # the spec's wire format before returning.
    cluster_id_by_label: Dict[int, str] = {}
    members_by_label: Dict[int, List[int]] = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue
        members_by_label.setdefault(label, []).append(idx)
        cluster_id_by_label.setdefault(label, f"cluster_{label}")

    # Precompute centroid + size per dense cluster so each assignment
    # row can look them up in O(1).
    size_by_label: Dict[int, int] = {}
    centroid_by_label: Dict[int, Dict[str, float]] = {}
    for label, member_idx_list in members_by_label.items():
        size_by_label[label] = len(member_idx_list)
        lat_sum = sum(coords[i][0] for i in member_idx_list)
        lon_sum = sum(coords[i][1] for i in member_idx_list)
        centroid_by_label[label] = {
            "lat": lat_sum / len(member_idx_list),
            "lon": lon_sum / len(member_idx_list),
        }

    assignments: List[PriorityClusterAssignment] = []
    for idx, label in enumerate(labels):
        if label == -1:
            assignments.append(
                PriorityClusterAssignment(
                    cluster_id=NOISE_CLUSTER_ID,
                    cluster_size=1,
                    cluster_centroid={
                        "lat": coords[idx][0],
                        "lon": coords[idx][1],
                    },
                )
            )
            continue
        assignments.append(
            PriorityClusterAssignment(
                cluster_id=cluster_id_by_label[label],
                cluster_size=size_by_label[label],
                cluster_centroid=dict(centroid_by_label[label]),
            )
        )

    # Cluster summaries — one per dense label, in label order. DBSCAN
    # yields non-negative labels counting from zero in the order clusters
    # are discovered, so sorting by label is the natural "order clusters
    # were found" ordering the UI expects.
    clusters: List[PriorityCluster] = []
    for label in sorted(members_by_label.keys()):
        member_idx_list = members_by_label[label]
        cluster_bucket = _highest_priority_bucket(
            buckets[i] for i in member_idx_list
        )
        cluster_fuel_grades = sorted(
            {
                fuel_grades[i]
                for i in member_idx_list
                if fuel_grades[i] is not None
            }
        )
        clusters.append(
            PriorityCluster(
                cluster_id=cluster_id_by_label[label],
                centroid=dict(centroid_by_label[label]),
                member_count=size_by_label[label],
                highest_priority_bucket=cluster_bucket,
                fuel_grades=cluster_fuel_grades,
            )
        )
    return assignments, clusters


# ---------------------------------------------------------------------------
# Cluster internal helpers
# ---------------------------------------------------------------------------


def _run_dbscan(
    coords: Sequence[Tuple[float, float]],
    *,
    eps_miles: float,
    min_samples: int,
) -> List[int]:
    """Run sklearn DBSCAN with the haversine metric on lat/lon pairs.

    Isolated so tests that only care about the aggregation logic can
    monkeypatch this function without spinning up sklearn and numpy.

    Haversine distance in sklearn returns radians, so we convert both
    the input coordinates and the eps threshold into radians before
    calling ``fit``. Mean-radius-of-Earth of 3958.8 miles matches the
    value used by :func:`haversine_distance_meters` in
    :mod:`driver.services.geo_utils` to within 0.001% — any drift from
    that constant would skew eps comparisons near the threshold.
    """

    # Local imports keep the ``prioritization_helpers`` module light for
    # callers (like tests) that never invoke the cluster helpers. Both
    # libraries are already pinned in requirements.txt.
    import numpy as np  # type: ignore[import-not-found]
    from sklearn.cluster import DBSCAN  # type: ignore[import-not-found]

    eps_radians = eps_miles / EARTH_RADIUS_MILES
    coords_array = np.asarray(coords, dtype=float)
    radians = np.radians(coords_array)
    model = DBSCAN(
        eps=eps_radians,
        min_samples=min_samples,
        metric="haversine",
        algorithm="ball_tree",
    )
    labels = model.fit_predict(radians)
    return [int(label) for label in labels]


def _read_lat_lon(entry: ClusterEntry, idx: int) -> Tuple[float, float]:
    """Extract a lat/lon pair from a cluster entry.

    Supports both attribute access and mapping access, and both the
    ``location_lat``/``location_lon`` naming used by
    :class:`CombinableGroupEntry` and the ``lat``/``lon`` naming used by
    simpler dict payloads. Raises :class:`TypeError` with the entry's
    index when neither convention yields a coordinate pair.
    """

    lat = _read_number(entry, ("location_lat", "lat"))
    lon = _read_number(entry, ("location_lon", "lon"))
    if lat is None or lon is None:
        raise TypeError(
            f"entries[{idx}] must expose a lat/lon pair "
            "(location_lat/location_lon or lat/lon)"
        )
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(
            f"entries[{idx}] location_lat out of range: {lat!r}"
        )
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(
            f"entries[{idx}] location_lon out of range: {lon!r}"
        )
    return lat, lon


def _read_number(
    entry: ClusterEntry, names: Tuple[str, ...]
) -> Optional[float]:
    """Return the first numeric value among ``names`` or ``None``."""

    for name in names:
        if isinstance(entry, Mapping):
            raw = entry.get(name)
        else:
            raw = getattr(entry, name, None)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _read_optional_str(entry: ClusterEntry, name: str) -> Optional[str]:
    """Return a stripped string value for ``name`` or ``None``.

    Both attribute and mapping access are tried. Enum instances are
    coerced to their ``.value`` so :class:`PriorityBucket`-style inputs
    work without special casing.
    """

    if isinstance(entry, Mapping):
        raw = entry.get(name)
    else:
        raw = getattr(entry, name, None)
    if raw is None:
        return None
    if hasattr(raw, "value") and not isinstance(raw, str):
        raw = raw.value  # Enum → value
    if not isinstance(raw, str):
        raw = str(raw)
    stripped = raw.strip()
    return stripped or None


def _highest_priority_bucket(
    buckets: Iterable[Optional[str]],
) -> Optional[str]:
    """Return the most urgent bucket among ``buckets`` or ``None``.

    Casing is normalized to match :data:`_BUCKET_RANK` keys. Values not
    in the recognised set are ignored so a typo never causes the cluster
    to surface a meaningless bucket.
    """

    best_rank: Optional[int] = None
    best_bucket: Optional[str] = None
    for raw in buckets:
        if raw is None:
            continue
        key = raw.lower()
        rank = _BUCKET_RANK.get(key)
        if rank is None:
            continue
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_bucket = key
    return best_bucket


__all__.extend(
    [
        "EARTH_RADIUS_MILES",
        "NOISE_CLUSTER_ID",
        "PriorityClusterAssignment",
        "PriorityCluster",
        "compute_priority_clusters",
    ]
)
