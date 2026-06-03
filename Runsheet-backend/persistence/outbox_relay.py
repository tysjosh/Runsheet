"""Outbox relay: drains unpublished outbox rows into Elasticsearch.

The relay is the bridge that keeps the ES read/search projection consistent
with the Postgres source-of-truth. It polls ``outbox_events`` for rows with
``published_at IS NULL`` (oldest first), indexes each ``payload`` into its
``target_index`` under the aggregate id, and stamps ``published_at`` on
success. Failures increment ``attempts`` and record ``last_error`` so a poison
row does not wedge the queue forever.

Delivery is at-least-once: each event is an idempotent upsert (same ES id), so
re-delivering after a crash between "indexed" and "marked published" is safe.

Run it as a periodic background task (e.g. from the bootstrap scheduler) by
calling :meth:`run_forever`, or drive a single drain with :meth:`drain_once`
(used by tests and one-shot CLI invocations).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import select

from persistence.database import session_scope
from persistence.models import OutboxEventORM

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 10


class OutboxRelay:
    """Projects transactional-outbox rows into Elasticsearch.

    Args:
        es_service: An object exposing ``async index_document(index, id, doc)``
            (the app's ``ElasticsearchService`` satisfies this).
        batch_size: Max events drained per :meth:`drain_once` call.
    """

    def __init__(self, es_service, *, batch_size: int = 100) -> None:
        self._es = es_service
        self._batch_size = batch_size
        self._stopped = asyncio.Event()

    async def drain_once(self) -> int:
        """Project up to ``batch_size`` unpublished events. Returns the count published."""
        from services.time_utils import utcnow

        published = 0
        async with session_scope() as session:
            result = await session.execute(
                select(OutboxEventORM)
                .where(OutboxEventORM.published_at.is_(None))
                .where(OutboxEventORM.attempts < _MAX_ATTEMPTS)
                .order_by(OutboxEventORM.id.asc())
                .limit(self._batch_size)
            )
            events = list(result.scalars().all())

            for event in events:
                try:
                    await self._es.index_document(
                        event.target_index,
                        event.aggregate_id,
                        dict(event.payload),
                    )
                    event.published_at = utcnow()
                    published += 1
                except Exception as exc:  # noqa: BLE001 — record and continue
                    event.attempts += 1
                    event.last_error = str(exc)[:1000]
                    logger.warning(
                        "Outbox relay failed to project event id=%s (%s/%s): %s",
                        event.id, event.attempts, _MAX_ATTEMPTS, exc,
                    )
            # session_scope commits published_at / attempts updates atomically.

        if published:
            logger.info("Outbox relay published %d event(s) to Elasticsearch", published)
        return published

    async def run_forever(self, *, poll_interval_seconds: float = 1.0) -> None:
        """Continuously drain the outbox until :meth:`stop` is called."""
        logger.info("Outbox relay started (poll every %.1fs)", poll_interval_seconds)
        while not self._stopped.is_set():
            try:
                drained = await self.drain_once()
            except Exception:  # noqa: BLE001 — never let the loop die
                logger.exception("Outbox relay drain cycle failed; backing off")
                drained = 0
            # When idle, sleep the poll interval; when busy, loop tight to clear backlog.
            if drained == 0:
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(), timeout=poll_interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        logger.info("Outbox relay stopped")

    def stop(self) -> None:
        """Signal :meth:`run_forever` to exit after the current cycle."""
        self._stopped.set()


async def project_pending(es_service, *, max_batches: Optional[int] = None) -> int:
    """Drain the outbox to completion (or ``max_batches`` cycles). Returns total published.

    Convenience entry point for CLI / backfill use.
    """
    relay = OutboxRelay(es_service)
    total = 0
    batches = 0
    while True:
        n = await relay.drain_once()
        total += n
        batches += 1
        if n == 0:
            break
        if max_batches is not None and batches >= max_batches:
            break
    return total
