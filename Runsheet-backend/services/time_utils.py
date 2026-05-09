"""
UTC-aware datetime helper used across services that write timestamps to
Elasticsearch, Redis, or external APIs. Replaces ad-hoc
``datetime.now()`` and the deprecated ``datetime.utcnow()`` so every
timestamp is timezone-aware and serialises to an ISO-8601 string with
the ``+00:00`` suffix.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current time as a timezone-aware ``datetime`` in UTC."""
    return datetime.now(timezone.utc)
