"""
POD Hash Chain — canonical JSON serialization and SHA-256 hashing helpers.

Implements the tamper-evident POD record chain specified by Capability 4 of the
fuel-ops-hardening spec. Each POD record carries a ``previous_pod_hash`` that
references the immediately prior POD's ``pod_hash`` in insertion order (per
tenant). Any retroactive mutation of a POD's canonical fields breaks the chain
starting at the modified record, making tampering detectable during audits.

The canonical form is produced by :func:`canonicalize_pod`, which:

    * Selects a fixed set of fields (tenant_id, pod_id, order_id,
      delivered_gallons, recipient_name, signature_ref, photo_refs, geotag,
      delivered_at, previous_pod_hash).
    * Uses stable key ordering via ``json.dumps(..., sort_keys=True)`` so that
      attribute insertion order never influences the hash.
    * Normalizes ``delivered_at`` to ISO 8601 with a terminal ``Z`` for UTC
      (e.g. ``2024-01-02T03:04:05Z``).
    * Sorts ``photo_refs`` so reordering client-side has no effect on the hash.
    * Rounds floating-point values — ``delivered_gallons`` to 3 decimal places
      and each ``geotag`` coordinate to 7 decimal places — so that lossless
      JSON round-trips do not drift.
    * Emits compact separators (``","`` and ``":"``) so cosmetic whitespace
      cannot affect the bytes hashed.

:func:`compute_pod_hash` applies SHA-256 to the canonical bytes and returns the
lowercase hexadecimal digest.

Both helpers accept either a Pydantic model or a plain ``dict`` so that the
POD persistence path (Task 8.10) and the verification endpoint (Task 8.11) can
hash records regardless of representation.

Validates: Requirement 4.5.1 (canonical JSON + SHA-256 POD_Hash).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: SHA-256 zero-hash used as ``previous_pod_hash`` for the first POD in a
#: tenant's chain (64 ``"0"`` hex characters).
ZERO_HASH: str = "0" * 64

#: Rounding precision for ``delivered_gallons`` (matches ES mapping scale).
_GALLONS_DECIMALS = 3

#: Rounding precision for geotag lat/lon (≈ 1 cm resolution, matches WGS 84
#: driver-app precision).
_GEOTAG_DECIMALS = 7

#: Canonical POD fields (ordering here is informational — ``sort_keys`` on the
#: JSON encoder guarantees actual byte-level order).
CANONICAL_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "pod_id",
    "order_id",
    "delivered_gallons",
    "recipient_name",
    "signature_ref",
    "photo_refs",
    "geotag",
    "delivered_at",
    "previous_pod_hash",
)


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def _get(pod: Any, key: str, default: Any = None) -> Any:
    """Return ``pod[key]`` for mappings, ``getattr(pod, key)`` for models."""
    if isinstance(pod, Mapping):
        return pod.get(key, default)
    return getattr(pod, key, default)


def _normalize_delivered_at(value: Any) -> str:
    """Render ``delivered_at`` as an ISO 8601 string with a terminal ``Z``.

    Accepts either a ``datetime`` (naive or timezone-aware) or a string already
    in ISO 8601 form. Naive datetimes are treated as UTC. A trailing
    ``+00:00`` offset is replaced with ``Z`` to produce a single canonical
    representation for UTC timestamps.
    """
    if value is None:
        raise ValueError("delivered_at is required for POD canonicalization")

    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        iso = dt.isoformat()
    elif isinstance(value, str):
        # Accept both ``...Z`` and ``...+00:00`` inputs.
        raw = value.strip()
        if not raw:
            raise ValueError("delivered_at string is empty")
        # Use fromisoformat for validation + normalization when possible.
        parseable = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(parseable)
        except ValueError:
            # Fall back to returning the string verbatim if we cannot parse it;
            # the canonical form is then whatever the caller supplied (the
            # hash will still be deterministic for identical inputs).
            return raw.replace("+00:00", "Z")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        iso = dt.isoformat()
    else:
        raise TypeError(
            f"delivered_at must be datetime or ISO 8601 string, got {type(value).__name__}"
        )

    # Replace the UTC offset suffix with a single ``Z`` so the canonical form
    # is unambiguous and matches the design doc (Capability 4, canonicalize_pod).
    return iso.replace("+00:00", "Z")


def _normalize_photo_refs(value: Any) -> list[str]:
    """Return a sorted list of string photo refs (empty list when absent)."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise TypeError("photo_refs must be an iterable of strings, not a scalar")
    if not isinstance(value, Iterable):
        raise TypeError(
            f"photo_refs must be iterable, got {type(value).__name__}"
        )
    refs = [str(ref) for ref in value]
    refs.sort()
    return refs


def _normalize_geotag(value: Any) -> Optional[list[float]]:
    """Return ``[lat, lon]`` rounded to 7 decimals, or ``None`` when absent.

    Accepts either a ``[lat, lon]``/``(lat, lon)`` pair or a Pydantic
    ``GeoPoint``-style object with ``lat``/``lng`` (or ``lon``) attributes.
    """
    if value is None:
        return None

    if isinstance(value, Mapping):
        lat = value.get("lat")
        lon = value.get("lng", value.get("lon"))
    elif isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ValueError("geotag sequence must have exactly 2 elements")
        lat, lon = value
    else:
        lat = getattr(value, "lat", None)
        lon = getattr(value, "lng", getattr(value, "lon", None))

    if lat is None or lon is None:
        raise ValueError("geotag requires both lat and lon components")

    return [round(float(lat), _GEOTAG_DECIMALS), round(float(lon), _GEOTAG_DECIMALS)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def canonicalize_pod(pod: Any) -> bytes:
    """Return the canonical JSON byte-string for ``pod``.

    The output is stable under:

        * Attribute/key insertion order (``sort_keys=True``).
        * ``photo_refs`` ordering (entries are sorted before hashing).
        * Cosmetic whitespace (compact separators are used).
        * Floating-point representation noise within 3 decimals for gallons
          and 7 decimals for geotag coordinates (values are rounded).
        * ``delivered_at`` timezone representation (normalized to UTC with a
          terminal ``Z``).

    ``pod`` may be a Pydantic ``BaseModel``, a plain ``dict``/``Mapping``, or
    any object exposing the canonical field names as attributes.

    Validates: Requirement 4.5.1.
    """
    delivered_gallons = _get(pod, "delivered_gallons")
    if delivered_gallons is None:
        raise ValueError("delivered_gallons is required for POD canonicalization")

    payload: dict[str, Any] = {
        "tenant_id": str(_get(pod, "tenant_id", "")),
        "pod_id": str(_get(pod, "pod_id", "")),
        "order_id": str(_get(pod, "order_id", "")),
        "delivered_gallons": round(float(delivered_gallons), _GALLONS_DECIMALS),
        "recipient_name": _get(pod, "recipient_name") or "",
        "signature_ref": _get(pod, "signature_ref") or "",
        "photo_refs": _normalize_photo_refs(_get(pod, "photo_refs")),
        "geotag": _normalize_geotag(_get(pod, "geotag")),
        "delivered_at": _normalize_delivered_at(_get(pod, "delivered_at")),
        "previous_pod_hash": str(_get(pod, "previous_pod_hash") or ZERO_HASH),
    }

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_pod_hash(pod: Any) -> str:
    """Return the SHA-256 hex digest of :func:`canonicalize_pod`.

    Validates: Requirement 4.5.1.
    """
    return hashlib.sha256(canonicalize_pod(pod)).hexdigest()


__all__ = [
    "ZERO_HASH",
    "CANONICAL_FIELDS",
    "canonicalize_pod",
    "compute_pod_hash",
]
