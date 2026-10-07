"""Real-auth harness for the customer-portal suites (design §13).

Suites here import ``main.app`` and run with ``auth_provider="supertokens"``
(the only provider), with fake session verifiers installed through the real
seams — ``configure_session_verifier`` (HTTP) and
``configure_ws_session_verifier`` (WebSocket). They never use
``override_auth``: it bypasses the central deny (E1) and replaces
``get_tenant_context`` (E1b), which is exactly what these suites test.

A request picks its session with the ``X-Portal-Test-Session`` header (HTTP)
or ``?token=<key>`` (WebSocket). Data collaborators are in-memory fakes wired
through the portal ``configure_*`` seams and restored afterwards.

Fixtures later FEATs reuse:

* ``portal_app`` / ``client``: ``main.app`` and a non-raising TestClient.
* ``portal_on``: portal + backbone flags on (``set_flags`` to change them).
* ``sessions``: :class:`SessionFactory`; ``cA``/``cB`` (demo-tenant customers
  A/B) and ``cC`` (qa-tenant-b customer C), each with an active grant.
  ``sessions.staff(role)`` / ``sessions.staff_bundle()`` for staff.
* ``portal_fakes``: :class:`PortalFakes` (grant store, customer service,
  pipeline, invoice service, spies).
* ``handler_spy``: records every ``APIRoute.handle`` call.
* ``portal_routes()`` / ``fill_path()`` / ``audit_records()`` helpers.
"""
from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

# Same stub the route smoke test installs before importing main. The root
# conftest already imports the real module, which makes this a no-op there.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

T1 = "demo-tenant"
T2 = "qa-tenant-b"
CUSTOMER_A = "QA-PORTAL-CUST-A"
CUSTOMER_B = "QA-PORTAL-CUST-B"
CUSTOMER_C = "QA-PORTAL-CUST-C"
SESSION_HEADER = "X-Portal-Test-Session"
TEST_EMAIL = "portal-user@example.test"

_PATH_PARAM = re.compile(r"\{[^}]+\}")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@dataclass
class Session:
    key: str
    user_id: str
    claims: Dict[str, Any]

    @property
    def headers(self) -> Dict[str, str]:
        return {SESSION_HEADER: self.key}


class SessionFactory:
    """Mints sessions with unique user ids, so rate-limit buckets never collide."""

    def __init__(self) -> None:
        self.by_key: Dict[str, Session] = {}

    def make(self, claims: Dict[str, Any], *, user_id: Optional[str] = None) -> Session:
        user_id = user_id or f"st-{uuid.uuid4().hex[:12]}"
        key = f"sess-{uuid.uuid4().hex[:12]}"
        full = {"sub": user_id, "has_pii_access": False, **claims}
        session = Session(key=key, user_id=user_id, claims=full)
        self.by_key[key] = session
        return session

    def customer(
        self,
        tenant_id: str,
        customer_id: Optional[str],
        *,
        roles: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> Session:
        claims: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "roles": list(roles) if roles is not None else ["customer"],
        }
        if customer_id is not None:
            claims["customer_id"] = customer_id
        return self.make(claims, user_id=user_id)

    def staff(self, role: str, tenant_id: str = T1) -> Session:
        claims: Dict[str, Any] = {"tenant_id": tenant_id, "roles": [role]}
        if role == "driver":
            claims["driver_id"] = "QA-PORTAL-DRV-1"
        return self.make(claims)

    def staff_bundle(self, tenant_id: str = T1) -> Session:
        from auth.supertokens_init import PLATFORM_STAFF_ROLES

        return self.make({"tenant_id": tenant_id, "roles": list(PLATFORM_STAFF_ROLES)})


class _HeaderVerifier:
    """HTTP ``SessionVerifier`` fake: the session named by the test header."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    async def verify(self, request):
        from ops.middleware.tenant_guard import VerifiedSession

        session = self._factory.by_key.get(request.headers.get(SESSION_HEADER, ""))
        if session is None:
            return None
        return VerifiedSession(user_id=session.user_id, claims=dict(session.claims))


# ---------------------------------------------------------------------------
# Data fakes
# ---------------------------------------------------------------------------


class FakeGrantStore:
    """In-memory ``PortalGrantStore`` keyed (tenant, user, customer)."""

    def __init__(self) -> None:
        self.grants: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.fail: Optional[Exception] = None

    def grant(self, session: Session) -> Dict[str, Any]:
        c = session.claims
        row = {"grant_id": f"pug_{uuid.uuid4().hex[:12]}", "first_seen_at": None}
        self.grants[(c["tenant_id"], session.user_id, c.get("customer_id") or "")] = row
        return row

    def revoke(self, session: Session) -> None:
        c = session.claims
        self.grants.pop((c["tenant_id"], session.user_id, c.get("customer_id") or ""), None)

    async def find_active_grant(self, *, tenant_id, user_id, customer_id):
        self.calls.append(("find_active_grant", {"tenant_id": tenant_id, "user_id": user_id}))
        if self.fail is not None:
            raise self.fail
        row = self.grants.get((tenant_id, user_id, customer_id))
        return dict(row) if row is not None else None

    async def mark_first_seen(self, *, grant_id):
        self.calls.append(("mark_first_seen", {"grant_id": grant_id}))
        for row in self.grants.values():
            if row["grant_id"] == grant_id and row["first_seen_at"] is None:
                row["first_seen_at"] = "now"


class FakeCustomerService:
    def __init__(self) -> None:
        self.customers: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.calls: List[Tuple[str, str]] = []

    def add(self, tenant_id: str, customer_id: str, *, status: str = "active") -> None:
        self.customers[(tenant_id, customer_id)] = {
            "customer_id": customer_id,
            "tenant_id": tenant_id,
            "display_name": f"Customer {customer_id[-1]}",
            "status": status,
        }

    async def get(self, tenant_id: str, customer_id: str) -> Dict[str, Any]:
        from errors.exceptions import resource_not_found

        self.calls.append((tenant_id, customer_id))
        doc = self.customers.get((tenant_id, customer_id))
        if doc is None:
            raise resource_not_found(f"Customer '{customer_id}' not found")
        return dict(doc)


class FakePipeline:
    def __init__(self, state: str = "shadow") -> None:
        self.state = state

    async def get_ordering_state(self, tenant_id: str) -> str:
        return self.state


@dataclass
class PortalFakes:
    grants: FakeGrantStore
    customers: FakeCustomerService
    pipeline: FakePipeline
    invoice_service: Any
    checker: Any
    clock: List[float] = field(default_factory=lambda: [1000.0])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def portal_app():
    from main import app

    return app


@pytest.fixture
def set_flags(monkeypatch):
    """``set_flags(portal=..., backbone=..., invoicing=...)``; clears the cache."""
    from config.settings import clear_settings_cache

    def _set(*, portal: bool = True, backbone: bool = True, invoicing: bool = True) -> None:
        monkeypatch.setenv("CUSTOMER_PORTAL_ENABLED", "true" if portal else "false")
        monkeypatch.setenv("COMMERCE_BACKBONE_ENABLED", "true" if backbone else "false")
        monkeypatch.setenv("COMMERCE_INVOICING_ENABLED", "true" if invoicing else "false")
        clear_settings_cache()

    return _set


@pytest.fixture
def portal_on(set_flags):
    set_flags()


@pytest.fixture
def sessions(portal_app) -> Iterator[SessionFactory]:
    """Install the fake HTTP + WS verifiers for this test, then restore."""
    import bootstrap.websockets as ws
    from auth.test_auth import is_test_auth_bypass_active
    from config.settings import get_settings
    from ops.middleware import tenant_guard

    assert get_settings().auth_provider == "supertokens"
    factory = SessionFactory()

    async def _ws_verify(access_token, anti_csrf):  # noqa: ARG001
        session = factory.by_key.get(access_token or "")
        return dict(session.claims) if session is not None else None

    saved_overrides = dict(portal_app.dependency_overrides)
    portal_app.dependency_overrides.pop(tenant_guard.get_tenant_context, None)
    saved_http = tenant_guard._session_verifier
    saved_ws = ws._ws_session_verifier
    tenant_guard.configure_session_verifier(_HeaderVerifier(factory))
    ws.configure_ws_session_verifier(_ws_verify)
    tenant_guard.configure_tenant_guard(None)
    assert not is_test_auth_bypass_active(portal_app)
    try:
        yield factory
    finally:
        tenant_guard.configure_session_verifier(saved_http)
        ws.configure_ws_session_verifier(saved_ws)
        portal_app.dependency_overrides.clear()
        portal_app.dependency_overrides.update(saved_overrides)


@pytest.fixture
def portal_fakes(monkeypatch) -> Iterator[PortalFakes]:
    """Wire in-memory collaborators through the portal seams; restore after."""
    import portal.api.me_endpoints as me
    from portal.services import principal
    from portal.services import portal_payment_service as pay

    grants = FakeGrantStore()
    customers = FakeCustomerService()
    for tenant_id, customer_id in ((T1, CUSTOMER_A), (T1, CUSTOMER_B), (T2, CUSTOMER_C)):
        customers.add(tenant_id, customer_id)
    pipeline = FakePipeline()
    invoice_service = object()
    clock = [1000.0]
    checker = principal.PortalPrincipalChecker(
        customer_service=customers,
        grant_store=grants,
        cache_seconds=60,
        clock=lambda: clock[0],
    )

    saved_checker = principal.get_principal_checker()
    saved_me = dict(me._services)
    saved_pay = (pay._connector_factory, pay._payment_service)
    principal.configure_portal_principal(checker)
    me.configure_portal_me(
        customer_service=customers,
        invoice_service=invoice_service,
        order_intake_pipeline=pipeline,
    )
    pay.configure_portal_payments(connector_factory=None, payment_service=None)

    async def _email(_user_id: str) -> str:
        return TEST_EMAIL

    import auth.api.account_endpoints as account_endpoints
    import auth.password_admin as password_admin

    monkeypatch.setattr(password_admin, "_email_for_st_user_id", _email)
    monkeypatch.setattr(account_endpoints, "_email_for_st_user_id", _email)
    try:
        yield PortalFakes(grants, customers, pipeline, invoice_service, checker, clock)
    finally:
        principal.configure_portal_principal(saved_checker)
        me._services.clear()
        me._services.update(saved_me)
        pay.configure_portal_payments(*saved_pay)


@pytest.fixture
def client(portal_app) -> TestClient:
    return TestClient(portal_app, raise_server_exceptions=False)


@pytest.fixture
def cA(sessions, portal_fakes) -> Session:
    s = sessions.customer(T1, CUSTOMER_A)
    portal_fakes.grants.grant(s)
    return s


@pytest.fixture
def cB(sessions, portal_fakes) -> Session:
    s = sessions.customer(T1, CUSTOMER_B)
    portal_fakes.grants.grant(s)
    return s


@pytest.fixture
def cC(sessions, portal_fakes) -> Session:
    s = sessions.customer(T2, CUSTOMER_C)
    portal_fakes.grants.grant(s)
    return s


@pytest.fixture
def handler_spy(monkeypatch) -> List[str]:
    """Record the path of every ``APIRoute.handle`` call (i.e. routing reached a handler)."""
    calls: List[str] = []
    original = APIRoute.handle

    async def _spy(self, scope, receive, send):
        calls.append(self.path)
        return await original(self, scope, receive, send)

    monkeypatch.setattr(APIRoute, "handle", _spy)
    return calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fill_path(path: str) -> str:
    """Fill every path parameter with ``x``."""
    return _PATH_PARAM.sub("x", path)


def api_routes(app) -> List[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]


def portal_routes(app) -> List[Tuple[str, APIRoute]]:
    """``(method, route)`` for every ``/api/portal/*`` route."""
    from portal.scope import PORTAL_PATH_PREFIX

    return [
        (method, route)
        for route in api_routes(app)
        if route.path.startswith(PORTAL_PATH_PREFIX)
        for method in sorted(route.methods)
    ]


def portal_admin_routes(app) -> List[Tuple[str, APIRoute]]:
    """``(method, route)`` for every portal-user admin route (FEAT-002+)."""
    pattern = re.compile(r"^/api/commerce/customers/[^/]+/portal-users")
    return [
        (method, route)
        for route in api_routes(app)
        if pattern.match(route.path)
        for method in sorted(route.methods)
    ]


def call(client: TestClient, method: str, path: str, session: Optional[Session], **kw):
    headers = dict(kw.pop("headers", {}) or {})
    if session is not None:
        headers.update(session.headers)
    return client.request(method, path, headers=headers, **kw)


def audit_records(caplog) -> List[Dict[str, Any]]:
    """The ``extra_data`` of every ``portal_audit`` record captured so far."""
    return [
        getattr(r, "extra_data")
        for r in caplog.records
        if r.name == "portal_audit" and hasattr(r, "extra_data")
    ]


def health_container() -> SimpleNamespace:
    """A minimal ``app.state.container`` for ``GET /health`` without lifespan."""

    class _Health:
        async def check_health(self):
            return {"status": "healthy", "timestamp": "2026-10-08T00:00:00Z"}

    return SimpleNamespace(health_check_service=_Health())
