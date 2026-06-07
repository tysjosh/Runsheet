#!/usr/bin/env python3
"""CLI wrapper for the Alembic revision drift check.

Exits non-zero (without a traceback) when the configured database is behind the
latest migration head(s), printing the remediation command. Intended for deploy
pre-flight and local use:

    DATABASE_URL=postgresql+psycopg://... ENVIRONMENT=development \\
        ./venv/bin/python -m scripts.check_migrations

A no-op (exit 0) when the persistence layer is dormant (no DATABASE_URL) or when
SKIP_MIGRATION_CHECK is set.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the backend package root is importable when run as a script.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


def main() -> int:
    from persistence.migration_check import check_migrations_current

    ok = check_migrations_current(raise_on_drift=False)
    if ok:
        print("✅ Database schema is at the latest Alembic head.")
        return 0
    print(
        "❌ Database schema is behind the latest migrations. "
        "Run: ./venv/bin/alembic upgrade head",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
