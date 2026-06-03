"""Alembic migration environment for the persistence layer.

The database URL is sourced from the application settings
(``settings.database_url``) rather than alembic.ini, so the migration runner
and the runtime engine always target the same database. Migrations run
synchronously: an async SQLAlchemy URL (``postgresql+psycopg://...``) is
normalised to its sync form for Alembic, which uses a blocking engine.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure the backend package root is importable when alembic runs from CWD.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from persistence.database import Base  # noqa: E402
import persistence.models  # noqa: E402,F401  (registers tables on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_sync_url() -> str:
    """Return a synchronous SQLAlchemy URL for Alembic to connect with."""
    from config.settings import get_settings

    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "Cannot run migrations: settings.database_url is not configured. "
            "Set DATABASE_URL before invoking alembic."
        )
    # Alembic uses a synchronous engine — strip async driver suffixes.
    return (
        url.replace("postgresql+psycopg://", "postgresql+psycopg://")
        .replace("postgresql+asyncpg://", "postgresql+psycopg://")
        .replace("sqlite+aiosqlite://", "sqlite://")
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    context.configure(
        url=_resolve_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_sync_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
