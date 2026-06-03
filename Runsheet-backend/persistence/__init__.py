"""PostgreSQL source-of-truth persistence layer.

This package introduces PostgreSQL as the transactional system-of-record for
the financial / commerce models (customers, accounts, invoices, payments) and
for concurrency-control primitives (idempotency keys), while keeping
Elasticsearch as a rebuildable read/search projection.

Design (advisory doc realised as code — first migration slice):

    PostgreSQL  = source-of-truth (ACID, FKs, UNIQUE constraints, FOR UPDATE)
    Outbox      = transactional outbox table written in the SAME transaction
                  as the business write
    Relay       = OutboxRelay drains the outbox and projects each event into
                  Elasticsearch (the existing *_current indices), so ES stays
                  in sync and remains fully rebuildable from Postgres.

The layer is **opt-in**: it is dormant unless ``settings.database_url`` is
configured. When dormant, the rest of the application behaves exactly as it
did before (ES-only). This makes adoption deliberate and reversible.
"""

from persistence.database import (
    Base,
    get_engine,
    get_sessionmaker,
    session_scope,
    is_persistence_enabled,
    dispose_engine,
)

__all__ = [
    "Base",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
    "is_persistence_enabled",
    "dispose_engine",
]
