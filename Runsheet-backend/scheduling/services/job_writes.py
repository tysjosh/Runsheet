"""The one write path for ``jobs_current`` documents.

Every job write goes to two places: the document store (``jobs_current``) and
the Postgres current-state row (``mirror_current_state_upsert("job", ...)``).
``JobService._get_job_doc`` — and so ``GET /jobs/{id}``, the driver ack/accept
state checks and job messaging — reads the Postgres row once reads are cut over.
A writer that updates only the document store leaves that row stale: a driver
accept that never mirrored kept the job ``scheduled`` in Postgres, so the
following ack was refused (N-FF-2).

The writes were spread over JobService, the driver endpoints, the cargo
service, the reroute service, the delay sweep and the confirmation protocol,
each remembering (or not) to mirror. They all go through here now, and each
function below ends with the mirror, so a new caller cannot skip it.

The mirror is best-effort, as everywhere else: the bridge logs and swallows a
Postgres failure, and is a no-op when dual-write is off.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from scheduling.services.scheduling_es_mappings import JOBS_CURRENT_INDEX


async def mirror_job(job_doc: dict, job_id: Optional[str] = None) -> None:
    """Upsert the full job document into its Postgres current-state row."""
    from commerce.services.commerce_persistence_bridge import (
        mirror_current_state_upsert,
    )

    await mirror_current_state_upsert(
        "job", job_doc, doc_id=job_id or job_doc.get("job_id")
    )


async def index_job(es: Any, job_id: str, doc: dict) -> dict:
    """Write a whole job document (creation) and mirror it."""
    await es.index_document(JOBS_CURRENT_INDEX, job_id, doc)
    await mirror_job(doc, job_id)
    return doc


async def update_job_fields(
    es: Any,
    job_id: str,
    fields: dict,
    *,
    job_doc: Optional[dict] = None,
) -> dict:
    """Merge ``fields`` into a job and mirror the merged document.

    Args:
        es: The document-store facade.
        job_id: The job to update.
        fields: The partial update (top-level keys, unwrapped).
        job_doc: The job as the caller already read it. It is updated in
            place and mirrored. When omitted the merged document is read back
            from the store; if that read finds nothing, nothing is mirrored.

    Returns:
        The merged job document (``{}`` when ``job_doc`` was omitted and the
        read-back found nothing).
    """
    await es.update_document(JOBS_CURRENT_INDEX, job_id, fields)
    if job_doc is None:
        job_doc = await es.get_document(JOBS_CURRENT_INDEX, job_id)
        if not isinstance(job_doc, dict) or not job_doc:
            return {}
    else:
        job_doc.update(fields)
    await mirror_job(job_doc, job_id)
    return job_doc


async def atomic_update_job(
    es: Any, job_id: str, transform: Callable[[dict], Optional[dict]]
) -> Tuple[Optional[dict], bool]:
    """Row-locked read-modify-write of a job; mirrors when ``transform`` applied.

    Same contract as the store's ``atomic_update``: returns ``(doc, applied)``.
    """
    doc, applied = await es.atomic_update(JOBS_CURRENT_INDEX, job_id, transform)
    if applied and isinstance(doc, dict):
        await mirror_job(doc, job_id)
    return doc, applied
