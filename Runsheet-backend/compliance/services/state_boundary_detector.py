"""State Boundary Detector — lat/lon → US state lookup with grid-cell caching.

Implements the state boundary detection component described in design §7
(IFTA Reporter) of the Fuel Compliance Backbone spec. Uses US Census TIGER
shapefile data for point-in-polygon state lookups, with a grid-cell caching
strategy to avoid repeated shapefile queries for nearby coordinates.

Grid-cell caching:
    The continental US is divided into 0.1° × 0.1° grid cells (~7mi × 5.5mi).
    Once a state is resolved for a grid cell, subsequent lookups in the same
    cell return the cached result without querying the shapefile. This provides
    O(1) amortized lookups for GPS telemetry that follows road corridors.

Public methods:
    * ``get_state(lat, lon)`` — returns 2-letter state code for a coordinate.
    * ``detect_boundary_crossing(points)`` — detects state boundary crossings
      in a sequence of GPS points.

Validates: Requirement 7.1
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Grid cell size in degrees (0.1° ≈ 7 miles lat, ~5.5 miles lon at 40°N)
GRID_CELL_SIZE: float = 0.1

# Default shapefile path (relative to project root)
DEFAULT_SHAPEFILE_PATH: str = "data/us_states.shp"

# US bounding box (approximate) for quick rejection
US_LAT_MIN: float = 24.0
US_LAT_MAX: float = 50.0
US_LON_MIN: float = -125.0
US_LON_MAX: float = -66.0


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


class BoundaryCrossing(BaseModel):
    """Represents a detected state boundary crossing.

    Emitted when consecutive GPS points are determined to be in different
    states, indicating the vehicle crossed a state line.

    Validates: Requirement 7.1
    """

    model_config = ConfigDict(extra="forbid")

    from_state: str = Field(
        ..., description="2-letter state code of the origin state"
    )
    to_state: str = Field(
        ..., description="2-letter state code of the destination state"
    )
    lat: float = Field(
        ..., description="Latitude where the crossing was detected"
    )
    lon: float = Field(
        ..., description="Longitude where the crossing was detected"
    )
    timestamp: Optional[datetime] = Field(
        default=None,
        description="Timestamp of the GPS reading at the crossing point",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grid_cell_key(lat: float, lon: float) -> Tuple[int, int]:
    """Compute the grid cell key for a given lat/lon coordinate.

    Divides the coordinate space into cells of GRID_CELL_SIZE degrees.
    Returns a tuple of (lat_cell, lon_cell) integers that uniquely
    identify the grid cell.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        Tuple of (lat_cell_index, lon_cell_index).
    """
    lat_cell = int(math.floor(lat / GRID_CELL_SIZE))
    lon_cell = int(math.floor(lon / GRID_CELL_SIZE))
    return (lat_cell, lon_cell)


def _is_within_us_bounds(lat: float, lon: float) -> bool:
    """Quick check whether a coordinate is within the continental US bounding box.

    This is a fast rejection filter — points outside this box cannot be
    in any US state (excluding Alaska/Hawaii which have separate bounds).

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        True if the point is within the approximate US bounding box.
    """
    return (US_LAT_MIN <= lat <= US_LAT_MAX) and (
        US_LON_MIN <= lon <= US_LON_MAX
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StateBoundaryDetector:
    """Service for detecting US state boundaries from GPS coordinates.

    Uses US Census TIGER shapefile data for point-in-polygon state lookups.
    Implements a grid-cell caching strategy where the US is divided into
    0.1° × 0.1° cells, and the state lookup result is cached per cell to
    avoid repeated shapefile queries.

    Args:
        shapefile_path: Path to the US states shapefile (.shp). If None,
            uses the default path ``data/us_states.shp``.

    Validates: Requirement 7.1
    """

    def __init__(self, shapefile_path: Optional[str] = None) -> None:
        self._shapefile_path = shapefile_path or DEFAULT_SHAPEFILE_PATH
        self._grid_cache: Dict[Tuple[int, int], Optional[str]] = {}
        self._state_polygons: Optional[List[Dict[str, Any]]] = None
        self._shapefile_loaded: bool = False
        self._load_attempted: bool = False

    def _load_shapefile(self) -> None:
        """Load the US states shapefile into memory.

        Parses the shapefile using pyshp and converts each state's geometry
        into a shapely Polygon/MultiPolygon for point-in-polygon testing.
        The state abbreviation (STUSPS field) is stored alongside each geometry.

        Gracefully degrades with a warning if the shapefile is not available
        or if required libraries are not installed.
        """
        if self._load_attempted:
            return
        self._load_attempted = True

        shapefile_resolved = Path(self._shapefile_path)
        if not shapefile_resolved.exists():
            logger.warning(
                "StateBoundaryDetector: shapefile not found at %s. "
                "State lookups will return None until the shapefile is "
                "installed (see task 12.11).",
                self._shapefile_path,
            )
            return

        try:
            import shapefile as shp
            from shapely.geometry import MultiPolygon, Polygon, shape
        except ImportError as exc:
            logger.warning(
                "StateBoundaryDetector: required library not available (%s). "
                "Install pyshp and shapely to enable state boundary detection.",
                exc,
            )
            return

        try:
            sf = shp.Reader(str(shapefile_resolved))
            fields = [field[0] for field in sf.fields[1:]]

            # Find the state abbreviation field
            # TIGER shapefiles use STUSPS for 2-letter state codes
            state_field_candidates = ["STUSPS", "STATE_ABBR", "ABBREV", "ST"]
            state_field_idx: Optional[int] = None
            for candidate in state_field_candidates:
                if candidate in fields:
                    state_field_idx = fields.index(candidate)
                    break

            if state_field_idx is None:
                # Fallback: try NAME field and map to abbreviation
                logger.warning(
                    "StateBoundaryDetector: could not find state abbreviation "
                    "field in shapefile. Available fields: %s",
                    fields,
                )
                return

            self._state_polygons = []
            for shape_record in sf.shapeRecords():
                try:
                    geom = shape(shape_record.shape.__geo_interface__)
                    state_abbr = shape_record.record[state_field_idx].strip()
                    if state_abbr and len(state_abbr) == 2:
                        self._state_polygons.append(
                            {"state": state_abbr.upper(), "geometry": geom}
                        )
                except Exception as exc:
                    logger.debug(
                        "StateBoundaryDetector: skipping malformed shape "
                        "record: %s",
                        exc,
                    )
                    continue

            self._shapefile_loaded = True
            logger.info(
                "StateBoundaryDetector: loaded %d state polygons from %s",
                len(self._state_polygons),
                self._shapefile_path,
            )

        except Exception as exc:
            logger.warning(
                "StateBoundaryDetector: failed to load shapefile %s: %s",
                self._shapefile_path,
                exc,
            )

    def get_state(self, lat: float, lon: float) -> Optional[str]:
        """Determine the US state for a given lat/lon coordinate.

        Uses grid-cell caching: if the grid cell containing this coordinate
        has already been resolved, returns the cached result. Otherwise,
        performs a point-in-polygon query against the loaded shapefile data.

        Args:
            lat: Latitude in decimal degrees (WGS84).
            lon: Longitude in decimal degrees (WGS84).

        Returns:
            2-letter US state code (e.g., "TX", "CA") or None if the
            coordinate is not within any US state or the shapefile is
            not available.
        """
        # Quick rejection for points clearly outside the US
        if not _is_within_us_bounds(lat, lon):
            return None

        # Check grid-cell cache
        cell_key = _grid_cell_key(lat, lon)
        if cell_key in self._grid_cache:
            return self._grid_cache[cell_key]

        # Ensure shapefile is loaded
        if not self._shapefile_loaded:
            self._load_shapefile()

        if not self._shapefile_loaded or self._state_polygons is None:
            # Shapefile not available — graceful degradation
            return None

        # Perform point-in-polygon lookup
        state = self._point_in_polygon_lookup(lat, lon)

        # Cache the result for this grid cell
        self._grid_cache[cell_key] = state

        return state

    def _point_in_polygon_lookup(
        self, lat: float, lon: float
    ) -> Optional[str]:
        """Perform a point-in-polygon query against all state polygons.

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.

        Returns:
            2-letter state code or None if no state contains the point.
        """
        try:
            from shapely.geometry import Point
        except ImportError:
            return None

        point = Point(lon, lat)  # shapely uses (x, y) = (lon, lat)

        for state_entry in self._state_polygons:  # type: ignore[union-attr]
            try:
                if state_entry["geometry"].contains(point):
                    return state_entry["state"]
            except Exception:
                continue

        return None

    def detect_boundary_crossing(
        self,
        points: List[Tuple[float, float]],
        timestamps: Optional[List[datetime]] = None,
    ) -> List[BoundaryCrossing]:
        """Detect state boundary crossings in a sequence of GPS points.

        Iterates through consecutive GPS readings and identifies where the
        resolved state changes, indicating a state boundary crossing.

        Args:
            points: List of (lat, lon) tuples representing GPS readings
                in chronological order.
            timestamps: Optional list of timestamps corresponding to each
                GPS point. If provided, the crossing timestamp is set to
                the timestamp of the point where the new state was detected.

        Returns:
            List of BoundaryCrossing instances for each detected crossing.
            Empty list if no crossings are detected or if fewer than 2
            points are provided.
        """
        if len(points) < 2:
            return []

        crossings: List[BoundaryCrossing] = []
        prev_state: Optional[str] = None

        for idx, (lat, lon) in enumerate(points):
            current_state = self.get_state(lat, lon)

            # Skip points where state cannot be determined
            if current_state is None:
                continue

            # Detect crossing: previous state was known and differs
            if prev_state is not None and current_state != prev_state:
                timestamp = (
                    timestamps[idx] if timestamps and idx < len(timestamps) else None
                )
                crossings.append(
                    BoundaryCrossing(
                        from_state=prev_state,
                        to_state=current_state,
                        lat=lat,
                        lon=lon,
                        timestamp=timestamp,
                    )
                )

            prev_state = current_state

        return crossings

    def clear_cache(self) -> None:
        """Clear the grid-cell cache.

        Useful for testing or when the shapefile data is updated.
        """
        self._grid_cache.clear()

    @property
    def cache_size(self) -> int:
        """Return the number of cached grid cells."""
        return len(self._grid_cache)

    @property
    def is_shapefile_loaded(self) -> bool:
        """Return whether the shapefile has been successfully loaded."""
        return self._shapefile_loaded
