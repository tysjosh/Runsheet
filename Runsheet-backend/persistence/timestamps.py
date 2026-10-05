"""Timestamp parsing shared by every compare-by-value guard on stored documents.

The stored and in-memory forms of one instant differ. ``OrderService`` stamps a
``datetime``, ``FuelOrder(...).model_dump(mode="json")`` stores pydantic's
``...Z`` string, ``PostgresDocumentStore.atomic_update`` stamps ``updated_at``
with its own ISO clock, and ``list_for_tenant`` returns ``model_dump(mode=
"python")`` datetimes. Guards therefore compare parsed values, never strings
(loading-plan-executor design K5a).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse a stored or in-memory timestamp to an aware UTC datetime; None/"" stay None.

    Naive values are taken as UTC. An unparseable value raises ``ValueError``;
    callers must not treat it as a match.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


__all__ = ["parse_ts"]
