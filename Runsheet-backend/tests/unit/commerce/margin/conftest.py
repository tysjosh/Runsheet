"""Fixtures for the margin-feed unit tests.

``margin_engine`` follows ``tests/persistence/conftest.py``: the persistence
layer is pointed at an in-memory SQLite database, the schema (including the
margin tables, their CHECKs and partial unique indexes) is built with
``create_all``, and torn down afterwards. Only tests that ask for it get a
database; the pure money tests run without one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import pytest
import pytest_asyncio

from commerce.services.margin_repository import MarginCandidate, MarginRepository
from config.settings import clear_settings_cache, get_settings
from persistence.database import Base, dispose_engine, get_engine

TENANT_A = "tenant-margin-a"
TENANT_B = "tenant-margin-b"
AS_OF = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def margin_engine(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    clear_settings_cache()
    assert get_settings().database_url == "sqlite+aiosqlite:///:memory:"
    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_engine()
        clear_settings_cache()


@pytest_asyncio.fixture
async def repo(margin_engine) -> MarginRepository:
    return MarginRepository()


def _candidate(**overrides: Any) -> MarginCandidate:
    """A costed, unflagged delivery candidate; override any field."""

    values: dict[str, Any] = dict(
        stage="delivery",
        source_key="order:ORD-1",
        order_id="ORD-1",
        customer_id="CUST-1",
        account_id="ACCT-1",
        product_code="DIESEL_2",
        terminal_id="TERM-1",
        gallons_ugal=1_000 * 1_000_000,
        unit_price_micros=3_000_000,
        revenue_cents=300_000,
        method="wac",
        product_cost_micros=2_500_000,
        adders_micros=0,
        landed_cost_micros=2_500_000,
        cost_cents=250_000,
        margin_cents=50_000,
        margin_per_gallon_micros=500_000,
        margin_bp=1_667,
        floor_micros_used=100_000,
        cost_snapshot={"method": "wac"},
        as_of=AS_OF,
        input_hash="h1",
    )
    values.update(overrides)
    return MarginCandidate(**values)


def _missing_cost(**overrides: Any) -> MarginCandidate:
    """A ``method=none`` candidate (all cost fields null, missing_cost flag)."""

    values: dict[str, Any] = dict(
        method="none",
        product_cost_micros=None,
        landed_cost_micros=None,
        cost_cents=None,
        margin_cents=None,
        margin_per_gallon_micros=None,
        margin_bp=None,
        no_cost_reason="no_lots_no_rack",
        flag_missing_cost=True,
    )
    values.update(overrides)
    return _candidate(**values)


@pytest.fixture
def make_candidate() -> Callable[..., MarginCandidate]:
    return _candidate


@pytest.fixture
def make_missing_cost() -> Callable[..., MarginCandidate]:
    return _missing_cost


# Margin service / sweep fixtures (FEAT-003).


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("COMMERCE_MARGIN_FEED_ENABLED", "true")
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture(params=[False, True], ids=["es_path", "postgres_path"])
def read_path(request, monkeypatch, margin_engine):
    """Run once with the ES fake and once with the SQLite mirror (freeze 10/13)."""

    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true" if request.param else "false")
    clear_settings_cache()
    yield request.param
    clear_settings_cache()
