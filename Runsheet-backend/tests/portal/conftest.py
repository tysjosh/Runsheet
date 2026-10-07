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


class FakePortalDB:
    """In-memory ``auth_users`` + ``portal_user_grants`` (FEAT-002).

    ``uow()`` is a :class:`PortalAccessUnitOfWork` factory with transaction
    semantics: work happens on a copy that replaces the state only on a clean
    exit. ``fail_commits = n`` makes the next ``n`` commits raise. The DB is
    also a ``PortalGrantStore`` (joined through ``st_user_id`` like the
    Postgres store), so a principal checker can run over it, and
    :meth:`claims_for` mimics ``_lookup_auth_user_claims``.
    """

    def __init__(self) -> None:
        self.auth_users: Dict[str, Dict[str, Any]] = {}
        self.grants: List[Dict[str, Any]] = []
        self.fail_commits = 0
        self.locks: List[Tuple[str, str]] = []
        self._seq = 0

    # -- seeding / inspection ----------------------------------------------

    def add_user(self, email: str, tenant_id: str, roles=(), **fields: Any) -> Dict[str, Any]:
        row = {
            "email": email,
            "tenant_id": tenant_id,
            "roles": list(roles),
            "has_pii_access": False,
            "driver_id": None,
            "st_user_id": None,
            "customer_id": None,
            **fields,
        }
        self.auth_users[email.casefold()] = row
        return row

    def user(self, email: str) -> Optional[Dict[str, Any]]:
        return self.auth_users.get(email.casefold())

    def active(self, tenant_id: str, customer_id: str) -> List[Dict[str, Any]]:
        return [
            g for g in self.grants
            if g["tenant_id"] == tenant_id and g["customer_id"] == customer_id
            and g["status"] == "active"
        ]

    def claims_for(self, st_user_id: str) -> Dict[str, Any]:
        rows = [r for r in self.auth_users.values() if r.get("st_user_id") == st_user_id]
        if len(rows) != 1:
            return {}
        r = rows[0]
        claims: Dict[str, Any] = {
            "tenant_id": r["tenant_id"],
            "roles": list(r["roles"]),
            "has_pii_access": bool(r["has_pii_access"]),
        }
        if r.get("customer_id"):
            claims["customer_id"] = r["customer_id"]
        return claims

    # -- unit of work --------------------------------------------------------

    def uow(self):
        import copy
        from contextlib import asynccontextmanager

        db = self

        @asynccontextmanager
        async def _scope():
            work = _FakeAccessUoW(db, copy.deepcopy(db.auth_users), copy.deepcopy(db.grants))
            yield work
            if db.fail_commits > 0:
                db.fail_commits -= 1
                raise RuntimeError("simulated commit failure")
            db.auth_users, db.grants = work.auth_users, work.grants

        return _scope()

    # -- PortalGrantStore ------------------------------------------------------

    async def find_active_grant(self, *, tenant_id, user_id, customer_id):
        for row in self.auth_users.values():
            if (row.get("st_user_id") == user_id and row["tenant_id"] == tenant_id
                    and row.get("customer_id") == customer_id):
                for g in self.active(tenant_id, customer_id):
                    if g["email"].casefold() == row["email"].casefold():
                        return {"grant_id": g["grant_id"], "first_seen_at": g["first_seen_at"]}
        return None

    async def mark_first_seen(self, *, grant_id):
        for g in self.grants:
            if g["grant_id"] == grant_id and g["first_seen_at"] is None:
                g["first_seen_at"] = "now"

    def _next_ts(self) -> str:
        self._seq += 1
        return f"2026-10-08T00:00:{self._seq:02d}+00:00"


class _FakeAccessUoW:
    def __init__(self, db: FakePortalDB, auth_users, grants) -> None:
        self._db = db
        self.auth_users = auth_users
        self.grants = grants

    async def lock_customer(self, *, tenant_id, customer_id):
        self._db.locks.append((tenant_id, customer_id))

    async def read_auth_user(self, email):
        row = self.auth_users.get(email.casefold())
        return dict(row) if row is not None else None

    def _match(self, g, tenant_id, customer_id):
        return g["tenant_id"] == tenant_id and g["customer_id"] == customer_id

    async def find_active_grant(self, *, tenant_id, customer_id, email):
        for g in self.grants:
            if (self._match(g, tenant_id, customer_id) and g["status"] == "active"
                    and g["email"].casefold() == email.casefold()):
                return dict(g)
        return None

    async def count_active_grants(self, *, tenant_id, customer_id):
        return sum(1 for g in self.grants if self._match(g, tenant_id, customer_id) and g["status"] == "active")

    async def upsert_customer_user(self, *, email, tenant_id, customer_id):
        row = self.auth_users.setdefault(email.casefold(), {"email": email, "st_user_id": None})
        row.update(tenant_id=tenant_id, roles=["customer"], has_pii_access=False,
                   driver_id=None, customer_id=customer_id)

    async def insert_grant(self, *, grant_id, tenant_id, customer_id, email, created_by):
        from errors.exceptions import portal_email_in_use

        if any(g["tenant_id"] == tenant_id and g["status"] == "active"
               and g["email"].casefold() == email.casefold() for g in self.grants):
            raise portal_email_in_use()
        g = {"grant_id": grant_id, "tenant_id": tenant_id, "customer_id": customer_id,
             "email": email, "status": "active", "first_seen_at": None,
             "created_by": created_by, "created_at": self._db._next_ts(),
             "revoked_by": None, "revoked_at": None}
        self.grants.append(g)
        return dict(g)

    async def list_grants(self, *, tenant_id, customer_id):
        rows = [dict(g) for g in self.grants if self._match(g, tenant_id, customer_id)]
        return sorted(rows, key=lambda g: (g["created_at"], g["grant_id"]), reverse=True)

    async def get_grant(self, *, tenant_id, customer_id, grant_id):
        for g in self.grants:
            if self._match(g, tenant_id, customer_id) and g["grant_id"] == grant_id:
                return dict(g)
        return None

    async def read_customer_user(self, *, email, tenant_id, customer_id):
        row = self.auth_users.get(email.casefold())
        if row and row["tenant_id"] == tenant_id and row.get("customer_id") == customer_id:
            return {"email": row["email"], "st_user_id": row.get("st_user_id")}
        return None

    async def delete_customer_user(self, *, email, tenant_id, customer_id):
        row = self.auth_users.get(email.casefold())
        if (row and row["tenant_id"] == tenant_id and row.get("customer_id") == customer_id
                and row["roles"] == ["customer"]):
            del self.auth_users[email.casefold()]
            return 1
        return 0

    async def mark_grant_revoked(self, *, grant_id, revoked_by):
        for g in self.grants:
            if g["grant_id"] == grant_id:
                g.update(status="revoked", revoked_by=revoked_by, revoked_at="2026-10-09T00:00:00+00:00")

    async def active_grant_user_ids(self, *, tenant_id, customer_id):
        ids = []
        for g in self.grants:
            if self._match(g, tenant_id, customer_id) and g["status"] == "active":
                row = self.auth_users.get(g["email"].casefold())
                if row and row.get("customer_id") == customer_id and row.get("st_user_id"):
                    ids.append(row["st_user_id"])
        return ids

    async def mark_provisioned(self, *, email, st_user_id):
        self.auth_users[email.casefold()]["st_user_id"] = st_user_id

    async def mark_failed(self, *, email, error):
        row = self.auth_users.get(email.casefold())
        if row is not None:
            row["provision_error"] = error


class FakeSuperTokens:
    """In-memory SuperTokens core: the provisioner admin plus the SDK seams.

    ``fail[op] = exc`` makes ``op`` raise (once per assignment if
    ``fail_once``). ``writes()`` lists every mutating call.
    """

    UNKNOWN = "UNKNOWN_USER_ID_ERROR"
    _WRITE_OPS = {"create_user", "set_user_roles", "set_user_metadata",
                  "revoke_sessions", "delete_user", "send_reset_email"}

    def __init__(self, db: FakePortalDB) -> None:
        self._db = db
        self.users: Dict[str, str] = {}  # email (casefold) -> st user id
        self.roles: Dict[str, List[str]] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, Any]] = []
        self.fail: Dict[str, Exception] = {}
        self.unknown_raises = True

    def _call(self, op: str, arg: Any) -> None:
        self.calls.append((op, arg))
        exc = self.fail.pop(op, None)
        if exc is not None:
            raise exc

    def writes(self) -> List[Tuple[str, Any]]:
        return [c for c in self.calls if c[0] in self._WRITE_OPS]

    def known(self, uid: str) -> bool:
        return uid in self.users.values()

    # SuperTokensAdmin
    async def get_user_id_by_email(self, email):
        self._call("get_user_id_by_email", email)
        return self.users.get(email.casefold())

    async def create_user(self, email):
        self._call("create_user", email)
        uid = f"st-{uuid.uuid4().hex[:12]}"
        self.users[email.casefold()] = uid
        return uid

    async def set_user_roles(self, uid, roles):
        self._call("set_user_roles", (uid, list(roles)))
        if not self.known(uid) and self.unknown_raises:
            raise RuntimeError(f"{self.UNKNOWN}: {uid}")
        self.roles[uid] = list(roles)

    async def set_user_metadata(self, uid, metadata):
        self._call("set_user_metadata", (uid, dict(metadata)))
        self.metadata[uid] = dict(metadata)

    # SDK seams
    async def revoke_sessions(self, uid):
        self._call("revoke_sessions", uid)
        if not self.known(uid) and self.unknown_raises:
            raise RuntimeError(f"{self.UNKNOWN}: {uid}")
        return []

    async def delete_user(self, uid):
        self._call("delete_user", uid)
        if not self.known(uid):
            raise RuntimeError(f"{self.UNKNOWN}: {uid}")
        for email, value in list(self.users.items()):
            if value == uid:
                del self.users[email]
        self.roles.pop(uid, None)

    async def create_role(self, role, permissions):
        self._call("create_role", role)

    async def mint_link(self, email, *, tenant_id):
        from auth.password_admin import PasswordAdminError, PasswordSetLink

        self._call("mint_link", email)
        row = self._db.user(email)
        if row is None or row["tenant_id"] != tenant_id:
            raise PasswordAdminError("not_provisioned", "not provisioned")
        uid = self.users.get(email.casefold())
        if uid is None:
            raise PasswordAdminError("no_supertokens_user", "no user")
        return PasswordSetLink(email=email, st_user_id=uid, link=f"https://reset.test/{uid}")

    async def send_reset_email(self, st_tenant_id, uid, email):
        self._call("send_reset_email", uid)
        return "OK" if self.known(uid) else self.UNKNOWN


class FakeTelemetry:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def log_audit_event(self, **kwargs):
        self.events.append(kwargs)


@dataclass
class AccessFakes:
    db: FakePortalDB
    st: FakeSuperTokens
    telemetry: FakeTelemetry
    service: Any

    def outcomes(self, action: str) -> List[str]:
        return [e["details"]["outcome"] for e in self.telemetry.events
                if e["event_type"] == f"portal_user_{action}"]


def make_access_service(customers, db: FakePortalDB, st: FakeSuperTokens, telemetry=None):
    from portal.services.portal_access_service import PortalAccessService

    return PortalAccessService(
        customer_service=customers,
        uow_factory=db.uow,
        supertokens_admin=st,
        session_revoker=st.revoke_sessions,
        user_deleter=st.delete_user,
        role_creator=st.create_role,
        link_minter=st.mint_link,
        reset_emailer=st.send_reset_email,
        telemetry_service=telemetry,
    )


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
def access(portal_fakes) -> Iterator[AccessFakes]:
    """A ``PortalAccessService`` over :class:`FakePortalDB` / :class:`FakeSuperTokens`
    (customers from ``portal_fakes``), installed for the admin routes."""
    from portal.services import portal_access_service as pas

    db = FakePortalDB()
    st = FakeSuperTokens(db)
    telemetry = FakeTelemetry()
    service = make_access_service(portal_fakes.customers, db, st, telemetry)
    saved = pas.get_portal_access_service()
    pas.configure_portal_access(service)
    try:
        yield AccessFakes(db, st, telemetry, service)
    finally:
        pas.configure_portal_access(saved)


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
