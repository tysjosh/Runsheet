#!/usr/bin/env python3
"""
Fuel-ops hardening migration 002 — POD hash-chain backfill.

Purpose
-------
Task 12.5 of the ``fuel-ops-hardening`` spec (Requirement 4.5.2). Before
Capability 4 of this spec landed, Proof-of-Delivery records were
persisted without the tamper-evident ``pod_hash`` / ``previous_pod_hash``
fields that chain each POD to its immediate predecessor. Starting with
Task 8.10 every new POD is hashed under a per-tenant Redis lock by
:class:`services.pod_hash_chain_writer.PodHashChainWriter`. This
migration populates the same fields retroactively for every POD that
predates that wiring so the hash-chain verification endpoint
(``POST /api/fuel/pod/hash-chain/verify``, Task 8.11) can walk the full
history end-to-end without gaps.

What the migration does per tenant
----------------------------------

1. **Discover PODs** — scan the ``proof_of_delivery`` index for the
   tenant paginating by ``timestamp`` ASC so PODs are visited in
   insertion order (Requirement 4.5.2, "insertion order"). Ties on
   ``timestamp`` are broken deterministically by ``pod_id`` so two runs
   of the migration see the same ordering.

2. **Walk the chain** — maintain a running ``previous_pod_hash`` that
   starts at :data:`services.pod_hash_chain.ZERO_HASH` and advances to
   the ``pod_hash`` of each processed POD. For each POD:

   - **Already hashed** (``pod_hash`` present): verify that
     ``previous_pod_hash`` matches the running chain head; if it does
     not, log a ``WARNING`` (migration is non-destructive — mismatches
     are surfaced for operator investigation but the stored value is
     **not** recomputed). The running chain head then advances to the
     stored ``pod_hash``.

   - **Missing hash**: compute the canonical ``pod_hash`` using
     :func:`services.pod_hash_chain.compute_pod_hash` with the running
     chain head as ``previous_pod_hash``, then persist both fields
     (plus ``chain_sequence`` = ordinal position) via
     ``es.update_document``.

3. **Idempotent re-runs** — because the first branch above skips writes
   and the second branch only fires when ``pod_hash`` is missing, a
   second run over the same tenant produces zero writes in steady state.

Ordering note
-------------
The ``proof_of_delivery`` mapping carries both ``timestamp`` (the
driver-reported delivery time) and ``persisted_at`` (written only by
the PodHashChainWriter from Task 8.10 onwards). For historical PODs
written before that wiring, ``persisted_at`` is absent; ``timestamp``
is the only consistently-populated ordering key, and it is what the
spec's "insertion order" phrasing refers to in the context of this
backfill. The migration therefore sorts by ``timestamp`` ASC then
``pod_id`` ASC.

Usage
-----

Dry-run (logs the intended writes without touching ES)::

    python -m scripts.migrations.fuel_ops_hardening_002_pod_hash_chain --dry-run

Full run against a specific tenant::

    python -m scripts.migrations.fuel_ops_hardening_002_pod_hash_chain \\
        --tenant-id acme-staging

Full run over every tenant discovered in the ``proof_of_delivery``
index (Task 12.10 invokes this against staging)::

    python -m scripts.migrations.fuel_ops_hardening_002_pod_hash_chain

Exit codes
----------
``0`` on success, ``1`` when any tenant surfaces an error (the script
continues past per-tenant failures and exits non-zero only at the end
so partial progress is still committed).

Validates: Requirement 4.5.2.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone as _dt_timezone
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Tuple

# Ensure the project root is on sys.path so ``services`` / ``driver``
# modules resolve when the script is executed either as
# ``python -m scripts.migrations.fuel_ops_hardening_002_pod_hash_chain``
# or as a standalone ``python scripts/migrations/...``.
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from driver.services.driver_es_mappings import PROOF_OF_DELIVERY_INDEX  # noqa: E402
from services.pod_hash_chain import ZERO_HASH, compute_pod_hash  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fuel_ops_hardening_002")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: ES page size for the POD scan. PODs per tenant are rarely more than
#: low-hundreds-of-thousands even for large fleets, so a 500-doc page
#: keeps the migration memory profile flat without paying an excessive
#: round-trip tax.
POD_PAGE_SIZE = 500


# ---------------------------------------------------------------------------
# Result reporting dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TenantMigrationResult:
    """Per-tenant migration outcome for logging + test assertions."""

    tenant_id: str
    pods_scanned: int = 0
    pods_backfilled: int = 0
    pods_already_hashed: int = 0
    chain_mismatches: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    final_chain_head: str = ZERO_HASH

    def as_log_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "pods_scanned": self.pods_scanned,
            "pods_backfilled": self.pods_backfilled,
            "pods_already_hashed": self.pods_already_hashed,
            "chain_mismatches": list(self.chain_mismatches),
            "errors": list(self.errors),
            "final_chain_head": self.final_chain_head[:12] + "…"
            if self.final_chain_head
            else None,
        }


@dataclass
class MigrationSummary:
    """Aggregate outcome for a full migration run."""

    tenants_processed: int = 0
    pods_backfilled: int = 0
    pods_already_hashed: int = 0
    chain_mismatches: int = 0
    tenants_with_errors: int = 0

    def record(self, result: TenantMigrationResult) -> None:
        self.tenants_processed += 1
        self.pods_backfilled += result.pods_backfilled
        self.pods_already_hashed += result.pods_already_hashed
        self.chain_mismatches += len(result.chain_mismatches)
        if result.errors:
            self.tenants_with_errors += 1


# ---------------------------------------------------------------------------
# Chain-walking helper (pure, unit-testable)
# ---------------------------------------------------------------------------


@dataclass
class ChainStep:
    """One step in a chain walk.

    Attributes
    ----------
    pod_id:
        The POD identifier the step refers to.
    action:
        One of ``"backfill"`` (POD lacked ``pod_hash`` and the migration
        would compute + write it), ``"verified"`` (POD already had a
        ``pod_hash`` and ``previous_pod_hash`` matched the running chain
        head), or ``"mismatch"`` (POD already had a ``pod_hash`` but
        ``previous_pod_hash`` diverged from the running chain head; the
        migration logs a warning and does **not** recompute).
    previous_pod_hash:
        The hash the migration either wrote (``backfill``) or observed
        stored on the POD (``verified`` / ``mismatch``).
    pod_hash:
        The hash of the POD as of this step — newly computed for
        ``backfill`` steps, read from the persisted record for
        ``verified`` / ``mismatch`` steps.
    chain_sequence:
        The 1-indexed position of this POD in the tenant's chain.
    expected_previous_pod_hash:
        Only populated for ``mismatch`` steps: the hash the migration
        would have written had the POD been blank, i.e. the running
        chain head at the time of the step.
    """

    pod_id: str
    action: str  # "backfill" | "verified" | "mismatch"
    previous_pod_hash: str
    pod_hash: str
    chain_sequence: int
    expected_previous_pod_hash: Optional[str] = None


def walk_chain(
    pods: Iterable[Mapping[str, Any]],
    *,
    initial_previous_hash: str = ZERO_HASH,
) -> List[ChainStep]:
    """Walk ``pods`` in the given order and return one :class:`ChainStep` per POD.

    ``pods`` is consumed in insertion order — the caller is responsible
    for sorting (this migration uses ``timestamp`` ASC, ``pod_id`` ASC).

    Each POD is treated as follows:

    - If the POD already has a ``pod_hash`` and its stored
      ``previous_pod_hash`` equals the running chain head, a
      ``"verified"`` step is emitted and the head advances to that POD's
      stored ``pod_hash``.
    - If the POD already has a ``pod_hash`` but its stored
      ``previous_pod_hash`` differs from the running chain head, a
      ``"mismatch"`` step is emitted; the head advances to the stored
      ``pod_hash`` (the migration does not rewrite existing hashes, it
      only reports divergences).
    - If the POD has no ``pod_hash``, a ``"backfill"`` step is emitted
      with a freshly computed hash chained from the running head; the
      head advances to the computed hash.

    This helper is pure (no IO) so it can be exercised end-to-end in a
    unit test without standing up an ES fake.

    Validates: Requirement 4.5.2.
    """
    chain_head = str(initial_previous_hash or ZERO_HASH)
    steps: List[ChainStep] = []
    for sequence, pod in enumerate(pods, start=1):
        pod_id = str(pod.get("pod_id") or "")
        stored_hash = pod.get("pod_hash")
        stored_prev = pod.get("previous_pod_hash")

        if stored_hash:
            stored_hash_str = str(stored_hash)
            stored_prev_str = str(stored_prev or "")
            if stored_prev_str == chain_head:
                steps.append(
                    ChainStep(
                        pod_id=pod_id,
                        action="verified",
                        previous_pod_hash=stored_prev_str,
                        pod_hash=stored_hash_str,
                        chain_sequence=sequence,
                    )
                )
            else:
                steps.append(
                    ChainStep(
                        pod_id=pod_id,
                        action="mismatch",
                        previous_pod_hash=stored_prev_str,
                        pod_hash=stored_hash_str,
                        chain_sequence=sequence,
                        expected_previous_pod_hash=chain_head,
                    )
                )
            chain_head = stored_hash_str
            continue

        # ``previous_pod_hash`` is part of the canonical hashing payload
        # (see services.pod_hash_chain.canonicalize_pod) so we must feed
        # the running chain head into the POD view before hashing.
        hashing_view = dict(pod)
        hashing_view["previous_pod_hash"] = chain_head
        # Normalize ``delivered_at`` — historical PODs stored the value
        # under ``timestamp`` only; canonicalize_pod requires
        # ``delivered_at``.
        if not hashing_view.get("delivered_at"):
            hashing_view["delivered_at"] = hashing_view.get("timestamp")
        if hashing_view.get("delivered_gallons") is None:
            hashing_view["delivered_gallons"] = 0.0
        new_hash = compute_pod_hash(hashing_view)
        steps.append(
            ChainStep(
                pod_id=pod_id,
                action="backfill",
                previous_pod_hash=chain_head,
                pod_hash=new_hash,
                chain_sequence=sequence,
            )
        )
        chain_head = new_hash
    return steps


# ---------------------------------------------------------------------------
# Elasticsearch helpers
# ---------------------------------------------------------------------------


async def _discover_tenant_ids(es_service: Any) -> List[str]:
    """Return every distinct ``tenant_id`` that owns POD documents."""
    try:
        resp = await es_service.search_documents(
            PROOF_OF_DELIVERY_INDEX,
            {
                "size": 0,
                "aggs": {
                    "tenant_ids": {
                        "terms": {"field": "tenant_id", "size": 10_000}
                    }
                },
            },
            0,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "discover_tenant_ids: %s aggregation failed: %s",
            PROOF_OF_DELIVERY_INDEX,
            exc,
        )
        return []

    buckets = (
        (resp or {}).get("aggregations", {}).get("tenant_ids", {}).get("buckets", [])
    )
    tenant_ids: set[str] = set()
    for bucket in buckets:
        raw = bucket.get("key")
        if isinstance(raw, str) and raw.strip():
            tenant_ids.add(raw.strip())
    return sorted(tenant_ids)


async def _load_tenant_pods(
    es_service: Any, tenant_id: str
) -> List[Mapping[str, Any]]:
    """Return every POD for ``tenant_id`` sorted in insertion order.

    Sorts by ``timestamp`` ASC then ``pod_id`` ASC so PODs sharing a
    timestamp are visited deterministically across re-runs (idempotency).
    Pagination uses ``from`` + ``size`` rather than ``scroll`` because
    serverless Elasticsearch does not implement scroll.
    """
    pods: List[Mapping[str, Any]] = []
    offset = 0
    while True:
        query = {
            "query": {"term": {"tenant_id": tenant_id}},
            "from": offset,
            "size": POD_PAGE_SIZE,
            "sort": [
                {"timestamp": {"order": "asc", "missing": "_last"}},
                {"pod_id": {"order": "asc"}},
            ],
        }
        try:
            resp = await es_service.search_documents(
                PROOF_OF_DELIVERY_INDEX, query, POD_PAGE_SIZE
            )
        except Exception as exc:
            logger.warning(
                "load_tenant_pods: tenant=%s page=%d failed: %s",
                tenant_id,
                offset // POD_PAGE_SIZE,
                exc,
            )
            break

        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        if not hits:
            break

        for hit in hits:
            src = hit.get("_source") or {}
            if not isinstance(src, Mapping):
                continue
            pods.append(src)

        if len(hits) < POD_PAGE_SIZE:
            break
        offset += POD_PAGE_SIZE

    return pods


# ---------------------------------------------------------------------------
# Per-tenant migration
# ---------------------------------------------------------------------------


async def _migrate_tenant(
    *,
    tenant_id: str,
    es_service: Any,
    dry_run: bool,
) -> TenantMigrationResult:
    """Backfill ``pod_hash`` / ``previous_pod_hash`` for one tenant."""
    result = TenantMigrationResult(tenant_id=tenant_id)

    pods = await _load_tenant_pods(es_service, tenant_id)
    result.pods_scanned = len(pods)
    if not pods:
        logger.info("tenant=%s has no PODs; nothing to backfill", tenant_id)
        return result

    steps = walk_chain(pods)

    for step in steps:
        if step.action == "verified":
            result.pods_already_hashed += 1
            continue

        if step.action == "mismatch":
            result.pods_already_hashed += 1
            mismatch_msg = (
                f"pod_id={step.pod_id} stored_previous_pod_hash="
                f"{step.previous_pod_hash[:12]}… but chain head was "
                f"{(step.expected_previous_pod_hash or '')[:12]}…"
            )
            logger.warning(
                "tenant=%s chain_mismatch %s (migration is non-destructive; "
                "manual investigation required)",
                tenant_id,
                mismatch_msg,
            )
            result.chain_mismatches.append(mismatch_msg)
            continue

        # step.action == "backfill"
        logger.info(
            "tenant=%s pod_backfill pod_id=%s sequence=%d "
            "previous_pod_hash=%s… pod_hash=%s… dry_run=%s",
            tenant_id,
            step.pod_id,
            step.chain_sequence,
            step.previous_pod_hash[:12],
            step.pod_hash[:12],
            dry_run,
        )
        if dry_run:
            result.pods_backfilled += 1
            continue

        patch = {
            "previous_pod_hash": step.previous_pod_hash,
            "pod_hash": step.pod_hash,
            "chain_sequence": step.chain_sequence,
            "persisted_at": _utcnow_iso(),
        }
        try:
            await es_service.update_document(
                PROOF_OF_DELIVERY_INDEX, step.pod_id, patch
            )
        except Exception as exc:
            msg = f"update_document failed for pod_id={step.pod_id}: {exc}"
            logger.error("tenant=%s %s", tenant_id, msg)
            result.errors.append(msg)
            # Stop processing this tenant on the first write failure so
            # we don't leave a half-migrated chain that surfaces
            # mismatches on the next run.
            return result
        result.pods_backfilled += 1

    if steps:
        result.final_chain_head = steps[-1].pod_hash
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_migration(
    *,
    tenant_id: Optional[str] = None,
    dry_run: bool = False,
    es_service: Optional[Any] = None,
) -> MigrationSummary:
    """Run the POD hash-chain backfill over one or many tenants.

    Args:
        tenant_id: Restrict the run to this tenant when provided.
        dry_run: When ``True``, log intended writes and return
            counters without touching Elasticsearch.
        es_service: Optional
            :class:`services.elasticsearch_service.ElasticsearchService`.
            When ``None``, the production singleton is imported lazily
            so the module stays importable in test / smoke-check
            contexts that do not need ES.

    Returns:
        :class:`MigrationSummary` with aggregate counters across all
        processed tenants.
    """
    if es_service is None:
        from services.elasticsearch_service import elasticsearch_service as _es  # noqa: WPS433
        es_service = _es

    if tenant_id:
        tenant_ids: List[str] = [tenant_id]
    else:
        tenant_ids = await _discover_tenant_ids(es_service)

    if not tenant_ids:
        logger.info("No tenants with PODs discovered; nothing to migrate.")
        return MigrationSummary()

    logger.info(
        "fuel_ops_hardening_002: starting POD hash-chain backfill over %d tenant(s) dry_run=%s",
        len(tenant_ids),
        dry_run,
    )

    summary = MigrationSummary()
    for tid in tenant_ids:
        try:
            result = await _migrate_tenant(
                tenant_id=tid,
                es_service=es_service,
                dry_run=dry_run,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("tenant=%s migration crashed: %s", tid, exc)
            summary.tenants_processed += 1
            summary.tenants_with_errors += 1
            continue

        logger.info("tenant_result=%s", json.dumps(result.as_log_dict(), default=str))
        summary.record(result)

    logger.info(
        "fuel_ops_hardening_002: done tenants=%d pods_backfilled=%d "
        "pods_already_hashed=%d chain_mismatches=%d errors=%d",
        summary.tenants_processed,
        summary.pods_backfilled,
        summary.pods_already_hashed,
        summary.chain_mismatches,
        summary.tenants_with_errors,
    )
    return summary


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(_dt_timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuel_ops_hardening_002_pod_hash_chain",
        description=(
            "Backfill previous_pod_hash / pod_hash / chain_sequence for "
            "every existing POD in insertion order per tenant, starting "
            "from the zero-hash. Idempotent: re-runs are no-ops on "
            "already-hashed records and warn on chain mismatches."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Log every intended write without touching Elasticsearch. "
            "Recommended for the first run in any environment."
        ),
    )
    parser.add_argument(
        "--tenant-id",
        dest="tenant_id",
        default=None,
        help=(
            "Restrict the run to this tenant instead of every tenant "
            "discovered in the proof_of_delivery index."
        ),
    )
    return parser


async def main(argv: Optional[List[str]] = None) -> int:
    """Script entrypoint — parses args and kicks off :func:`run_migration`."""
    parser = _build_argparser()
    args = parser.parse_args(argv)

    try:
        summary = await run_migration(
            tenant_id=args.tenant_id,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        logger.exception("fuel_ops_hardening_002 crashed: %s", exc)
        return 1

    return 0 if summary.tenants_with_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
