"""Fixtures for the margin admin API tests (margin-feed FR6-FR8).

``margin_engine`` / ``repo`` / ``flag_on`` come from the margin unit-test
conftest; the app and client are :class:`._margin_api.MarginApi`.
"""
from __future__ import annotations

import pytest_asyncio

from commerce.api import margin_endpoints
from middleware.rate_limiter import limiter
from tests.unit.commerce.margin.conftest import (  # noqa: F401  (fixtures re-exported)
    flag_on,
    margin_engine,
    repo,
)

from ._margin_api import MarginApi


@pytest_asyncio.fixture
async def margin_api(repo, flag_on):  # noqa: F811
    limiter.reset()
    api = MarginApi(repo)
    try:
        yield api
    finally:
        await api.drain_runs()
        await api.client.aclose()
        margin_endpoints.configure_margin_api(margin_service=None, cost_entry_service=None)
        limiter.reset()


@pytest_asyncio.fixture
async def margin_api_flag_off(repo):  # noqa: F811
    """Persistence on, backbone on (``.env.test``), margin flag off (the default)."""
    limiter.reset()
    api = MarginApi(repo)
    try:
        yield api
    finally:
        await api.client.aclose()
        margin_endpoints.configure_margin_api(margin_service=None, cost_entry_service=None)
        limiter.reset()


