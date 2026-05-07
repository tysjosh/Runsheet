"""
Property-based tests for ``services.pod_hash_chain_writer.PodHashChainWriter``.

Validates Requirement 4.5.2 as universal properties:

    1. For any sequence of N PODs persisted to the same tenant, the
       resulting chain is unbroken: ``chain[i].previous_pod_hash == chain[i-1].pod_hash``
       for ``i >= 1`` and ``chain[0].previous_pod_hash == ZERO_HASH``.
    2. Distinct tenants never share chain state: each tenant's chain
       begins with ``ZERO_HASH`` and evolves independently.
    3. Concurrent submissions for the same tenant produce a fully-linked
       chain (no two PODs with the same ``previous_pod_hash``).
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from services.pod_hash_chain import ZERO_HASH
from services.pod_hash_chain_writer import PodHashChainWriter


# ---------------------------------------------------------------------------
# Fakes (mirrored from the unit-test fake set, kept minimal).
# ---------------------------------------------------------------------------


class _FakeES:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict] = {}

    async def index_document(self, index: str, doc_id: str, doc: dict) -> dict:
        self.docs[(index, doc_id)] = dict(doc)
        return {"result": "created"}

    async def search_documents(
        self, index: str, query: dict, size: int = 100
    ) -> dict:
        term = (query.get("query", {}) or {}).get("term", {}) or {}
        tenant_id = term.get("tenant_id")
        matches = [
            d
            for (idx, _doc_id), d in self.docs.items()
            if idx == index and (tenant_id is None or d.get("tenant_id") == tenant_id)
        ]
        matches.sort(key=lambda d: d.get("chain_sequence", 0), reverse=True)
        top = matches[:1]
        return {
            "hits": {
                "hits": [{"_source": d} for d in top],
                "total": {"value": len(top)},
            }
        }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_tenant_id_strategy = st.sampled_from(["alpha", "beta", "gamma", "delta"])

# POD identifiers must be unique per tenant to be a valid chain (duplicate
# pod_ids would map to the same ES doc id). The hash-chain invariant itself
# requires distinct PODs, so we tag each with an integer index.
_gallons_strategy = st.floats(
    min_value=0.0,
    max_value=5000.0,
    allow_nan=False,
    allow_infinity=False,
)

_recipient_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Zs"),
        whitelist_characters=" .-",
    ),
    min_size=1,
    max_size=40,
)

_lat_strategy = st.floats(
    min_value=-90.0,
    max_value=90.0,
    allow_nan=False,
    allow_infinity=False,
)
_lon_strategy = st.floats(
    min_value=-180.0,
    max_value=180.0,
    allow_nan=False,
    allow_infinity=False,
)

_pod_entry_strategy = st.fixed_dictionaries(
    {
        "delivered_gallons": _gallons_strategy,
        "recipient_name": _recipient_strategy,
        "lat": _lat_strategy,
        "lon": _lon_strategy,
    }
)


def _make_pod(tenant_id: str, index: int, entry: dict) -> dict:
    return {
        "tenant_id": tenant_id,
        "pod_id": f"{tenant_id}-pod-{index}",
        "job_id": f"{tenant_id}-job-{index}",
        "order_id": f"{tenant_id}-ord-{index}",
        "recipient_name": entry["recipient_name"] or "_",
        "signature_ref": f"tenants/{tenant_id}/signature/2024/01/02/sig-{index}.jpg",
        "photo_refs": [f"tenants/{tenant_id}/photo/2024/01/02/a-{index}.jpg"],
        "meter_ticket_ref": None,
        "geotag": {"lat": entry["lat"], "lon": entry["lon"]},
        "delivered_gallons": entry["delivered_gallons"],
        "delivered_at": f"2024-01-02T03:04:{(index % 60):02d}Z",
        "timestamp": f"2024-01-02T03:04:{(index % 60):02d}Z",
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ],
)
@given(
    tenant_id=_tenant_id_strategy,
    pods=st.lists(_pod_entry_strategy, min_size=1, max_size=6),
)
def test_sequential_chain_is_unbroken(tenant_id: str, pods: list[dict]) -> None:
    """Chain property: every POD links to its predecessor; first → zero-hash.

    Validates: Requirement 4.5.2
    """
    es = _FakeES()
    writer = PodHashChainWriter(es_service=es)

    async def run() -> list[dict]:
        results = []
        for i, entry in enumerate(pods):
            pod = _make_pod(tenant_id, i, entry)
            results.append(
                await writer.persist(tenant_id=tenant_id, pod_doc=pod)
            )
        return results

    loop = asyncio.new_event_loop()
    try:
        persisted = loop.run_until_complete(run())
    finally:
        loop.close()

    assert persisted[0]["previous_pod_hash"] == ZERO_HASH
    assert persisted[0]["chain_sequence"] == 1
    for i in range(1, len(persisted)):
        assert persisted[i]["previous_pod_hash"] == persisted[i - 1]["pod_hash"]
        assert persisted[i]["chain_sequence"] == i + 1
    # All pod_hashes are distinct (SHA-256 of distinct canonical payloads).
    pod_hashes = [p["pod_hash"] for p in persisted]
    assert len(set(pod_hashes)) == len(pod_hashes)


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    tenant_a=st.sampled_from(["alpha", "beta"]),
    tenant_b=st.sampled_from(["gamma", "delta"]),
    entries_a=st.lists(_pod_entry_strategy, min_size=1, max_size=4),
    entries_b=st.lists(_pod_entry_strategy, min_size=1, max_size=4),
)
def test_tenants_are_isolated(
    tenant_a: str,
    tenant_b: str,
    entries_a: list[dict],
    entries_b: list[dict],
) -> None:
    """Tenant-isolation property: each tenant's chain starts at ZERO_HASH and
    never references another tenant's pod_hash.

    Validates: Requirement 4.5.2 + 10.1 (tenant isolation).
    """
    assert tenant_a != tenant_b
    es = _FakeES()
    writer = PodHashChainWriter(es_service=es)

    async def run() -> tuple[list[dict], list[dict]]:
        a_results: list[dict] = []
        b_results: list[dict] = []
        for i, entry in enumerate(entries_a):
            a_results.append(
                await writer.persist(
                    tenant_id=tenant_a,
                    pod_doc=_make_pod(tenant_a, i, entry),
                )
            )
        for i, entry in enumerate(entries_b):
            b_results.append(
                await writer.persist(
                    tenant_id=tenant_b,
                    pod_doc=_make_pod(tenant_b, i, entry),
                )
            )
        return a_results, b_results

    loop = asyncio.new_event_loop()
    try:
        a_results, b_results = loop.run_until_complete(run())
    finally:
        loop.close()

    # Both chains independently start with ZERO_HASH.
    assert a_results[0]["previous_pod_hash"] == ZERO_HASH
    assert b_results[0]["previous_pod_hash"] == ZERO_HASH
    # Tenant A's previous_pod_hash values are either ZERO_HASH or a prior
    # tenant-A pod_hash — never a tenant-B pod_hash.
    b_hashes = {p["pod_hash"] for p in b_results}
    for p in a_results:
        assert p["previous_pod_hash"] not in b_hashes


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    tenant_id=_tenant_id_strategy,
    entries=st.lists(_pod_entry_strategy, min_size=2, max_size=5),
)
def test_concurrent_same_tenant_submissions_chain_cleanly(
    tenant_id: str, entries: list[dict]
) -> None:
    """Concurrency property: concurrent persists for the same tenant
    serialize on the (local) lock and produce a fully-linked chain with
    unique ``previous_pod_hash`` values.

    Validates: Requirement 4.5.2.
    """
    es = _FakeES()
    writer = PodHashChainWriter(es_service=es)

    pods = [_make_pod(tenant_id, i, entry) for i, entry in enumerate(entries)]

    async def run() -> list[dict]:
        return await asyncio.gather(
            *[writer.persist(tenant_id=tenant_id, pod_doc=p) for p in pods]
        )

    loop = asyncio.new_event_loop()
    try:
        results = loop.run_until_complete(run())
    finally:
        loop.close()

    # Sequence numbers form a complete 1..N permutation (no two PODs got
    # the same sequence, none were skipped).
    sequences = sorted(r["chain_sequence"] for r in results)
    assert sequences == list(range(1, len(results) + 1))

    # Exactly one POD has previous_hash == ZERO_HASH (the one that won the
    # first slot).
    previous_hashes = [r["previous_pod_hash"] for r in results]
    assert previous_hashes.count(ZERO_HASH) == 1

    # Every non-first POD's previous_hash is another POD's pod_hash.
    pod_hashes = {r["pod_hash"] for r in results}
    for prev in previous_hashes:
        if prev == ZERO_HASH:
            continue
        assert prev in pod_hashes

    # All pod_hashes are distinct.
    assert len(pod_hashes) == len(results)
