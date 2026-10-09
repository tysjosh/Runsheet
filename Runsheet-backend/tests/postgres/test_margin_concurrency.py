"""Margin feed on real PostgreSQL: migration, locks, partial uniques, CHECKs.

SQLite cannot show any of this: ``with_for_update`` is a no-op there, and the
point of these tests is that concurrent writers on separate connections are
serialized by row locks and partial unique indexes as the design says.

The suite builds its own scratch database (``margin_pytest_<hex>``) on the
server named by ``POSTGRES_TEST_URL`` / ``DATABASE_URL``, runs the real Alembic
chain into it (so the schema under test is the migration's, not
``create_all``'s), and drops it afterwards. The application database is never
touched. Without a server the suite skips; CI's docstore job forbids skips.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from commerce.models.margin import MAX_UNIT_COST_MICROS, compute_margin_amounts
from commerce.services.margin_repository import (
    MarginCandidate,
    MarginDuplicateEntryError,
    MarginRecomputeRunningError,
    MarginRepository,
)

_BACKEND = Path(__file__).resolve().parents[2]
TENANT = "pytest-margin-tenant"
AS_OF = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
MARGIN_TABLES = (
    "margin_cost_entries",
    "margin_settings",
    "margin_records",
    "margin_alerts",
    "margin_recompute_runs",
    "margin_weekly_reports",
    "margin_skipped_sources",
)


def _sync(url: str) -> str:
    return str(make_url(url).set(drivername="postgresql")
               .render_as_string(hide_password=False))


def _alembic(database_url: str, *args: str) -> None:
    env = dict(os.environ, DATABASE_URL=database_url)
    env.pop("POSTGRES_TEST_URL", None)
    env.setdefault("ENVIRONMENT", "test")
    env.setdefault("JWT_SECRET", "ci-test-jwt-secret")
    env.setdefault("JWT_ALGORITHM", "HS256")
    env.setdefault("REDIS_URL", "redis://localhost:6379/0")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _scalar(sync_url: str, sql: str) -> Any:
    import psycopg

    with psycopg.connect(sync_url) as conn:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None


@pytest.fixture(scope="session")
def margin_db_url(postgres_url):
    """A migrated scratch database; yields its async URL."""

    import psycopg

    admin = _sync(postgres_url)
    try:
        conn = psycopg.connect(admin, autocommit=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    name = f"margin_pytest_{uuid.uuid4().hex[:12]}"
    with conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = str(make_url(postgres_url).set(database=name)
              .render_as_string(hide_password=False))
    try:
        _alembic(url, "upgrade", "head")
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest_asyncio.fixture
async def pg_repo(margin_db_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(margin_db_url, pool_size=10)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(MARGIN_TABLES)}"))
    makers = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with makers() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    repo = MarginRepository(session_factory=scope)
    repo.engine = engine  # for raw SQL in tests
    try:
        yield repo
    finally:
        await engine.dispose()


def _candidate(**overrides: Any) -> MarginCandidate:
    values: dict[str, Any] = dict(
        stage="delivery", source_key="order:ORD-1", order_id="ORD-1", customer_id="CUST-1",
        product_code="DIESEL_2", terminal_id="TERM-1", gallons_ugal=1_000_000_000,
        unit_price_micros=3_000_000, revenue_cents=300_000, method="wac",
        product_cost_micros=2_500_000, adders_micros=0, landed_cost_micros=2_500_000,
        cost_cents=250_000, margin_cents=50_000, margin_per_gallon_micros=500_000,
        margin_bp=1_667, floor_micros_used=100_000, cost_snapshot={"method": "wac"},
        as_of=AS_OF, input_hash="h1",
    )
    values.update(overrides)
    return MarginCandidate(**values)


def _missing(**overrides: Any) -> MarginCandidate:
    values: dict[str, Any] = dict(
        method="none", product_cost_micros=None, landed_cost_micros=None, cost_cents=None,
        margin_cents=None, margin_per_gallon_micros=None, margin_bp=None,
        no_cost_reason="no_lots_no_rack", flag_missing_cost=True,
    )
    values.update(overrides)
    return _candidate(**values)


def _entry(**overrides: Any) -> dict:
    values = dict(
        kind="purchase", product_code="DIESEL_2", terminal_id="TERM-1",
        effective_at=AS_OF - timedelta(days=3), unit_cost_micros=2_500_000,
        gallons_milli=1_000_000, natural_key="nk-1", source="manual", created_by="admin",
    )
    values.update(overrides)
    return values


async def _count(repo, sql: str, **params: Any) -> int:
    async with repo.engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_includes_0011_margin_feed():
    # 0012_customer_portal was re-parented onto 0011_margin_feed, so the
    # single head is a descendant of the margin revision, not the margin
    # revision itself.
    script = _script()
    heads = script.get_heads()
    assert len(heads) == 1
    chain = {rev.revision for rev in script.walk_revisions(base="base", head=heads[0])}
    assert "0011_margin_feed" in chain
    assert script.get_revision("0011_margin_feed").down_revision == "0010_acct_override_audit"


def test_migration_round_trip(margin_db_url):
    sync = _sync(margin_db_url)
    head = _script().get_current_head()
    tables_sql = (
        "select count(*) from information_schema.tables "
        "where table_schema = 'public' and table_name like 'margin\\_%'"
    )
    assert _scalar(sync, "select version_num from alembic_version") == head
    assert _scalar(sync, tables_sql) == 7

    _alembic(margin_db_url, "downgrade", "0010_acct_override_audit")
    assert _scalar(sync, "select version_num from alembic_version") == "0010_acct_override_audit"
    assert _scalar(sync, tables_sql) == 0

    _alembic(margin_db_url, "upgrade", "head")
    assert _scalar(sync, "select version_num from alembic_version") == head
    assert _scalar(sync, tables_sql) == 7
    jsonb = _scalar(
        sync,
        "select data_type from information_schema.columns "
        "where table_name = 'margin_records' and column_name = 'cost_snapshot'",
    )
    assert jsonb == "jsonb"


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    ["cost_cents", "margin_cents", "product_cost_micros", "margin_per_gallon_micros"],
)
async def test_method_none_with_zero_cost_column_fails_the_check(pg_repo, column):
    with pytest.raises(IntegrityError) as exc:
        await pg_repo.write_record(TENANT, _missing(**{column: 0}), "live")
    assert getattr(exc.value.orig, "sqlstate", None) == "23514"  # check_violation
    assert "ck_mr_cost_null_iff_none" in str(exc.value)
    assert await _count(pg_repo, "select count(*) from margin_records") == 0


async def test_missing_flag_check(pg_repo):
    with pytest.raises(IntegrityError) as exc:
        await pg_repo.write_record(TENANT, _missing(flag_missing_cost=False), "live")
    assert "ck_mr_missing_flag" in str(exc.value)


async def test_margin_bp_extreme_bound_persists(pg_repo):
    gallons_ugal = 1_000_000 * 1_000_000  # 1,000,000 gal
    revenue = 100  # 1 micro x 1,000,000 gal = $1.00
    amounts = compute_margin_amounts(
        gallons_ugal=gallons_ugal, unit_price_micros=1, revenue_cents=revenue,
        landed_cost_micros=MAX_UNIT_COST_MICROS,
    )
    assert amounts.margin_bp < -(2**31)
    result = await pg_repo.write_record(
        TENANT,
        _candidate(gallons_ugal=gallons_ugal, unit_price_micros=1, revenue_cents=revenue,
                   product_cost_micros=MAX_UNIT_COST_MICROS,
                   landed_cost_micros=MAX_UNIT_COST_MICROS, cost_cents=amounts.cost_cents,
                   margin_cents=amounts.margin_cents,
                   margin_per_gallon_micros=amounts.margin_per_gallon_micros,
                   margin_bp=amounts.margin_bp),
        "live",
    )
    stored = await pg_repo.get_record(TENANT, result.record["record_id"])
    assert stored["margin_bp"] == amounts.margin_bp
    assert stored["cost_cents"] == 10_000_000_000


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_writers_leave_one_active_row(pg_repo):
    for i in range(5):
        key = f"order:RACE-{i}"
        results = await asyncio.gather(
            pg_repo.write_record(TENANT, _candidate(source_key=key, input_hash="h1"), "live"),
            pg_repo.write_record(TENANT, _candidate(source_key=key, input_hash="h2"), "live"),
        )
        assert all(r.outcome != "conflict" for r in results)
        active = await _count(
            pg_repo,
            "select count(*) from margin_records where tenant_id = :t and source_key = :k "
            "and status = 'active'",
            t=TENANT, k=key,
        )
        assert active == 1


async def test_concurrent_entry_inserts_one_winner(pg_repo):
    results = await asyncio.gather(
        pg_repo.insert_entry(TENANT, _entry()),
        pg_repo.insert_entry(TENANT, _entry()),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, MarginDuplicateEntryError)]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0].existing_entry_id == winners[0]["entry_id"]

    # Different natural keys, same BOL: uq_mce_active_bol decides.
    results = await asyncio.gather(
        pg_repo.insert_entry(TENANT, _entry(natural_key="b1", bol_id="BOL-9")),
        pg_repo.insert_entry(TENANT, _entry(natural_key="b2", bol_id="BOL-9")),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == ["MarginDuplicateEntryError", "dict"]


async def test_concurrent_start_run_one_winner(pg_repo):
    args = dict(requested_by="admin", start_date=date(2026, 9, 1), end_date=date(2026, 9, 7),
                stages=["invoice"], only_missing=True, reason="r")
    results = await asyncio.gather(
        pg_repo.start_run(TENANT, **args),
        pg_repo.start_run(TENANT, **args),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == ["MarginRecomputeRunningError", "dict"]
    assert await _count(
        pg_repo, "select count(*) from margin_recompute_runs where status = 'running'"
    ) == 1


async def test_concurrent_ensure_activated_returns_one_value(pg_repo):
    """Freeze-13 (e): a new tenant activated twice at once gets one watermark."""

    first, second = await asyncio.gather(
        pg_repo.ensure_activated(TENANT, now=AS_OF),
        pg_repo.ensure_activated(TENANT, now=AS_OF + timedelta(seconds=5)),
    )
    assert first == second
    assert await pg_repo.ensure_activated(TENANT, now=AS_OF + timedelta(days=1)) == first
    assert await _count(pg_repo, "select count(*) from margin_settings") == 1


async def test_concurrent_alert_outcomes_create_one_alert(pg_repo):
    """Two ECS tasks settling one pending record produce one margin_alerts row."""

    record = (await pg_repo.write_record(
        TENANT, _candidate(flag_negative_margin=True), "live"
    )).record
    alert = {"alert_type": "negative_margin", "severity": "high",
             "dedupe_key": record["record_id"], "record_id": record["record_id"],
             "order_id": record["order_id"]}
    results = await asyncio.gather(
        pg_repo.record_alert_outcome(TENANT, record["record_id"], [alert]),
        pg_repo.record_alert_outcome(TENANT, record["record_id"], [alert]),
    )
    assert sorted(len(r) for r in results) == [0, 1]
    assert await _count(pg_repo, "select count(*) from margin_alerts") == 1
    assert (await pg_repo.get_record(TENANT, record["record_id"]))["alert_state"] == "done"


# ---------------------------------------------------------------------------
# Query plans
# ---------------------------------------------------------------------------


async def test_pending_queue_query_uses_the_partial_index(pg_repo):
    for i in range(50):
        await pg_repo.write_record(
            TENANT, _candidate(source_key=f"order:P{i}", flag_negative_margin=i % 10 == 0),
            "live",
        )
    stmt = MarginRepository._pending_records_stmt(TENANT, 500)
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    async with pg_repo.engine.connect() as conn:
        await conn.execute(text("ANALYZE margin_records"))
        await conn.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(row[0] for row in await conn.execute(text(f"EXPLAIN {sql}")))
    assert "ix_mr_alert_pending" in plan, plan
