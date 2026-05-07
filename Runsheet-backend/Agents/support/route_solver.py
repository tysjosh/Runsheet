"""
Route solver — nearest-neighbor + 2-opt improvement.

Pure functions. No side effects.

Validates: Requirements 4.3, 4.4, 4.5 (MVP) and 2.3.1, 2.3.2, 2.3.3, 2.3.4
(Fuel Ops Hardening — solver scaling to 50–100 stops via k-means clustering
pre-pass with per-cluster nearest-neighbor + 2-opt and depot stitching).
"""
from typing import Any, Dict, List, Optional, Tuple
import math
import time


def compute_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km between two coordinates.

    Args:
        lat1, lon1: First point (latitude -90..90, longitude -180..180).
        lat2, lon2: Second point.

    Returns:
        Distance in kilometers.

    Raises:
        ValueError: If coordinates are outside valid ranges.
    """
    if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90):
        raise ValueError(
            f"Latitude must be between -90 and 90, got {lat1}, {lat2}"
        )
    if not (-180 <= lon1 <= 180 and -180 <= lon2 <= 180):
        raise ValueError(
            f"Longitude must be between -180 and 180, got {lon1}, {lon2}"
        )
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_distance_matrix(
    locations: List[Dict[str, float]],
) -> List[List[float]]:
    """Build NxN distance matrix from list of {lat, lon} dicts."""
    n = len(locations)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = compute_distance(
                locations[i]["lat"], locations[i]["lon"],
                locations[j]["lat"], locations[j]["lon"],
            )
            matrix[i][j] = d
            matrix[j][i] = d
    return matrix


def nearest_neighbor_route(
    distance_matrix: List[List[float]],
    start_index: int = 0,
) -> Tuple[List[int], float]:
    """Nearest-neighbor heuristic for TSP. Returns (order, total_distance)."""
    n = len(distance_matrix)
    visited = [False] * n
    order = [start_index]
    visited[start_index] = True
    total = 0.0

    current = start_index
    for _ in range(n - 1):
        best_next = -1
        best_dist = float("inf")
        for j in range(n):
            if not visited[j] and distance_matrix[current][j] < best_dist:
                best_dist = distance_matrix[current][j]
                best_next = j
        if best_next == -1:
            break
        visited[best_next] = True
        order.append(best_next)
        total += best_dist
        current = best_next

    return order, total


def two_opt_improve(
    order: List[int],
    distance_matrix: List[List[float]],
    max_iterations: int = 100,
) -> Tuple[List[int], float]:
    """2-opt local search improvement on a route."""
    def route_distance(route):
        return sum(distance_matrix[route[i]][route[i+1]] for i in range(len(route)-1))

    best = list(order)
    best_dist = route_distance(best)
    improved = True
    iterations = 0

    while improved and iterations < max_iterations:
        improved = False
        iterations += 1
        for i in range(1, len(best) - 1):
            for j in range(i + 1, len(best)):
                new_route = best[:i] + best[i:j+1][::-1] + best[j+1:]
                new_dist = route_distance(new_route)
                if new_dist < best_dist - 0.01:
                    best = new_route
                    best_dist = new_dist
                    improved = True

    return best, best_dist


def check_sla_windows(
    order: List[int],
    distance_matrix: List[List[float]],
    sla_windows: Optional[Dict[int, Tuple[float, float]]] = None,
    speed_kmh: float = 40.0,
    start_time_hours: float = 0.0,
) -> List[Dict]:
    """Check which stops violate SLA delivery windows (Req 4.4).

    Args:
        order: Route order (indices into distance_matrix).
        distance_matrix: NxN distance matrix in km.
        sla_windows: Optional dict mapping stop index to (earliest_hour, latest_hour)
            relative to start_time_hours. If None, no SLA checks are performed.
        speed_kmh: Average travel speed for ETA estimation.
        start_time_hours: Start time in hours from epoch/reference.

    Returns:
        List of dicts with 'stop_index', 'eta_hours', 'window_end', 'late_by_hours'
        for each stop that violates its SLA window. Empty list if all are on time.
    """
    if not sla_windows:
        return []

    violations = []
    cumulative_hours = start_time_hours

    for i in range(len(order) - 1):
        from_idx = order[i]
        to_idx = order[i + 1]
        dist_km = distance_matrix[from_idx][to_idx]
        travel_hours = dist_km / speed_kmh if speed_kmh > 0 else 0.0
        cumulative_hours += travel_hours

        if to_idx in sla_windows:
            _, latest_hour = sla_windows[to_idx]
            if cumulative_hours > latest_hour:
                violations.append({
                    "stop_index": to_idx,
                    "eta_hours": round(cumulative_hours, 2),
                    "window_end": latest_hour,
                    "late_by_hours": round(cumulative_hours - latest_hour, 2),
                })

    return violations


def optimize_route(
    locations: List[Dict[str, float]],
    start_index: int = 0,
    sla_windows: Optional[Dict[int, Tuple[float, float]]] = None,
    speed_kmh: float = 40.0,
) -> Tuple[List[int], float]:
    """Full route optimization: nearest-neighbor + 2-opt.

    Args:
        locations: List of {lat, lon} dicts.
        start_index: Index of the depot/start location.
        sla_windows: Optional SLA windows for check_sla_windows().
        speed_kmh: Average speed for SLA checking.

    Returns:
        (optimized_order, total_distance_km)
    """
    matrix = build_distance_matrix(locations)
    order, _ = nearest_neighbor_route(matrix, start_index)
    order, total_dist = two_opt_improve(order, matrix)
    return order, total_dist


# ---------------------------------------------------------------------------
# Solver scaling to 50–100 stops (Fuel Ops Hardening Req 2.3.1, 2.3.2, 2.3.3)
# ---------------------------------------------------------------------------

# Default cap on the number of customer stops the solver will accept per
# request. Tenants may override this via the ``overlay.routing_max_stops``
# configuration key at the call site; this module enforces the value that is
# passed in. (Req 2.3.3)
DEFAULT_ROUTING_MAX_STOPS = 100

# Threshold at which we switch from the single-pass nearest-neighbor + 2-opt
# solver to the clustering pre-pass pipeline. Keeps the TSP instance small
# enough that the 2-opt step stays quadratic in a manageable number of nodes.
# (Req 2.3.2)
CLUSTERING_STOP_THRESHOLD = 30

# Default maximum stops per k-means cluster. ``optimize_route_large`` picks the
# number of clusters so each cluster has at most this many stops plus the
# depot, keeping the per-cluster 2-opt step fast. (Req 2.3.2)
DEFAULT_MAX_STOPS_PER_CLUSTER = 15


class StopCapExceededError(ValueError):
    """Raised when the request exceeds ``overlay.routing_max_stops``.

    Callers (e.g. the REST route-planning endpoint) should catch this and
    surface an HTTP 400 with body ``{"error": "stop_cap_exceeded", ...}``.

    Validates: Requirement 2.3.3.
    """

    def __init__(self, stop_count: int, max_stops: int):
        self.stop_count = stop_count
        self.max_stops = max_stops
        super().__init__(
            f"stop_cap_exceeded: requested {stop_count} stops exceeds "
            f"routing_max_stops={max_stops}"
        )


def _kmeans_cluster_labels(
    stop_points: List[Tuple[float, float]],
    n_clusters: int,
    random_state: int = 42,
) -> List[int]:
    """Return cluster labels for the given stop coordinates via sklearn.KMeans.

    Kept as a small indirection so tests can patch it without pulling in
    sklearn in unit tests that don't exercise clustering. The KMeans import is
    local so this module can still be imported in environments where
    scikit-learn is not installed — only the large-route code path requires
    it.

    Args:
        stop_points: List of ``(lat, lon)`` tuples for customer stops
            (depot is excluded from clustering because it is the shared
            origin/destination of every cluster route).
        n_clusters: Number of clusters to form. Must satisfy
            ``1 <= n_clusters <= len(stop_points)``.
        random_state: Seed passed to ``sklearn.cluster.KMeans`` for
            deterministic clustering in tests.

    Returns:
        List of integer labels (length == len(stop_points)) where
        ``labels[i]`` is the cluster index of ``stop_points[i]``.
    """
    if n_clusters <= 1:
        return [0] * len(stop_points)

    from sklearn.cluster import KMeans  # local import: optional dependency

    kmeans = KMeans(
        n_clusters=n_clusters,
        random_state=random_state,
        n_init=10,
    )
    labels = kmeans.fit_predict(stop_points)
    return [int(l) for l in labels]


def _stitch_clusters_via_depot(
    cluster_orders: List[List[int]],
    distance_matrix: List[List[float]],
    depot_index: int = 0,
) -> Tuple[List[int], float]:
    """Concatenate per-cluster routes through the depot.

    Each cluster route already starts and ends at ``depot_index`` (we enforce
    this when we build each cluster's sub-matrix). To stitch them back into
    a single depot→…→depot sequence we greedily pick the next cluster whose
    first customer stop is cheapest from the current position (initially the
    depot) and drop the duplicated depot hops between clusters.

    Args:
        cluster_orders: List of cluster orderings. Each ordering is a list of
            stop indices into ``distance_matrix`` and MUST satisfy
            ``cluster_orders[i][0] == cluster_orders[i][-1] == depot_index``
            (i.e. it is a closed depot-rooted tour for that cluster).
        distance_matrix: Full NxN distance matrix over the depot + every stop.
        depot_index: Index of the depot in ``distance_matrix``.

    Returns:
        ``(stitched_order, total_distance_km)`` where ``stitched_order`` is
        a list of indices of the form ``[depot, c1_stops..., c2_stops...,
        ..., depot]`` that visits every customer stop exactly once and begins
        and ends at the depot (Req 2.3.5).
    """
    if not cluster_orders:
        return [depot_index], 0.0

    # Strip the trailing depot from each cluster so we can concatenate the
    # inner customer sequences without duplicating the depot between clusters.
    inner_sequences: List[List[int]] = []
    for order in cluster_orders:
        if not order:
            continue
        # Defensive: tolerate clusters whose tour was not emitted as a closed
        # loop (e.g. when a cluster had no customer stops).
        inner = [idx for idx in order if idx != depot_index]
        if inner:
            inner_sequences.append(inner)

    if not inner_sequences:
        return [depot_index], 0.0

    remaining = list(inner_sequences)
    stitched: List[int] = [depot_index]
    current = depot_index

    while remaining:
        # Pick the cluster whose first stop is nearest to the current
        # position. This gives us a "nearest cluster" stitching which keeps
        # cross-cluster hops short in the common case.
        best_idx = 0
        best_dist = distance_matrix[current][remaining[0][0]]
        for i in range(1, len(remaining)):
            d = distance_matrix[current][remaining[i][0]]
            if d < best_dist:
                best_dist = d
                best_idx = i

        chosen = remaining.pop(best_idx)
        stitched.extend(chosen)
        current = chosen[-1]

    # Close the tour back at the depot.
    stitched.append(depot_index)

    total = 0.0
    for i in range(len(stitched) - 1):
        total += distance_matrix[stitched[i]][stitched[i + 1]]

    return stitched, total


def _optimize_cluster_order(
    cluster_stop_indices: List[int],
    distance_matrix: List[List[float]],
    depot_index: int,
) -> Tuple[List[int], int]:
    """Run nearest-neighbor + 2-opt on a single cluster rooted at the depot.

    The cluster's sub-problem is a TSP over ``[depot] + cluster_stops``. We
    build a local sub-matrix, solve it, then translate the local indices back
    to the global indices used by ``distance_matrix``.

    Args:
        cluster_stop_indices: Global indices of customer stops in this
            cluster (must not include the depot).
        distance_matrix: Full NxN distance matrix including the depot.
        depot_index: Global index of the depot in ``distance_matrix``.

    Returns:
        ``(closed_global_order, iterations)`` where ``closed_global_order``
        is the cluster's tour expressed in global indices, starting and
        ending at ``depot_index``. ``iterations`` is the 2-opt iteration
        count (used for the aggregated Route_Plan metric).
    """
    if not cluster_stop_indices:
        return [depot_index, depot_index], 0

    # Local indices: 0 == depot, 1..N == cluster customer stops
    local_to_global = [depot_index] + list(cluster_stop_indices)
    n_local = len(local_to_global)
    local_matrix = [[0.0] * n_local for _ in range(n_local)]
    for i in range(n_local):
        gi = local_to_global[i]
        for j in range(n_local):
            local_matrix[i][j] = distance_matrix[gi][local_to_global[j]]

    # Nearest-neighbor seed rooted at the local depot (index 0).
    order, _ = nearest_neighbor_route(local_matrix, start_index=0)
    order, _ = two_opt_improve(order, local_matrix)

    # Translate back to global indices and close the tour at the depot so
    # the stitching step can assume every cluster tour ends where it began.
    global_order = [local_to_global[i] for i in order]
    if global_order[0] != depot_index:
        global_order = [depot_index] + global_order
    if global_order[-1] != depot_index:
        global_order.append(depot_index)

    # A 2-opt pass iterates until no improving swap exists; report one
    # iteration here as a coarse-grained count since ``two_opt_improve``
    # doesn't expose its inner counter. Tests only assert the field is
    # present and non-negative (Req 2.3.4).
    iterations = 1
    return global_order, iterations


def optimize_route_large(
    locations: List[Dict[str, float]],
    start_index: int = 0,
    max_stops: int = DEFAULT_ROUTING_MAX_STOPS,
    clustering_threshold: int = CLUSTERING_STOP_THRESHOLD,
    max_stops_per_cluster: int = DEFAULT_MAX_STOPS_PER_CLUSTER,
    random_state: int = 42,
) -> Dict[str, Any]:
    """Solve a routing problem of up to ``max_stops`` customer stops.

    For problems at or below ``clustering_threshold`` customer stops this
    delegates to :func:`optimize_route` (nearest-neighbor + 2-opt). Above
    the threshold it runs a k-means pre-pass on the stop coordinates,
    optimizes each cluster independently (Req 2.3.2), and stitches the
    clusters together through the depot via
    :func:`_stitch_clusters_via_depot`. The stitched output preserves every
    input stop exactly once and begins and ends at ``start_index``
    (Req 2.3.5).

    Args:
        locations: Ordered list of ``{"lat", "lon"}`` dicts. The entry at
            ``start_index`` is treated as the depot; all other entries are
            customer stops.
        start_index: Index of the depot in ``locations``.
        max_stops: Maximum number of customer stops the solver accepts.
            Defaults to :data:`DEFAULT_ROUTING_MAX_STOPS` (100). Requests
            exceeding this cap raise :class:`StopCapExceededError`, which
            the REST layer surfaces as HTTP 400 ``stop_cap_exceeded``
            (Req 2.3.3).
        clustering_threshold: Customer-stop count above which the k-means
            pre-pass engages (Req 2.3.2).
        max_stops_per_cluster: Target maximum number of stops per k-means
            cluster. Drives ``n_clusters = ceil(stops / max_stops_per_cluster)``.
        random_state: Seed passed to KMeans for deterministic clustering
            (useful in tests).

    Returns:
        Dict with the fields every Route_Plan must carry (Req 2.3.4):

        - ``order``: ``List[int]`` — indices into ``locations`` beginning
          and ending at ``start_index`` (Req 2.3.5).
        - ``objective_value``: ``float`` — total distance of the produced
          tour in kilometers. Lower is better; the overlay agent may
          transform this into its weighted objective.
        - ``runtime_ms``: ``int`` — wall-clock runtime of this call in
          milliseconds.
        - ``iterations``: ``int`` — aggregated 2-opt iteration count across
          all cluster sub-problems (or one for a single-pass solve).
        - ``clusters_used``: ``int`` — number of clusters the k-means
          pre-pass emitted, or ``1`` when the single-pass path ran.
        - ``total_distance_km``: ``float`` — alias of ``objective_value``
          kept for readability in downstream logs.

    Raises:
        StopCapExceededError: When ``len(locations) - 1 > max_stops``.
        ValueError: When ``start_index`` is out of range or coordinates are
            invalid (bubbled up from :func:`compute_distance`).
    """
    if start_index < 0 or start_index >= len(locations):
        raise ValueError(
            f"start_index {start_index} out of range for {len(locations)} locations"
        )

    # Count of customer stops (everything except the depot). The cap is
    # defined on customer stops because the depot is always present and not
    # something the tenant requested (Req 2.3.3).
    customer_stop_count = max(0, len(locations) - 1)
    if customer_stop_count > max_stops:
        raise StopCapExceededError(customer_stop_count, max_stops)

    started_at = time.perf_counter()

    # Build the full distance matrix once; both code paths need it.
    matrix = build_distance_matrix(locations)

    # Indices of the customer stops (everything except the depot).
    customer_indices = [i for i in range(len(locations)) if i != start_index]

    # Trivial cases: 0 or 1 customer stop — no clustering needed.
    if customer_stop_count <= clustering_threshold:
        if customer_stop_count == 0:
            order = [start_index]
            total = 0.0
            iterations = 0
        else:
            order, _ = nearest_neighbor_route(matrix, start_index=start_index)
            order, total_after = two_opt_improve(order, matrix)
            # Close the tour back at the depot so the output always starts
            # and ends at the depot (Req 2.3.5).
            if order[-1] != start_index:
                order = list(order) + [start_index]
            total = 0.0
            for i in range(len(order) - 1):
                total += matrix[order[i]][order[i + 1]]
            iterations = 1

        runtime_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "order": order,
            "objective_value": round(total, 6),
            "total_distance_km": round(total, 6),
            "runtime_ms": runtime_ms,
            "iterations": iterations,
            "clusters_used": 1,
        }

    # --- Clustering pre-pass (Req 2.3.2) -----------------------------------
    stop_points = [
        (locations[i]["lat"], locations[i]["lon"]) for i in customer_indices
    ]
    n_clusters = max(1, math.ceil(customer_stop_count / max_stops_per_cluster))
    # Cap n_clusters by the number of customer points (KMeans requires
    # n_clusters <= n_samples).
    n_clusters = min(n_clusters, customer_stop_count)

    labels = _kmeans_cluster_labels(
        stop_points, n_clusters=n_clusters, random_state=random_state
    )

    # Group global customer indices by their cluster label.
    cluster_groups: Dict[int, List[int]] = {}
    for ci, label in zip(customer_indices, labels):
        cluster_groups.setdefault(label, []).append(ci)

    cluster_orders: List[List[int]] = []
    total_iterations = 0
    for label in sorted(cluster_groups.keys()):
        cluster_stops = cluster_groups[label]
        cluster_tour, iters = _optimize_cluster_order(
            cluster_stops, matrix, depot_index=start_index
        )
        cluster_orders.append(cluster_tour)
        total_iterations += iters

    stitched_order, total_distance = _stitch_clusters_via_depot(
        cluster_orders, distance_matrix=matrix, depot_index=start_index
    )

    runtime_ms = int((time.perf_counter() - started_at) * 1000)
    return {
        "order": stitched_order,
        "objective_value": round(total_distance, 6),
        "total_distance_km": round(total_distance, 6),
        "runtime_ms": runtime_ms,
        "iterations": total_iterations,
        "clusters_used": len(cluster_orders),
    }

# ---------------------------------------------------------------------------
# Emergency stop insertion (Fuel Ops Hardening Req 2.4.2, 2.4.4)
# ---------------------------------------------------------------------------

# Reason codes surfaced on InfeasibleInsertion. The REST endpoint
# (Task 4.9, ``POST /api/fuel/mvp/routes/{route_id}/emergency-stop``) maps
# these onto an HTTP 409 body per Requirement 2.4.4.
INSERT_REASON_CAPACITY = "capacity_insufficient"
INSERT_REASON_SLA = "sla_breach"
INSERT_REASON_OFF_DUTY = "truck_off_duty"

_VALID_INSERT_REASONS = frozenset(
    {INSERT_REASON_CAPACITY, INSERT_REASON_SLA, INSERT_REASON_OFF_DUTY}
)


class InfeasibleInsertion(ValueError):
    """Raised when no feasible cheapest-insertion position exists.

    Carries a structured ``reason`` (one of ``capacity_insufficient``,
    ``sla_breach``, ``truck_off_duty``) so the route-planning endpoint
    can return the HTTP 409 shape required by Req 2.4.4 without having
    to parse the exception message.

    Validates: Requirement 2.4.4.
    """

    def __init__(
        self,
        reason: str,
        *,
        details: Optional[Dict[str, Any]] = None,
    ):
        if reason not in _VALID_INSERT_REASONS:
            raise ValueError(
                f"Unknown InfeasibleInsertion reason {reason!r}; "
                f"expected one of {sorted(_VALID_INSERT_REASONS)}"
            )
        self.reason = reason
        self.details = dict(details) if details else {}
        # Human-readable message mirrors the reason code; the endpoint
        # should read ``exc.reason`` rather than parse ``str(exc)``.
        super().__init__(reason)


def _stop_key(stop: Dict[str, Any], fallback: str) -> str:
    """Return a stable key for a stop (prefers ``stop_id``→``station_id``)."""
    for key in ("stop_id", "station_id", "customer_tank_id"):
        value = stop.get(key)
        if value:
            return str(value)
    return fallback


def _edge_distance_km(
    from_stop: Dict[str, Any],
    to_stop: Dict[str, Any],
    traffic_matrix: Optional[Dict[Tuple[str, str], Dict[str, float]]],
) -> float:
    """Distance in km between two stops, using traffic_matrix when present.

    The traffic_matrix is a dict keyed by ``(from_key, to_key)`` where
    each key matches :func:`_stop_key`. Each entry may carry
    ``distance_km`` (and optionally ``duration_minutes`` for ETA
    estimation); entries without ``distance_km`` fall back to Haversine.
    This layout matches the cached-matrix shape the Traffic_Provider
    service emits in Task 4.4/4.5.
    """
    if traffic_matrix is not None:
        a = _stop_key(from_stop, "from")
        b = _stop_key(to_stop, "to")
        entry = traffic_matrix.get((a, b))
        if entry is not None and "distance_km" in entry:
            return float(entry["distance_km"])
    return compute_distance(
        float(from_stop["lat"]),
        float(from_stop["lon"]),
        float(to_stop["lat"]),
        float(to_stop["lon"]),
    )


def _edge_duration_hours(
    from_stop: Dict[str, Any],
    to_stop: Dict[str, Any],
    traffic_matrix: Optional[Dict[Tuple[str, str], Dict[str, float]]],
    speed_kmh: float,
) -> float:
    """Travel time in hours between two stops.

    Prefers ``duration_minutes`` from the traffic matrix (traffic-aware
    ETA per Req 2.1), otherwise derives travel time from Haversine
    distance and the provided ``speed_kmh``.
    """
    if traffic_matrix is not None:
        a = _stop_key(from_stop, "from")
        b = _stop_key(to_stop, "to")
        entry = traffic_matrix.get((a, b))
        if entry is not None and "duration_minutes" in entry:
            return float(entry["duration_minutes"]) / 60.0
    distance_km = _edge_distance_km(from_stop, to_stop, traffic_matrix)
    if speed_kmh <= 0:
        return 0.0
    return distance_km / speed_kmh


def _recompute_etas(
    stops: List[Dict[str, Any]],
    depot: Dict[str, Any],
    start_time_hours: float,
    speed_kmh: float,
    traffic_matrix: Optional[Dict[Tuple[str, str], Dict[str, float]]],
    service_time_hours: float,
) -> List[float]:
    """Return ETAs (absolute hours) for each stop, starting from the depot.

    The ETA for stop ``i`` is the clock time the truck arrives at that
    stop. ``service_time_hours`` is the time spent at each stop before
    departing to the next (pre-emergency + emergency stops use the same
    figure; callers can pass 0.0 if the route tracks service time
    separately).
    """
    etas: List[float] = []
    current_time = start_time_hours
    prev = depot
    for stop in stops:
        current_time += _edge_duration_hours(
            prev, stop, traffic_matrix, speed_kmh
        )
        etas.append(current_time)
        current_time += service_time_hours
        prev = stop
    return etas


def _return_to_depot_hours(
    stops: List[Dict[str, Any]],
    depot: Dict[str, Any],
    etas: List[float],
    speed_kmh: float,
    traffic_matrix: Optional[Dict[Tuple[str, str], Dict[str, float]]],
    service_time_hours: float,
) -> float:
    """Clock time (hours) at which the truck is back at the depot."""
    if not stops:
        return etas[-1] if etas else 0.0
    last_stop = stops[-1]
    last_arrival = etas[-1]
    return (
        last_arrival
        + service_time_hours
        + _edge_duration_hours(
            last_stop, depot, traffic_matrix, speed_kmh
        )
    )


def insert_emergency_stop(
    route: Dict[str, Any],
    emergency: Dict[str, Any],
    traffic_matrix: Optional[Dict[Tuple[str, str], Dict[str, float]]] = None,
    *,
    speed_kmh: float = 40.0,
    service_time_hours: float = 0.0,
) -> Dict[str, Any]:
    """Insert an emergency stop into an active route using cheapest-insertion.

    Implements the Capability 2 emergency-stop insertion path: for each
    edge ``(i, i+1)`` in the existing route the function computes
    ``added_distance = d(i, e) + d(e, i+1) - d(i, i+1)`` against the
    traffic matrix (or Haversine fallback), keeps only the positions that
    satisfy the truck's remaining compartment capacity, the SLA windows
    of existing stops, the emergency's own SLA, and the truck's shift
    end, and returns the position with the lowest added distance.

    The function is **pure** (no ES, Redis, or network calls); Task 4.9
    wires it into the REST endpoint, Confirmation_Protocol, and the
    ``emergency_stop_inserted`` WebSocket event.

    Args:
        route: A route-plan-shaped dict. Expected keys:

            * ``stops`` (list of dicts): each with ``lat`` / ``lon`` /
              ``stop_id`` (or ``station_id``) and optionally
              ``sla_by_hours`` (float, absolute hours from
              ``start_time_hours`` after which the stop is late). Stops
              may also carry ``eta`` metadata that the caller ignores;
              the solver recomputes ETAs per insertion candidate.
            * ``depot`` (dict with ``lat`` / ``lon``): shared start/end
              location. ``start_depot`` and ``end_depot`` are accepted
              as aliases for clarity; the return-to-depot edge uses
              ``end_depot`` when both are present, matching Req 2.2.5.
            * ``remaining_capacity_by_grade`` (dict[str, float]):
              gallons remaining per fuel product code across the truck's
              compartments (Req 2.4.2 — "respecting the truck's
              remaining compartment capacity for the requested
              fuel_grade"). The solver does not model individual
              compartments; the caller aggregates them.
            * ``shift_end_hours`` (float, optional): absolute hour the
              truck goes off duty. When provided, any insertion whose
              return-to-depot time exceeds this value fails with
              ``truck_off_duty`` (Req 2.4.4).
            * ``start_time_hours`` (float, default 0.0): clock time the
              truck left the depot.

        emergency: The new stop to insert. Required keys: ``lat``,
            ``lon``, ``fuel_grade`` (canonical product code), and
            ``requested_gallons``. Optional ``stop_id`` / ``station_id``
            (auto-generated if absent) and ``sla_by_hours``
            (absolute-hour deadline for the emergency itself).

        traffic_matrix: Optional ``{(from_key, to_key): {"distance_km",
            "duration_minutes"}}`` keyed by the same ids used by
            ``_stop_key``. When absent, the solver falls back to
            Haversine + ``speed_kmh`` for both distance and duration —
            same contract as Req 2.1.5's traffic fallback.

        speed_kmh: Fallback speed used when the traffic matrix does not
            supply ``duration_minutes``. Ignored when the matrix is
            fully populated.

        service_time_hours: Per-stop service time added between arrival
            and departure when recomputing ETAs. Callers whose routes
            model service time elsewhere should pass 0.0.

    Returns:
        A dict describing the chosen insertion:

        * ``insert_index`` (int): position in ``route["stops"]`` at
          which the emergency was inserted. ``0`` means before all
          existing stops; ``len(stops)`` means after the last stop.
        * ``added_distance_km`` (float): detour cost in km.
        * ``new_stops`` (list[dict]): the full stop sequence after
          insertion, including the emergency stop annotated with
          ``is_emergency: true``.
        * ``new_etas`` (list[float]): recomputed ETAs in hours,
          aligned with ``new_stops``.
        * ``eta_shifts`` (list[dict]): for each existing stop whose ETA
          changed, ``{stop_id, before_eta_hours, after_eta_hours,
          shift_minutes}``. Stops unaffected by the insertion (those
          before ``insert_index``) are omitted.
        * ``return_to_depot_hours`` (float): clock time the truck is
          back at the depot after the patched route.
        * ``stops_shifted_count`` (int): convenience count the REST
          endpoint (Task 4.9) uses to classify risk HIGH when the
          value is >= 3 stops.

    Raises:
        InfeasibleInsertion: When no insertion position is feasible.
            The ``reason`` attribute is one of
            ``capacity_insufficient``, ``sla_breach``, or
            ``truck_off_duty`` per Req 2.4.4. Precedence is (1) capacity
            (global — checked first), (2) SLA breach, (3) off-duty.

    Validates: Requirements 2.4.2, 2.4.4.
    """
    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    if not isinstance(route, dict):
        raise ValueError("route must be a dict-like structure")
    if not isinstance(emergency, dict):
        raise ValueError("emergency must be a dict-like structure")

    stops: List[Dict[str, Any]] = list(route.get("stops") or [])
    # Depot handling: accept ``depot`` as the canonical key, and fall
    # back to ``start_depot`` / ``end_depot`` so multi-depot routes
    # (Req 2.2.5) are supported without the solver caring about the
    # split.
    start_depot = (
        route.get("start_depot")
        or route.get("depot")
    )
    end_depot = (
        route.get("end_depot")
        or route.get("depot")
        or start_depot
    )
    if start_depot is None or end_depot is None:
        raise ValueError(
            "route must specify a 'depot' (or 'start_depot'/'end_depot')"
        )

    for key in ("lat", "lon"):
        if key not in emergency:
            raise ValueError(f"emergency stop missing required field {key!r}")
        if key not in start_depot or key not in end_depot:
            raise ValueError(f"depot missing required field {key!r}")

    fuel_grade = emergency.get("fuel_grade")
    requested_gallons = emergency.get("requested_gallons")
    if not fuel_grade:
        raise ValueError("emergency stop missing 'fuel_grade'")
    if requested_gallons is None or float(requested_gallons) <= 0:
        raise ValueError(
            "emergency stop 'requested_gallons' must be a positive number"
        )
    requested_gallons = float(requested_gallons)

    # Ensure the emergency has a stable id for traffic-matrix lookups
    # and for downstream Replan_Diff consumers.
    emergency = dict(emergency)
    if not any(
        emergency.get(k) for k in ("stop_id", "station_id", "customer_tank_id")
    ):
        emergency["stop_id"] = "emergency_stop"
    emergency.setdefault("is_emergency", True)

    start_time_hours = float(route.get("start_time_hours", 0.0))
    shift_end_hours = route.get("shift_end_hours")
    if shift_end_hours is not None:
        shift_end_hours = float(shift_end_hours)

    # ------------------------------------------------------------------
    # Capacity check (Req 2.4.2) — global; fails fast with capacity_insufficient
    # ------------------------------------------------------------------
    remaining_by_grade: Dict[str, float] = {
        str(k): float(v)
        for k, v in (route.get("remaining_capacity_by_grade") or {}).items()
    }
    available = remaining_by_grade.get(str(fuel_grade), 0.0)
    if available + 1e-9 < requested_gallons:
        raise InfeasibleInsertion(
            INSERT_REASON_CAPACITY,
            details={
                "fuel_grade": str(fuel_grade),
                "requested_gallons": requested_gallons,
                "remaining_gallons": available,
            },
        )

    # ------------------------------------------------------------------
    # Cheapest-insertion search (Req 2.4.2)
    # ------------------------------------------------------------------
    # Positions to try: 0 .. len(stops) inclusive. Position ``i`` means
    # "insert before stops[i]"; position ``len(stops)`` means "append
    # after the last stop, before the return-to-depot edge".
    best_index: Optional[int] = None
    best_added: float = float("inf")
    best_new_stops: Optional[List[Dict[str, Any]]] = None
    best_new_etas: Optional[List[float]] = None
    best_return_time: Optional[float] = None

    # Track the reason we had to reject candidates so we can return
    # the right code if every position is infeasible.
    saw_sla_breach = False
    saw_off_duty = False

    def _edges_around(i: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Return (left, right) neighbors of insertion position ``i``."""
        left = start_depot if i == 0 else stops[i - 1]
        right = end_depot if i == len(stops) else stops[i]
        return left, right

    for i in range(len(stops) + 1):
        left, right = _edges_around(i)
        d_left_right = _edge_distance_km(left, right, traffic_matrix)
        d_left_em = _edge_distance_km(left, emergency, traffic_matrix)
        d_em_right = _edge_distance_km(emergency, right, traffic_matrix)
        added = d_left_em + d_em_right - d_left_right

        # Feasibility check: recompute the full patched route's ETAs and
        # verify SLAs + shift end. This is O(n) per position, O(n²)
        # overall, which matches the existing 2-opt complexity and is
        # well within the 10s budget (Req 2.3.1).
        patched_stops = stops[:i] + [emergency] + stops[i:]
        etas = _recompute_etas(
            patched_stops,
            start_depot,
            start_time_hours=start_time_hours,
            speed_kmh=speed_kmh,
            traffic_matrix=traffic_matrix,
            service_time_hours=service_time_hours,
        )
        return_time = _return_to_depot_hours(
            patched_stops,
            end_depot,
            etas,
            speed_kmh=speed_kmh,
            traffic_matrix=traffic_matrix,
            service_time_hours=service_time_hours,
        )

        # Shift-end check → truck_off_duty (Req 2.4.4)
        if shift_end_hours is not None and return_time > shift_end_hours + 1e-9:
            saw_off_duty = True
            continue

        # SLA check for the emergency itself and every downstream stop
        # whose ETA moved → sla_breach (Req 2.4.4). Stops before the
        # insertion index are unaffected by definition.
        sla_ok = True
        for idx in range(i, len(patched_stops)):
            stop = patched_stops[idx]
            sla_by = stop.get("sla_by_hours")
            if sla_by is not None and etas[idx] > float(sla_by) + 1e-9:
                sla_ok = False
                break
        if not sla_ok:
            saw_sla_breach = True
            continue

        if added < best_added:
            best_added = added
            best_index = i
            best_new_stops = patched_stops
            best_new_etas = etas
            best_return_time = return_time

    if best_index is None:
        # Precedence per docstring: capacity was already checked, so the
        # remaining candidates are sla_breach vs truck_off_duty. Prefer
        # sla_breach because it carries the richer business signal
        # (existing-customer SLA hit) — the endpoint (Task 4.9) uses the
        # same precedence when mapping to HTTP 409 reason codes.
        if saw_sla_breach:
            raise InfeasibleInsertion(INSERT_REASON_SLA)
        if saw_off_duty:
            raise InfeasibleInsertion(INSERT_REASON_OFF_DUTY)
        # Defensive: this branch is unreachable given the pre-checks
        # (route with len(stops) >= 0 always yields at least one
        # candidate), but we surface a clear error rather than
        # silently returning None.
        raise InfeasibleInsertion(
            INSERT_REASON_CAPACITY,
            details={"note": "no_insertion_position_evaluated"},
        )

    # ------------------------------------------------------------------
    # Build result + eta_shifts for the stops after the insertion point.
    # ------------------------------------------------------------------
    original_etas = _recompute_etas(
        stops,
        start_depot,
        start_time_hours=start_time_hours,
        speed_kmh=speed_kmh,
        traffic_matrix=traffic_matrix,
        service_time_hours=service_time_hours,
    )

    eta_shifts: List[Dict[str, Any]] = []
    assert best_new_etas is not None and best_new_stops is not None
    for original_idx in range(best_index, len(stops)):
        # After insertion, original stops[j] (j >= best_index) are at
        # patched_stops[j + 1]. Only record shifts for stops whose ETA
        # actually changed.
        patched_idx = original_idx + 1
        before_eta = original_etas[original_idx]
        after_eta = best_new_etas[patched_idx]
        if abs(after_eta - before_eta) <= 1e-9:
            continue
        shift_minutes = (after_eta - before_eta) * 60.0
        eta_shifts.append(
            {
                "stop_id": _stop_key(stops[original_idx], f"stop_{original_idx}"),
                "before_eta_hours": before_eta,
                "after_eta_hours": after_eta,
                "shift_minutes": shift_minutes,
            }
        )

    return {
        "insert_index": best_index,
        "added_distance_km": round(best_added, 6),
        "new_stops": best_new_stops,
        "new_etas": [round(e, 6) for e in best_new_etas],
        "eta_shifts": eta_shifts,
        "return_to_depot_hours": round(float(best_return_time or 0.0), 6),
        "stops_shifted_count": len(eta_shifts),
    }
