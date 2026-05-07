"""
Unit tests for ``services.pod_hash_chain``.

Validates Requirement 4.5.1:
    * Canonical JSON serialization uses stable key ordering.
    * ``delivered_at`` is emitted as an ISO 8601 string terminated with ``Z``.
    * ``photo_refs`` are sorted before hashing.
    * Floating-point fields (``delivered_gallons``, ``geotag``) are rounded
      before hashing.
    * :func:`compute_pod_hash` returns the SHA-256 of the canonical bytes.

Determinism is exercised across key-order permutations and input-shape
variations (dict vs. attribute-style objects, datetime vs. ISO string,
``photo_refs`` in shuffled orders, etc.).
"""
from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from services.pod_hash_chain import (
    CANONICAL_FIELDS,
    ZERO_HASH,
    canonicalize_pod,
    compute_pod_hash,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _base_pod_dict() -> dict:
    """Return a reference POD payload used across determinism tests."""
    return {
        "tenant_id": "tenant-1",
        "pod_id": "pod-001",
        "order_id": "ord-42",
        "delivered_gallons": 123.4567,
        "recipient_name": "Jane Doe",
        "signature_ref": "tenants/tenant-1/signature/2024/01/02/abc.jpg",
        "photo_refs": [
            "tenants/tenant-1/photo/2024/01/02/b.jpg",
            "tenants/tenant-1/photo/2024/01/02/a.jpg",
            "tenants/tenant-1/photo/2024/01/02/c.jpg",
        ],
        "geotag": [40.12345678, -74.87654321],
        "delivered_at": datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "previous_pod_hash": "a" * 64,
    }


# ---------------------------------------------------------------------------
# Canonicalization structural tests
# ---------------------------------------------------------------------------


class TestCanonicalizePodShape:
    """Requirement 4.5.1 — canonical JSON structure."""

    def test_returns_bytes_with_sorted_keys(self) -> None:
        canonical = canonicalize_pod(_base_pod_dict())

        assert isinstance(canonical, bytes)

        parsed = json.loads(canonical)
        # Python 3.7+ preserves insertion order; json.loads mirrors the on-wire
        # key order, and the canonical form uses ``sort_keys=True``.
        assert list(parsed.keys()) == sorted(CANONICAL_FIELDS)

    def test_delivered_at_is_iso_z_terminated(self) -> None:
        canonical = canonicalize_pod(_base_pod_dict())
        parsed = json.loads(canonical)

        assert parsed["delivered_at"].endswith("Z")
        assert "+00:00" not in parsed["delivered_at"]
        assert parsed["delivered_at"] == "2024-01-02T03:04:05Z"

    def test_photo_refs_are_sorted(self) -> None:
        canonical = canonicalize_pod(_base_pod_dict())
        parsed = json.loads(canonical)

        assert parsed["photo_refs"] == sorted(_base_pod_dict()["photo_refs"])

    def test_delivered_gallons_rounded_to_three_decimals(self) -> None:
        pod = _base_pod_dict()
        pod["delivered_gallons"] = 123.4567891  # extra precision

        parsed = json.loads(canonicalize_pod(pod))

        assert parsed["delivered_gallons"] == 123.457

    def test_geotag_rounded_to_seven_decimals(self) -> None:
        pod = _base_pod_dict()
        pod["geotag"] = [40.123456789, -74.987654321]

        parsed = json.loads(canonicalize_pod(pod))

        assert parsed["geotag"] == [40.1234568, -74.9876543]

    def test_compact_separators_no_whitespace(self) -> None:
        canonical = canonicalize_pod(_base_pod_dict())

        assert b", " not in canonical
        assert b": " not in canonical


# ---------------------------------------------------------------------------
# Determinism under key-order permutation (primary acceptance test)
# ---------------------------------------------------------------------------


class TestCanonicalizationDeterminism:
    """Requirement 4.5.1 — deterministic hashing across key orderings."""

    def test_dict_key_order_does_not_change_canonical_bytes(self) -> None:
        base = _base_pod_dict()
        base_bytes = canonicalize_pod(base)

        keys = list(base.keys())
        # Sample a handful of permutations (full factorial is 10! = 3.6M).
        permutations = [
            list(reversed(keys)),
            sorted(keys),
            sorted(keys, reverse=True),
            keys[::2] + keys[1::2],
            keys[1::2] + keys[::2],
        ]

        for permuted_keys in permutations:
            reordered = {key: base[key] for key in permuted_keys}
            assert canonicalize_pod(reordered) == base_bytes

    def test_canonical_bytes_stable_across_many_permutations(self) -> None:
        base = _base_pod_dict()
        reference = canonicalize_pod(base)
        keys = list(base.keys())

        # Exhaustively check every permutation of the first five keys, which
        # covers key-order mixing without blowing up the runtime.
        for head in itertools.permutations(keys[:5]):
            tail = [k for k in keys if k not in head]
            reordered = {k: base[k] for k in list(head) + tail}
            assert canonicalize_pod(reordered) == reference

    def test_hash_is_sha256_of_canonical_bytes(self) -> None:
        pod = _base_pod_dict()
        expected = hashlib.sha256(canonicalize_pod(pod)).hexdigest()

        assert compute_pod_hash(pod) == expected
        # SHA-256 hex digest is 64 lowercase hex characters.
        assert len(expected) == 64
        assert all(c in "0123456789abcdef" for c in expected)

    def test_hash_stable_across_key_order_permutations(self) -> None:
        base = _base_pod_dict()
        reference_hash = compute_pod_hash(base)
        keys = list(base.keys())

        for permuted_keys in (list(reversed(keys)), sorted(keys)):
            reordered = {key: base[key] for key in permuted_keys}
            assert compute_pod_hash(reordered) == reference_hash

    def test_hash_stable_across_photo_ref_permutations(self) -> None:
        base = _base_pod_dict()
        reference_hash = compute_pod_hash(base)

        for shuffled in itertools.permutations(base["photo_refs"]):
            variant = dict(base)
            variant["photo_refs"] = list(shuffled)
            assert compute_pod_hash(variant) == reference_hash


# ---------------------------------------------------------------------------
# Input-shape flexibility
# ---------------------------------------------------------------------------


class TestCanonicalizationInputShapes:
    """Requirement 4.5.1 — accept Pydantic models, dicts, and attribute objects."""

    def test_dict_and_object_inputs_hash_identically(self) -> None:
        as_dict = _base_pod_dict()
        as_object = SimpleNamespace(**as_dict)

        assert compute_pod_hash(as_dict) == compute_pod_hash(as_object)

    def test_datetime_and_iso_string_hash_identically(self) -> None:
        as_datetime = _base_pod_dict()

        as_iso_z = dict(as_datetime)
        as_iso_z["delivered_at"] = "2024-01-02T03:04:05Z"

        as_iso_offset = dict(as_datetime)
        as_iso_offset["delivered_at"] = "2024-01-02T03:04:05+00:00"

        ref = compute_pod_hash(as_datetime)
        assert compute_pod_hash(as_iso_z) == ref
        assert compute_pod_hash(as_iso_offset) == ref

    def test_naive_datetime_treated_as_utc(self) -> None:
        aware = _base_pod_dict()
        naive = dict(aware)
        naive["delivered_at"] = datetime(2024, 1, 2, 3, 4, 5)  # no tzinfo

        assert compute_pod_hash(aware) == compute_pod_hash(naive)

    def test_geotag_accepts_mapping_and_attribute_forms(self) -> None:
        base = _base_pod_dict()
        reference = compute_pod_hash(base)

        as_mapping = dict(base)
        as_mapping["geotag"] = {"lat": 40.12345678, "lng": -74.87654321}

        as_object = dict(base)
        as_object["geotag"] = SimpleNamespace(lat=40.12345678, lng=-74.87654321)

        assert compute_pod_hash(as_mapping) == reference
        assert compute_pod_hash(as_object) == reference

    def test_missing_optional_fields_default_to_empty(self) -> None:
        pod = {
            "tenant_id": "t",
            "pod_id": "p",
            "order_id": "o",
            "delivered_gallons": 10.0,
            # recipient_name, signature_ref, photo_refs omitted entirely
            "geotag": [1.0, 2.0],
            "delivered_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            # previous_pod_hash omitted — should default to ZERO_HASH
        }

        parsed = json.loads(canonicalize_pod(pod))

        assert parsed["recipient_name"] == ""
        assert parsed["signature_ref"] == ""
        assert parsed["photo_refs"] == []
        assert parsed["previous_pod_hash"] == ZERO_HASH


# ---------------------------------------------------------------------------
# Tamper sensitivity (sanity for Task 8.11 hash-chain verification)
# ---------------------------------------------------------------------------


class TestCanonicalizationTamperSensitivity:
    """Requirement 4.5.1 — mutating any canonical field changes the hash."""

    @pytest.mark.parametrize(
        "field,mutation",
        [
            ("tenant_id", "tenant-other"),
            ("pod_id", "pod-999"),
            ("order_id", "ord-99"),
            ("delivered_gallons", 123.458),  # > rounding threshold
            ("recipient_name", "Someone Else"),
            ("signature_ref", "tenants/tenant-1/signature/2024/01/02/other.jpg"),
            (
                "photo_refs",
                [
                    "tenants/tenant-1/photo/2024/01/02/a.jpg",
                    "tenants/tenant-1/photo/2024/01/02/b.jpg",
                ],
            ),
            ("geotag", [41.0, -74.87654321]),
            (
                "delivered_at",
                datetime(2024, 1, 2, 3, 4, 6, tzinfo=timezone.utc),  # +1 second
            ),
            ("previous_pod_hash", "b" * 64),
        ],
    )
    def test_any_field_mutation_changes_hash(
        self, field: str, mutation: object
    ) -> None:
        base = _base_pod_dict()
        base_hash = compute_pod_hash(base)

        mutated = dict(base)
        mutated[field] = mutation

        assert compute_pod_hash(mutated) != base_hash

    def test_rounding_noise_within_tolerance_does_not_change_hash(self) -> None:
        base = _base_pod_dict()
        base_hash = compute_pod_hash(base)

        noisy = dict(base)
        # Sub-rounding-threshold noise: 123.4567 rounds to 123.457 regardless.
        noisy["delivered_gallons"] = 123.45674
        assert compute_pod_hash(noisy) == base_hash


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_zero_hash_is_64_zero_chars(self) -> None:
        assert ZERO_HASH == "0" * 64
        assert len(ZERO_HASH) == 64
