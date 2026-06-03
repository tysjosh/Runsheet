"""Transactional outbox enqueue helper.

A single function, :func:`enqueue`, appends an :class:`OutboxEventORM` row to
the current session. Callers MUST invoke it inside the same ``session_scope``
as the business write so the event and the row commit (or roll back) together.

The relay (:mod:`persistence.outbox_relay`) later projects each row into ES.
Because the projection document is computed at enqueue time from the freshly
written ORM row, the relay needs no knowledge of the domain — it just indexes
``payload`` into ``target_index`` under id ``aggregate_id``.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.models import OutboxEventORM
from persistence.projections import PROJECTORS


class UnknownAggregateError(ValueError):
    """Raised when no projector is registered for an aggregate type."""


def enqueue(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: str,
    tenant_id: str,
    event_type: str,
    row: Any,
) -> OutboxEventORM:
    """Append an outbox event projecting ``row`` to its ES index.

    Args:
        session: The active async session (same transaction as the write).
        aggregate_type: One of the keys in ``projections.PROJECTORS``
            (``customer`` | ``account`` | ``invoice`` | ``payment``).
        aggregate_id: The aggregate's primary id (used as the ES document id).
        tenant_id: Tenant scope.
        event_type: Domain event label, e.g. ``created`` / ``updated`` /
            ``voided``. Stored for audit; the relay treats every event as an
            idempotent upsert of the projected document.
        row: The ORM row to project (already added/flushed in this session).

    Returns:
        The created (unflushed) :class:`OutboxEventORM`.

    Raises:
        UnknownAggregateError: if ``aggregate_type`` has no projector.
    """
    projector = PROJECTORS.get(aggregate_type)
    if projector is None:
        raise UnknownAggregateError(
            f"No projector registered for aggregate_type={aggregate_type!r}"
        )
    target_index, to_doc = projector
    payload: Dict[str, Any] = to_doc(row)

    event = OutboxEventORM(
        tenant_id=tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        target_index=target_index,
        payload=payload,
    )
    session.add(event)
    return event
