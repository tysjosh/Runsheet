"""Bootstrap for the PostgreSQL source-of-truth persistence layer.

Starts the :class:`persistence.outbox_relay.OutboxRelay` as a background task
that continuously drains transactional-outbox events into Elasticsearch, so the
ES projection stays eventually-consistent with the Postgres source-of-truth.

The whole module is a no-op when the persistence layer is dormant
(``settings.database_url`` unset) or when ``outbox_relay_enabled`` is False —
so the default ES-only deployment is completely unaffected.

The relay is registered AFTER ``core`` (which builds the ES service) so the
projection target exists, and is cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import logging

from .container import ServiceContainer

logger = logging.getLogger(__name__)

# Module-level handles so shutdown can stop + cancel the task cleanly.
_relay = None
_relay_task = None


async def initialize(app, container: ServiceContainer) -> None:
    """Start the outbox relay background task when persistence is active."""
    global _relay, _relay_task

    from persistence.database import is_persistence_enabled

    if not is_persistence_enabled():
        logger.info("Persistence layer dormant (no DATABASE_URL) — outbox relay not started")
        return

    # Fail fast if the database is behind the latest migrations. An authored-
    # but-unapplied migration otherwise surfaces as a 500 on the first
    # read-cutover query (missing column) rather than an obvious boot error.
    # Skippable via SKIP_MIGRATION_CHECK=1 for emergencies.
    from persistence.migration_check import check_migrations_current

    check_migrations_current(raise_on_drift=True)

    settings = container.get("settings") if container.has("settings") else None
    if settings is not None and not getattr(settings, "outbox_relay_enabled", True):
        logger.info("Outbox relay disabled by settings — not started")
        return

    es_service = container.get("es_service") if container.has("es_service") else None
    if es_service is None:
        logger.warning("Outbox relay not started: es_service unavailable in container")
        return

    poll_interval = (
        getattr(settings, "outbox_relay_poll_interval_seconds", 1.0)
        if settings is not None
        else 1.0
    )

    from persistence.outbox_relay import OutboxRelay

    _relay = OutboxRelay(es_service)
    container.outbox_relay = _relay
    _relay_task = asyncio.create_task(
        _relay.run_forever(poll_interval_seconds=poll_interval)
    )
    logger.info(
        "Outbox relay started (poll interval %.1fs) — projecting Postgres "
        "outbox events into Elasticsearch",
        poll_interval,
    )


async def shutdown(app, container: ServiceContainer) -> None:
    """Stop the relay loop and cancel its background task."""
    global _relay, _relay_task

    if _relay is not None:
        _relay.stop()

    if _relay_task is not None and not _relay_task.done():
        _relay_task.cancel()
        try:
            await _relay_task
        except asyncio.CancelledError:
            pass
        logger.info("Outbox relay task stopped")

    _relay = None
    _relay_task = None
