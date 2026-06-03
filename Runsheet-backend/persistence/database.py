"""Async SQLAlchemy engine, session factory, and declarative base.

Centralises connection management for the PostgreSQL source-of-truth. The
engine is created lazily from ``settings.database_url`` and cached as a
process-wide singleton. When no ``database_url`` is configured the layer is
dormant: :func:`is_persistence_enabled` returns ``False`` and callers should
fall back to the legacy ES-only path.

The same module supports tests, which pass an in-memory SQLite async URL
(``sqlite+aiosqlite:///:memory:``). SQLite-specific engine arguments (no
pool sizing, ``StaticPool`` so the in-memory DB survives across sessions) are
applied automatically based on the URL scheme.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all persistence ORM models."""


# Process-wide singletons. Reset via :func:`dispose_engine` (used by tests).
_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None
_engine_url: Optional[str] = None


def _resolve_database_url() -> Optional[str]:
    """Return the configured async database URL, or ``None`` if dormant."""
    # Imported lazily so importing this module never forces settings to load
    # (keeps import-time side effects out of test collection).
    from config.settings import get_settings

    url = get_settings().database_url
    if url is None:
        return None
    url = url.strip()
    return url or None


def is_persistence_enabled() -> bool:
    """``True`` when a PostgreSQL (or SQLite) source-of-truth is configured."""
    return _resolve_database_url() is not None


def _build_engine(url: str) -> AsyncEngine:
    """Create an :class:`AsyncEngine` with scheme-appropriate options."""
    from config.settings import get_settings

    settings = get_settings()

    if url.startswith("sqlite"):
        # In-memory SQLite for tests: a StaticPool keeps a single shared
        # connection alive so schema + data persist across sessions within
        # one test, and check_same_thread is relaxed for the asyncio driver.
        return create_async_engine(
            url,
            echo=settings.database_echo,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

    return create_async_engine(
        url,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
    )


def get_engine() -> AsyncEngine:
    """Return the cached async engine, creating it on first use.

    Raises:
        RuntimeError: if the persistence layer is dormant (no ``database_url``).
    """
    global _engine, _sessionmaker, _engine_url

    url = _resolve_database_url()
    if url is None:
        raise RuntimeError(
            "Persistence layer is dormant: settings.database_url is not set. "
            "Guard call sites with persistence.is_persistence_enabled()."
        )

    # Rebuild if the configured URL changed (e.g. settings cache cleared in tests).
    if _engine is None or _engine_url != url:
        _engine = _build_engine(url)
        _sessionmaker = async_sessionmaker(
            bind=_engine,
            expire_on_commit=False,
            autoflush=False,
        )
        _engine_url = url
        logger.info("Persistence engine initialised for %s", _scrub(url))

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the cached async session factory, creating the engine if needed."""
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None  # for type-checkers; get_engine sets it
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transactional scope around a series of operations.

    Commits on success, rolls back on exception, and always closes the
    session. Business write + outbox insert should happen inside one scope so
    they commit atomically.
    """
    sessionmaker = get_sessionmaker()
    session = sessionmaker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Dispose the engine and reset singletons (used by tests / shutdown)."""
    global _engine, _sessionmaker, _engine_url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
    _engine_url = None


def _scrub(url: str) -> str:
    """Redact credentials from a DB URL for safe logging."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme = url[: scheme_sep + 3]
    rest = url[scheme_sep + 3 :]
    host_part = rest.split("@", 1)[1]
    return f"{scheme}***@{host_part}"
