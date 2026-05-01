"""
Route solver — nearest-neighbor + 2-opt improvement.

Pure functions. No side effects.

Validates: Requirements 4.3, 4.4, 4.5
"""
from typing import Dict, List, Optional, Tuple
import math


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
