"""Unit tests for the Alembic revision drift check.

Exercises the decision logic of ``check_migrations_current`` without a live
database by monkeypatching the head-resolution helpers, plus the skip / dormant
short-circuits.
"""

from __future__ import annotations

import pytest

import persistence.migration_check as mc
from persistence.migration_check import (
    MigrationsOutOfDateError,
    check_migrations_current,
)


@pytest.fixture(autouse=True)
def _clear_skip_env(monkeypatch):
    monkeypatch.delenv(mc._SKIP_ENV, raising=False)


def _force_enabled(monkeypatch, url: str = "postgresql+psycopg://u:p@localhost/db"):
    """Make the check believe the persistence layer is active."""
    monkeypatch.setattr(
        "persistence.database.is_persistence_enabled", lambda: True
    )

    class _S:
        database_url = url

    monkeypatch.setattr("config.settings.get_settings", lambda: _S())


def test_skip_env_short_circuits(monkeypatch):
    monkeypatch.setenv(mc._SKIP_ENV, "1")
    # Even if heads would mismatch, the skip env returns True without checking.
    monkeypatch.setattr(mc, "_script_heads", lambda: {"head_a"})
    monkeypatch.setattr(mc, "_db_heads", lambda _url: set())
    assert check_migrations_current() is True


def test_dormant_persistence_is_noop(monkeypatch):
    monkeypatch.setattr(
        "persistence.database.is_persistence_enabled", lambda: False
    )
    assert check_migrations_current() is True


def test_sqlite_url_is_skipped(monkeypatch):
    # SQLite is test-only (schema via create_all); the check must not fire even
    # if heads would mismatch.
    _force_enabled(monkeypatch, url="sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(mc, "_script_heads", lambda: {"0003_x"})
    monkeypatch.setattr(mc, "_db_heads", lambda _url: set())
    assert check_migrations_current() is True


def test_current_db_passes(monkeypatch):
    _force_enabled(monkeypatch)
    monkeypatch.setattr(mc, "_script_heads", lambda: {"0003_x"})
    monkeypatch.setattr(mc, "_db_heads", lambda _url: {"0003_x"})
    assert check_migrations_current() is True


def test_behind_db_raises_by_default(monkeypatch):
    _force_enabled(monkeypatch)
    monkeypatch.setattr(mc, "_script_heads", lambda: {"0003_x"})
    monkeypatch.setattr(mc, "_db_heads", lambda _url: {"0002_y"})
    with pytest.raises(MigrationsOutOfDateError) as exc:
        check_migrations_current()
    # Message names the pending revision and the remediation command.
    assert "0003_x" in str(exc.value)
    assert "alembic upgrade head" in str(exc.value)


def test_behind_db_returns_false_when_not_raising(monkeypatch):
    _force_enabled(monkeypatch)
    monkeypatch.setattr(mc, "_script_heads", lambda: {"0003_x"})
    monkeypatch.setattr(mc, "_db_heads", lambda _url: set())
    assert check_migrations_current(raise_on_drift=False) is False


def test_connection_failure_surfaces(monkeypatch):
    _force_enabled(monkeypatch)

    def _boom():
        raise RuntimeError("cannot reach db")

    monkeypatch.setattr(mc, "_script_heads", _boom)
    with pytest.raises(MigrationsOutOfDateError):
        check_migrations_current()
    # Non-raising mode reports False instead.
    assert check_migrations_current(raise_on_drift=False) is False


def test_to_sync_url_normalises_async_drivers():
    assert (
        mc._to_sync_url("postgresql+asyncpg://u:p@h/db")
        == "postgresql+psycopg://u:p@h/db"
    )
    assert mc._to_sync_url("sqlite+aiosqlite:///x.db") == "sqlite:///x.db"
    # Already-sync URLs are unchanged.
    assert (
        mc._to_sync_url("postgresql+psycopg://u:p@h/db")
        == "postgresql+psycopg://u:p@h/db"
    )
