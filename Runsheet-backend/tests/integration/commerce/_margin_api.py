"""Margin admin API harness shared by the integration and export tests.

The real ``margin_endpoints`` router runs on a local ``FastAPI()`` app with the
real ``MarginService`` / ``MarginCostEntryService`` over the in-memory SQLite
margin tables and the document-store fake. Requests go through
``httpx.AsyncClient`` + ``ASGITransport`` so the app runs on the test's own
event loop, which the single-connection SQLite engine needs.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded

from commerce.api import margin_endpoints
from commerce.services.margin_cost_entry_service import MarginCostEntryService
from errors.handlers import register_exception_handlers
from middleware.rate_limiter import _custom_rate_limit_handler, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit.commerce.margin._margin_fakes import CapturingTelemetry, FakeTerminals
from tests.unit.commerce.margin._service_support import SweepStore, build_service
from tests.unit.commerce.margin.conftest import TENANT_A, TENANT_B

TERMINAL = "TERM-1"
TERMINAL_B_ONLY = "TERM-B"
BASE = "/api/commerce/margin"


class MarginApi:
    """One app + client; ``as_(...)`` switches the caller's tenant and roles."""

    def __init__(self, repository: Any) -> None:
        self.repo = repository
        self.store = SweepStore()
        # Identical terminal ids in both tenants (AC-24), plus one only B has.
        self.terminals = FakeTerminals(
            {TENANT_A: [TERMINAL], TENANT_B: [TERMINAL, TERMINAL_B_ONLY]}
        )
        self.telemetry = CapturingTelemetry()
        self.service = build_service(
            repository, self.store, terminals=self.terminals, telemetry=self.telemetry
        )
        self.entries = MarginCostEntryService(
            repository, terminals=self.terminals, es_service=self.store, telemetry=self.telemetry
        )
        margin_endpoints.configure_margin_api(
            margin_service=self.service, cost_entry_service=self.entries
        )
        self.ctx = TenantContext(
            tenant_id=TENANT_A, user_id="u-admin", has_pii_access=False, roles=["admin"]
        )
        self.app = FastAPI()
        self.app.state.limiter = limiter
        register_exception_handlers(self.app)
        self.app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
        self.app.include_router(margin_endpoints.router)
        self.app.dependency_overrides[get_tenant_context] = lambda: self.ctx
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    def as_(self, *roles: str, tenant: str = TENANT_A, user: str = "u-admin") -> "MarginApi":
        self.ctx = TenantContext(
            tenant_id=tenant, user_id=user, has_pii_access=False, roles=list(roles)
        )
        return self

    async def drain_runs(self) -> None:
        runs = list(getattr(self.service, "_runs", ()))
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)
        await self.service.drain()


def route_list() -> List[Tuple[str, str]]:
    """(method, path) for every route on the margin router, so a new route is covered."""
    out: List[Tuple[str, str]] = []
    for route in margin_endpoints.router.routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            out.append((method, route.path))
    return out


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


async def call(api: MarginApi, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Send a request shaped for the route: JSON body for POST/PUT, a file for import."""
    url = concrete(path)
    if method in ("POST", "PUT") and not kwargs:
        if url.endswith("/cost-entries/import"):
            kwargs = {"files": {"file": ("c.csv", b"kind\n", "text/csv")}}
        else:
            kwargs = {"json": {}}
    return await api.client.request(method, url, **kwargs)


def error_code(resp: httpx.Response) -> Optional[str]:
    try:
        body: Dict[str, Any] = resp.json()
    except ValueError:
        return None
    return body.get("error_code")
