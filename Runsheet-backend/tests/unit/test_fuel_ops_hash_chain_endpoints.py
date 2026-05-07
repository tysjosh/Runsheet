"""
Unit + property-based tests for the POD hash-chain endpoints.

Exercises:

* ``GET /api/fuel/pod/{pod_id}/hash-proof`` (Req 4.5.3) — returns the
  stored ``pod_hash`` + ``previous_pod_hash`` + canonical payload.
* ``POST /api/fuel/pod/hash-chain/verify`` (Req 4.5.4, 4.5.5) — walks
  a range of pod_ids, re-computes hashes, and reports the first
  mismatch.

Tenant isolation, 404 semantics, and the cross-tenant guardrails are
verified alongside the happy paths.

Validates: Requirements 4.5.3, 4.5.4, 4.5.5.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    router,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.pod_hash_chain import (
    ZERO_HASH,
    canonicalize_pod,
    compute_pod_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-1"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["dispatcher"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_pod(
    *,
    pod_id: str,
    tenant_id: str = "tenant-1",
    order_id: str = "order-1",
    delivered_gallons: float = 123.456,
    recipient_name: str = "Jane Doe",
    signature_ref: str = "tenants/tenant-1/signature/sig.png",
    photo_refs: Optional[list[str]] = None,
    geotag: Optional[Dict[str, float]] = None,
    delivered_at: str = "2024-01-02T03:04:05Z",
    previous_pod_hash: str = ZERO_HASH,
    compute_real_hash: bool = True,
    override_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a POD doc as stored by the hash-chain-aware writer (Task 8.10)."""

    photo_refs = photo_refs or ["tenants/tenant-1/photo/p1.jpg"]
    geotag = geotag or {"lat": 40.12345678, "lon": -74.87654321}

    hash_fields = {
        "tenant_id": tenant_id,
        "pod_id": pod_id,
        "order_id": order_id,
        "delivered_gallons": delivered_gallons,
        "recipient_name": recipient_name,
        "signature_ref": signature_ref,
        "photo_refs": photo_refs,
        "geotag": geotag,
        "delivered_at": delivered_at,
        "previous_pod_hash": previous_pod_hash,
    }
    if override_hash is not None:
        pod_hash = override_hash
    elif compute_real_hash:
        pod_hash = compute_pod_hash(hash_fields)
    else:
        pod_hash = "0" * 64

    doc = {
        "pod_id": pod_id,
        "tenant_id": tenant_id,
        "job_id": order_id,
        "order_id": order_id,
        "delivered_gallons": delivered_gallons,
        "recipient_name": recipient_name,
        "signature_ref": signature_ref,
        "photo_refs": photo_refs,
        "geotag": geotag,
        "timestamp": delivered_at,
        "delivered_at": delivered_at,
        "pod_hash": pod_hash,
        "previous_pod_hash": previous_pod_hash,
        "status": "submitted",
    }
    return doc


def _make_es_service(pod_docs: List[Dict[str, Any]]):
    """Return a MagicMock ES service that serves the supplied POD docs.

    The stub understands two access patterns used by the hash-chain
    endpoints:

    * ``search_documents`` with a ``bool.must`` carrying a ``term`` clause
      on ``pod_id`` → return the matching doc filtered by ``tenant_id``.
    * ``search_documents`` with a ``range`` clause on ``timestamp`` →
      return all docs for the tenant whose timestamp sits inside the
      range, ordered ascending.
    """

    def _filter(query: Dict[str, Any]) -> List[Dict[str, Any]]:
        clauses = query.get("query", {}).get("bool", {}).get("must", [])
        tenant_id = None
        pod_id = None
        ts_range = None
        for clause in clauses:
            if "term" in clause:
                term = clause["term"]
                if "tenant_id" in term:
                    tenant_id = term["tenant_id"]
                if "pod_id" in term:
                    pod_id = term["pod_id"]
            if "range" in clause:
                ts_range = clause["range"].get("timestamp", {})

        filtered = [d for d in pod_docs if tenant_id is None or d["tenant_id"] == tenant_id]
        if pod_id is not None:
            filtered = [d for d in filtered if d["pod_id"] == pod_id]
        if ts_range:
            gte = ts_range.get("gte")
            lte = ts_range.get("lte")
            if gte:
                filtered = [d for d in filtered if d["timestamp"] >= gte]
            if lte:
                filtered = [d for d in filtered if d["timestamp"] <= lte]
            filtered = sorted(filtered, key=lambda d: d["timestamp"])
        return filtered

    async def _search(index: str, query: Dict[str, Any], size: int):
        if index != "proof_of_delivery":
            return {"hits": {"hits": [], "total": {"value": 0}}}
        matches = _filter(query)[:size]
        return {
            "hits": {
                "hits": [{"_source": d} for d in matches],
                "total": {"value": len(matches)},
            }
        }

    es = MagicMock()
    es.search_documents = AsyncMock(side_effect=_search)
    return es


def _build_app(pod_docs: List[Dict[str, Any]], tenant_id: str = "tenant-1"):
    app = FastAPI()
    app.include_router(router)
    es = _make_es_service(pod_docs)
    configure_fuel_ops_endpoints(es_service=es)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(tenant_id)
    return app, es


# ---------------------------------------------------------------------------
# GET /api/fuel/pod/{pod_id}/hash-proof
# ---------------------------------------------------------------------------


class TestHashProofEndpoint:
    """Req 4.5.3: canonical payload + stored hashes."""

    def test_returns_stored_hash_and_canonical_payload(self):
        pod = _build_pod(pod_id="POD-1")
        app, _ = _build_app([pod])
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/POD-1/hash-proof")

        assert resp.status_code == 200
        data = resp.json()
        assert data["pod_id"] == "POD-1"
        assert data["tenant_id"] == "tenant-1"
        assert data["pod_hash"] == pod["pod_hash"]
        assert data["previous_pod_hash"] == ZERO_HASH
        # The canonical payload must contain every field that feeds the hash.
        payload = data["canonical_payload"]
        assert payload["pod_id"] == "POD-1"
        assert payload["tenant_id"] == "tenant-1"
        assert payload["order_id"] == "order-1"
        # Raw bytes should round-trip to the same hash locally.
        import hashlib
        bytes_raw = data["canonical_payload_bytes"].encode("utf-8")
        assert hashlib.sha256(bytes_raw).hexdigest() == pod["pod_hash"]

    def test_canonical_bytes_match_server_canonicalization(self):
        """Re-serializing canonical_payload yields canonical_payload_bytes."""
        pod = _build_pod(pod_id="POD-1", photo_refs=["z.jpg", "a.jpg"])
        app, _ = _build_app([pod])
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/POD-1/hash-proof")
        data = resp.json()
        payload = data["canonical_payload"]
        # photo_refs in the payload must be sorted (canonicalize_pod sorts them).
        assert payload["photo_refs"] == ["a.jpg", "z.jpg"]

        # Manually re-serialize using the same rules canonicalize_pod uses.
        reserialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        assert reserialized == data["canonical_payload_bytes"]

    def test_missing_pod_returns_404(self):
        app, _ = _build_app([])
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/UNKNOWN/hash-proof")

        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "pod_not_found"

    def test_cross_tenant_pod_is_hidden_as_404(self):
        pod = _build_pod(pod_id="POD-1", tenant_id="other-tenant")
        app, _ = _build_app([pod], tenant_id="tenant-1")
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/POD-1/hash-proof")

        assert resp.status_code == 404

    def test_pod_without_hash_returns_409(self):
        pod = _build_pod(pod_id="POD-1")
        pod.pop("pod_hash")
        app, _ = _build_app([pod])
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/POD-1/hash-proof")

        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "pod_hash_unavailable"


# ---------------------------------------------------------------------------
# POST /api/fuel/pod/hash-chain/verify
# ---------------------------------------------------------------------------


def _build_chain(tenant_id: str = "tenant-1") -> List[Dict[str, Any]]:
    """Build a 3-POD linked chain honoring previous_pod_hash → pod_hash."""

    pod_a = _build_pod(
        pod_id="POD-A",
        tenant_id=tenant_id,
        order_id="ord-a",
        delivered_at="2024-01-01T00:00:00Z",
        previous_pod_hash=ZERO_HASH,
    )
    pod_b = _build_pod(
        pod_id="POD-B",
        tenant_id=tenant_id,
        order_id="ord-b",
        delivered_at="2024-01-01T01:00:00Z",
        previous_pod_hash=pod_a["pod_hash"],
    )
    pod_c = _build_pod(
        pod_id="POD-C",
        tenant_id=tenant_id,
        order_id="ord-c",
        delivered_at="2024-01-01T02:00:00Z",
        previous_pod_hash=pod_b["pod_hash"],
    )
    return [pod_a, pod_b, pod_c]


class TestHashChainVerifyEndpoint:
    """Req 4.5.4: recompute hashes + report first mismatch."""

    def test_intact_chain_passes_verification(self):
        chain = _build_chain()
        app, _ = _build_app(chain)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": ["POD-A", "POD-B", "POD-C"]},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["verified_count"] == 3
        assert data["total_requested"] == 3
        assert data["first_mismatch"] is None
        assert data["pod_ids_checked"] == ["POD-A", "POD-B", "POD-C"]

    def test_mutated_field_is_detected_at_that_pod(self):
        """Req 4.5.5: first mismatch returns the offending pod_id."""
        chain = _build_chain()
        # Mutate the delivered_gallons on POD-B *without* updating pod_hash.
        chain[1]["delivered_gallons"] = 999.999

        app, _ = _build_app(chain)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": ["POD-A", "POD-B", "POD-C"]},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert data["verified_count"] == 1  # POD-A passed
        mismatch = data["first_mismatch"]
        assert mismatch["pod_id"] == "POD-B"
        assert mismatch["reason"] == "stored_hash_mismatch"
        assert mismatch["stored_hash"] == chain[1]["pod_hash"]
        assert mismatch["computed_hash"] != mismatch["stored_hash"]

    def test_broken_chain_linkage_is_detected(self):
        """Mutating only ``previous_pod_hash`` (and re-computing ``pod_hash``
        so the row's own canonical check still passes) surfaces a
        ``previous_hash_mismatch`` at the linkage-breaking POD."""
        chain = _build_chain()
        # Replace POD-B's previous_pod_hash with a bogus value and recompute
        # its pod_hash so the row's own canonical-vs-stored check passes.
        # The broken linkage is then between POD-A (prior) and POD-B
        # (whose stored previous_pod_hash no longer matches POD-A's hash).
        chain[1]["previous_pod_hash"] = "f" * 64
        chain[1]["pod_hash"] = compute_pod_hash(
            {
                "tenant_id": chain[1]["tenant_id"],
                "pod_id": chain[1]["pod_id"],
                "order_id": chain[1]["order_id"],
                "delivered_gallons": chain[1]["delivered_gallons"],
                "recipient_name": chain[1]["recipient_name"],
                "signature_ref": chain[1]["signature_ref"],
                "photo_refs": chain[1]["photo_refs"],
                "geotag": chain[1]["geotag"],
                "delivered_at": chain[1]["delivered_at"],
                "previous_pod_hash": "f" * 64,
            }
        )

        app, _ = _build_app(chain)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": ["POD-A", "POD-B", "POD-C"]},
        )

        data = resp.json()
        assert data["valid"] is False
        # POD-B's stored previous_pod_hash no longer equals POD-A's
        # stored pod_hash → linkage fails at POD-B.
        assert data["first_mismatch"]["pod_id"] == "POD-B"
        assert data["first_mismatch"]["reason"] == "previous_hash_mismatch"
        assert data["verified_count"] == 1  # POD-A passed

    def test_missing_stored_hash_is_reported(self):
        chain = _build_chain()
        chain[1].pop("pod_hash")
        app, _ = _build_app(chain)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": ["POD-A", "POD-B", "POD-C"]},
        )

        data = resp.json()
        assert data["valid"] is False
        assert data["first_mismatch"]["pod_id"] == "POD-B"
        assert data["first_mismatch"]["reason"] == "missing_stored_hash"

    def test_missing_pod_in_walk_is_reported(self):
        chain = _build_chain()
        app, _ = _build_app(chain)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": ["POD-A", "POD-MISSING", "POD-C"]},
        )

        data = resp.json()
        assert data["valid"] is False
        assert data["first_mismatch"]["pod_id"] == "POD-MISSING"
        assert data["first_mismatch"]["reason"] == "pod_not_found"

    def test_rejects_mixing_selectors(self):
        app, _ = _build_app(_build_chain())
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={
                "pod_ids": ["POD-A"],
                "from_pod_id": "POD-A",
                "to_pod_id": "POD-B",
            },
        )

        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "invalid_selector"

    def test_rejects_empty_pod_ids(self):
        app, _ = _build_app(_build_chain())
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": []},
        )

        assert resp.status_code == 400

    def test_rejects_missing_selector(self):
        app, _ = _build_app(_build_chain())
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={},
        )

        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "missing_selector"

    def test_range_mode_walks_inclusive_window(self):
        chain = _build_chain()
        app, _ = _build_app(chain)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={
                "from_pod_id": "POD-A",
                "to_pod_id": "POD-C",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["pod_ids_checked"] == ["POD-A", "POD-B", "POD-C"]

    def test_range_mode_404_when_anchor_missing(self):
        app, _ = _build_app(_build_chain())
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={
                "from_pod_id": "POD-A",
                "to_pod_id": "POD-UNKNOWN",
            },
        )

        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "pod_not_found"

    def test_cross_tenant_pod_ids_are_reported_as_not_found(self):
        """A POD owned by another tenant must be invisible from the verify
        walk — the caller only sees its own chain."""
        mine = _build_pod(
            pod_id="POD-ONE",
            tenant_id="tenant-1",
            delivered_at="2024-01-01T00:00:00Z",
        )
        theirs = _build_pod(
            pod_id="POD-OTHER",
            tenant_id="tenant-2",
            delivered_at="2024-01-01T01:00:00Z",
        )
        app, _ = _build_app([mine, theirs], tenant_id="tenant-1")
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": ["POD-ONE", "POD-OTHER"]},
        )

        data = resp.json()
        assert data["valid"] is False
        assert data["first_mismatch"]["pod_id"] == "POD-OTHER"
        assert data["first_mismatch"]["reason"] == "pod_not_found"


# ---------------------------------------------------------------------------
# Property-based tests (validates: Requirements 4.5.4, 4.5.5)
# ---------------------------------------------------------------------------

pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings, strategies as st


_pod_id_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=16,
).filter(lambda s: s.strip() == s and len(s) >= 1)


@st.composite
def _intact_chain(draw, tenant_id: str = "tenant-1"):
    """Generate an arbitrary but internally consistent POD chain."""

    pod_ids = draw(
        st.lists(_pod_id_strategy, min_size=2, max_size=6, unique=True)
    )
    gallons = draw(
        st.lists(
            st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
            min_size=len(pod_ids),
            max_size=len(pod_ids),
        )
    )
    chain: List[Dict[str, Any]] = []
    prev_hash = ZERO_HASH
    for idx, (pid, g) in enumerate(zip(pod_ids, gallons)):
        delivered_at = f"2024-01-01T{idx:02d}:00:00Z"
        pod = _build_pod(
            pod_id=pid,
            tenant_id=tenant_id,
            order_id=f"ord-{idx}",
            delivered_gallons=round(g, 3),
            delivered_at=delivered_at,
            previous_pod_hash=prev_hash,
        )
        chain.append(pod)
        prev_hash = pod["pod_hash"]
    return chain


class TestHashChainProperty:
    """Property: mutating any canonical field of any POD breaks verification
    at that pod_id (round-trip tamper detection). Validates: Req 4.5.5."""

    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(chain=_intact_chain(), tamper_index=st.integers(min_value=0, max_value=5))
    def test_intact_chain_always_verifies(self, chain, tamper_index):
        """Without tampering, verification always succeeds."""
        app, _ = _build_app(chain)
        client = TestClient(app)
        pod_ids = [p["pod_id"] for p in chain]

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": pod_ids},
        )
        data = resp.json()
        assert data["valid"] is True, data
        assert data["verified_count"] == len(chain)

    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        chain=_intact_chain(),
        field=st.sampled_from(
            ["delivered_gallons", "recipient_name", "order_id", "delivered_at"]
        ),
    )
    def test_any_field_mutation_is_detected_at_mutated_pod(self, chain, field):
        """Mutate one canonical field on one POD (without updating the
        stored pod_hash). Verification must flag that pod_id as the first
        mismatch."""
        # Pick the middle POD when available, otherwise the last.
        target_idx = len(chain) - 1 if len(chain) == 2 else len(chain) // 2
        target = chain[target_idx]

        if field == "delivered_gallons":
            target[field] = float(target[field]) + 17.125
        elif field == "delivered_at":
            target["timestamp"] = "2099-12-31T23:59:59Z"
            target["delivered_at"] = "2099-12-31T23:59:59Z"
        elif field == "order_id":
            target[field] = target[field] + "-tampered"
        else:
            target[field] = str(target.get(field, "")) + "_tampered"

        app, _ = _build_app(chain)
        client = TestClient(app)
        pod_ids = [p["pod_id"] for p in chain]

        resp = client.post(
            "/api/fuel/pod/hash-chain/verify",
            json={"pod_ids": pod_ids},
        )
        data = resp.json()
        assert data["valid"] is False
        assert data["first_mismatch"]["pod_id"] == target["pod_id"]
        # All PODs before the mutated one should have verified cleanly.
        assert data["verified_count"] == target_idx

    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(chain=_intact_chain())
    def test_hash_proof_payload_round_trip(self, chain):
        """The canonical_payload returned by hash-proof, re-serialized using
        the canonical JSON rules, matches canonical_payload_bytes and hashes
        back to the stored pod_hash. Validates: Req 4.5.3."""
        import hashlib

        app, _ = _build_app(chain)
        client = TestClient(app)

        for pod in chain:
            resp = client.get(f"/api/fuel/pod/{pod['pod_id']}/hash-proof")
            assert resp.status_code == 200
            data = resp.json()
            payload = data["canonical_payload"]
            reserialized = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            )
            assert reserialized == data["canonical_payload_bytes"]
            local = hashlib.sha256(reserialized.encode("utf-8")).hexdigest()
            assert local == pod["pod_hash"]
            assert data["pod_hash"] == pod["pod_hash"]
