"""
Unit tests for the ``walk_chain`` helper used by the
``fuel_ops_hardening_002_pod_hash_chain`` migration script (Task 12.5,
Requirement 4.5.2).

Covers:
    * Empty input yields no steps.
    * A single un-hashed POD is chained from the zero-hash and a valid
      SHA-256 digest is produced.
    * A sequence of un-hashed PODs threads the chain so each POD's
      ``previous_pod_hash`` equals the prior POD's computed ``pod_hash``.
    * An already-hashed POD whose ``previous_pod_hash`` matches the
      running chain head is tagged ``"verified"`` and the head advances
      to its stored ``pod_hash``.
    * A mix of already-hashed and un-hashed PODs correctly chains the
      un-hashed ones from whatever hash the prior (already-hashed) POD
      stored.
    * A stored ``previous_pod_hash`` that diverges from the running head
      is reported as a ``"mismatch"`` but the existing stored ``pod_hash``
      is not rewritten.
    * Chain sequence numbers are 1-indexed and monotonic.
    * Re-running the walker over a fully-backfilled chain is a no-op
      (idempotency requirement for the migration).
"""
from __future__ import annotations

import hashlib
import re

import pytest

from scripts.migrations.fuel_ops_hardening_002_pod_hash_chain import (
    ChainStep,
    walk_chain,
)
from services.pod_hash_chain import ZERO_HASH, compute_pod_hash


SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _pod(pod_id: str, *, gallons: float = 100.0, **overrides):
    """Build a minimal POD document suitable for hashing."""
    doc = {
        "pod_id": pod_id,
        "tenant_id": "tenant-A",
        "order_id": f"order-{pod_id}",
        "delivered_gallons": gallons,
        "recipient_name": "Test Recipient",
        "signature_ref": f"tenants/tenant-A/signature/2024/01/01/{pod_id}.png",
        "photo_refs": [],
        "geotag": {"lat": 40.7128, "lon": -74.0060},
        "timestamp": "2024-01-01T10:00:00Z",
    }
    doc.update(overrides)
    return doc


class TestWalkChain:
    def test_empty_input_yields_no_steps(self) -> None:
        assert walk_chain([]) == []

    def test_single_unhashed_pod_chains_from_zero_hash(self) -> None:
        pod = _pod("pod-1")
        steps = walk_chain([pod])
        assert len(steps) == 1
        step = steps[0]
        assert step.action == "backfill"
        assert step.pod_id == "pod-1"
        assert step.previous_pod_hash == ZERO_HASH
        assert SHA256_HEX.match(step.pod_hash)
        assert step.chain_sequence == 1

    def test_hash_matches_compute_pod_hash_with_chained_previous(self) -> None:
        pod = _pod("pod-1")
        steps = walk_chain([pod])
        # Independently recompute the expected hash using the same
        # hashing_view the walker would have built.
        hashing_view = dict(pod)
        hashing_view["previous_pod_hash"] = ZERO_HASH
        hashing_view.setdefault("delivered_at", hashing_view.get("timestamp"))
        assert steps[0].pod_hash == compute_pod_hash(hashing_view)

    def test_multiple_unhashed_pods_thread_previous_hashes(self) -> None:
        pods = [_pod(f"pod-{i}", gallons=100.0 + i) for i in range(1, 4)]
        steps = walk_chain(pods)
        assert [s.action for s in steps] == ["backfill", "backfill", "backfill"]
        # Each POD's previous_pod_hash == prior POD's computed pod_hash.
        assert steps[0].previous_pod_hash == ZERO_HASH
        assert steps[1].previous_pod_hash == steps[0].pod_hash
        assert steps[2].previous_pod_hash == steps[1].pod_hash
        # Sequences are 1-indexed and monotonic.
        assert [s.chain_sequence for s in steps] == [1, 2, 3]
        # All computed hashes are distinct SHA-256 digests.
        assert len({s.pod_hash for s in steps}) == 3
        assert all(SHA256_HEX.match(s.pod_hash) for s in steps)

    def test_already_hashed_pod_is_verified_when_chain_matches(self) -> None:
        # Build a POD that already carries the hashes the walker would
        # have computed so the "verified" branch fires.
        pod1 = _pod("pod-1")
        hashing_view = dict(pod1)
        hashing_view["previous_pod_hash"] = ZERO_HASH
        hashing_view.setdefault("delivered_at", hashing_view.get("timestamp"))
        pod1_hash = compute_pod_hash(hashing_view)
        pod1_stored = dict(
            pod1,
            previous_pod_hash=ZERO_HASH,
            pod_hash=pod1_hash,
        )
        steps = walk_chain([pod1_stored])
        assert len(steps) == 1
        assert steps[0].action == "verified"
        assert steps[0].previous_pod_hash == ZERO_HASH
        assert steps[0].pod_hash == pod1_hash

    def test_mix_of_hashed_then_unhashed_threads_from_stored_hash(self) -> None:
        # First POD already carries a stored pod_hash; second POD is
        # missing it and must chain from the first's stored hash.
        stored_first_hash = hashlib.sha256(b"seed-hash-1").hexdigest()
        pod1 = dict(
            _pod("pod-1"),
            previous_pod_hash=ZERO_HASH,
            pod_hash=stored_first_hash,
        )
        pod2 = _pod("pod-2", gallons=150.0)
        steps = walk_chain([pod1, pod2])
        assert [s.action for s in steps] == ["verified", "backfill"]
        assert steps[1].previous_pod_hash == stored_first_hash
        # Sanity-check: recompute the expected hash for pod2.
        hashing_view = dict(pod2)
        hashing_view["previous_pod_hash"] = stored_first_hash
        hashing_view.setdefault("delivered_at", hashing_view.get("timestamp"))
        assert steps[1].pod_hash == compute_pod_hash(hashing_view)

    def test_mismatched_previous_hash_is_reported_not_rewritten(self) -> None:
        # Stored pod_hash is valid but previous_pod_hash points at a
        # different ancestor than the running chain head (ZERO_HASH for
        # the first POD). The walker must flag this as a mismatch and
        # preserve the stored hash.
        bogus_prev = "f" * 64
        stored_hash = hashlib.sha256(b"whatever").hexdigest()
        pod = dict(
            _pod("pod-1"),
            previous_pod_hash=bogus_prev,
            pod_hash=stored_hash,
        )
        steps = walk_chain([pod])
        assert len(steps) == 1
        assert steps[0].action == "mismatch"
        assert steps[0].previous_pod_hash == bogus_prev
        assert steps[0].pod_hash == stored_hash
        assert steps[0].expected_previous_pod_hash == ZERO_HASH

    def test_rerun_over_backfilled_chain_is_idempotent(self) -> None:
        # Walk once to produce hashes, then synthesize a "backfilled"
        # input by copying each step's hashes onto its POD and re-walking.
        pods = [_pod(f"pod-{i}") for i in range(1, 4)]
        first_pass = walk_chain(pods)
        backfilled = [
            dict(
                pod,
                previous_pod_hash=step.previous_pod_hash,
                pod_hash=step.pod_hash,
            )
            for pod, step in zip(pods, first_pass)
        ]
        second_pass = walk_chain(backfilled)
        assert [s.action for s in second_pass] == ["verified", "verified", "verified"]
        # Second pass observes the same hashes it verified.
        assert [s.pod_hash for s in second_pass] == [s.pod_hash for s in first_pass]

    def test_returns_chainstep_instances(self) -> None:
        pod = _pod("pod-1")
        steps = walk_chain([pod])
        assert all(isinstance(s, ChainStep) for s in steps)
