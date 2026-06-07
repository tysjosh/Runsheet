"""Fast-fail Alembic revision check for the PostgreSQL source-of-truth.

A migration can be authored (ORM model + ``alembic/versions/*.py``) but never
applied to a running database. When that happens, the read-cutover path issues
``SELECT``s that reference columns the DB does not have, producing a 500 on the
first read rather than an obvious boot-time error.

:func:`check_migrations_current` compares the database's applied Alembic
revision(s) against the migration scripts' head(s) and reports drift. It is
called at startup (see ``bootstrap/persistence.py``) so an unapplied migration
fails fast and loudly with the exact remediation command, and is also exposed
via ``scripts/check_migrations.py`` for deploy/CI use.

The check is a no-op when the persistence layer is dormant
(``settings.database_url`` unset) — the default ES-only deployment is
unaffected. It can be disabled with ``SKIP_MIGRATION_CHECK=1`` as an emergency
escape hatch.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _BACKEND_ROOT / "alembic"

#: Env var that disables the check entirely (emergency escape hatch).
_SKIP_ENV = "SKIP_MIGRATION_CHECK"


class MigrationsOutOfDateError(RuntimeError):
    """Raised when the database is behind the latest migration head(s)."""


def _to_sync_url(url: str) -> str:
    """Normalise an async SQLAlchemy URL to its sync form for a blocking engine.

    Mirrors ``alembic/env.py::_resolve_sync_url`` so the check connects to the
    same database the migration runner targets.
    """
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://").replace(
        "sqlite+aiosqlite://", "sqlite://"
    )


def _script_heads() -> Set[str]:
    """Return the migration scripts' head revision id(s)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    script = ScriptDirectory.from_config(cfg)
    return set(script.get_heads())


def _db_heads(sync_url: str) -> Set[str]:
    """Return the revision id(s) currently applied to the database."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            return set(ctx.get_current_heads())
    finally:
        engine.dispose()


def check_migrations_current(*, raise_on_drift: bool = True) -> bool:
    """Verify the database is migrated to the latest Alembic head(s).

    Returns ``True`` when the DB is current (or when the check is skipped /
    persistence is dormant). When the DB is behind:

    * if ``raise_on_drift`` is ``True`` (startup default), raises
      :class:`MigrationsOutOfDateError` with the remediation command;
    * otherwise logs an error and returns ``False`` (used by the CLI to set an
      exit code without a traceback).

    Args:
        raise_on_drift: Whether to raise (vs. return ``False``) on drift.

    Returns:
        ``True`` when current/skipped/dormant, ``False`` when behind and
        ``raise_on_drift`` is ``False``.

    Raises:
        MigrationsOutOfDateError: When behind and ``raise_on_drift`` is ``True``.
    """
    if os.environ.get(_SKIP_ENV):
        logger.warning(
            "Alembic migration check skipped (%s set) — schema drift will not "
            "be detected at startup",
            _SKIP_ENV,
        )
        return True

    # Dormant persistence layer (ES-only deployment): nothing to check.
    from persistence.database import is_persistence_enabled

    if not is_persistence_enabled():
        logger.debug("Migration check skipped: persistence layer dormant")
        return True

    from config.settings import get_settings

    database_url = get_settings().database_url
    if not database_url:  # defensive; is_persistence_enabled already covers this
        return True

    sync_url = _to_sync_url(database_url)

    # The Alembic migrations target PostgreSQL. SQLite is used only by the test
    # harness, where the schema is provisioned directly via
    # ``Base.metadata.create_all`` (no Alembic history), so a revision check
    # would be a false positive. Skip it for sqlite.
    if sync_url.startswith("sqlite"):
        logger.debug("Migration check skipped: sqlite URL (test/dev schema)")
        return True

    try:
        heads = _script_heads()
        current = _db_heads(sync_url)
    except Exception as exc:  # noqa: BLE001
        # A connection/parse failure should not be silently swallowed, but it
        # is a different failure mode than "behind". Surface it clearly; at
        # startup this aborts boot (the DB is unreachable anyway).
        msg = f"Could not verify Alembic migration state: {exc}"
        if raise_on_drift:
            raise MigrationsOutOfDateError(msg) from exc
        logger.error(msg)
        return False

    if current == heads:
        logger.info(
            "Alembic migration check passed (DB at head: %s)",
            ", ".join(sorted(heads)) or "<none>",
        )
        return True

    pending = heads - current
    msg = (
        "Database schema is behind the latest migrations. "
        f"DB heads={sorted(current) or ['<none>']}, "
        f"expected heads={sorted(heads)}, pending={sorted(pending)}. "
        "Run:  ./venv/bin/alembic upgrade head"
    )
    if raise_on_drift:
        raise MigrationsOutOfDateError(msg)
    logger.error(msg)
    return False


__all__ = ["check_migrations_current", "MigrationsOutOfDateError"]
