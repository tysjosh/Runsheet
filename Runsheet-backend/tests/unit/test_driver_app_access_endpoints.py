"""
Unit tests for the App_Access_Service operations on
:mod:`fuel.api.driver_endpoints`.

Task 4.6 of the driver-mobile-app spec. Exercises
``POST/DELETE /api/ops/drivers/{driver_id}/app-access`` against an in-memory
``auth_users`` unit of work (with commit / rollback semantics) and an in-memory
SuperTokens admin, so the real ``auth.provisioner.provision_user`` path runs
without the SDK or a database.

Covers:
* admin gate — dispatcher / driver callers are rejected (Req 1.23)
* unknown ``driver_id`` → 404 with no SuperTokens write (Req 1.24)
* grant creates the user, adds the ``driver`` role, links ``driver_id``
  (Req 1.17) and reports ``created`` / ``updated`` (Req 1.21)
* repeating a grant is idempotent — one SuperTokens user, mapping unchanged
  (Req 1.20)
* a ``driver_id`` already linked to another email → 409 (Req 1.17)
* a failure mid-grant leaves no link and removes the ``driver`` role again
  (Req 1.18)
* revoke removes the role, clears the link, revokes sessions, and leaves
  ``drivers_current`` in place (Req 1.25)

Validates: Requirements 1.17, 1.18, 1.19, 1.20, 1.21, 1.22, 1.23, 1.24, 1.25,
1.26.
"""
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from fuel.api.driver_endpoints import configure_driver_endpoints, router
from fuel.order_models import Driver
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDriverRepository:
    """Minimal ``drivers_current`` read surface."""

    def __init__(self) -> None:
        self._drivers: Dict[Tuple[str, str], Driver] = {}

    def seed(self, driver: Driver) -> None:
        self._drivers[(driver.tenant_id, driver.driver_id)] = driver

    async def get(self, tenant_id: str, driver_id: str) -> Optional[Driver]:
        return self._drivers.get((tenant_id, driver_id))

    def count(self) -> int:
        return len(self._drivers)


class FakeAuthUsersUnitOfWork:
    """In-memory ``AppAccessUnitOfWork`` over a staged row dict."""

    def __init__(self, rows: Dict[str, Dict[str, Any]]) -> None:
        self.rows = rows

    @staticmethod
    def _key(email: str) -> str:
        return (email or "").strip().casefold()

    async def email_linked_to_driver(
        self, *, tenant_id: str, driver_id: str
    ) -> Optional[str]:
        for row in self.rows.values():
            if row.get("tenant_id") == tenant_id and row.get("driver_id") == driver_id:
                return row["email"]
        return None

    async def read_row(self, email: str) -> Optional[Dict[str, Any]]:
        row = self.rows.get(self._key(email))
        return copy.deepcopy(row) if row is not None else None

    async def upsert_app_access(
        self,
        *,
        email: str,
        tenant_id: str,
        driver_id: str,
        roles: Sequence[str],
        has_pii_access: bool,
    ) -> None:
        key = self._key(email)
        row = self.rows.setdefault(key, {"email": email, "st_user_id": None})
        row.update(
            {
                "email": email,
                "tenant_id": tenant_id,
                "driver_id": driver_id,
                "roles": list(roles),
                "has_pii_access": bool(has_pii_access),
            }
        )

    async def clear_app_access(self, *, email: str, roles: Sequence[str]) -> None:
        row = self.rows.get(self._key(email))
        if row is not None:
            row["roles"] = list(roles)
            row["driver_id"] = None

    async def mark_provisioned(self, *, email: str, st_user_id: str) -> None:
        row = self.rows.get(self._key(email))
        if row is not None:
            row["st_user_id"] = st_user_id
            row["provision_error"] = None

    async def mark_failed(self, *, email: str, error: str) -> None:
        row = self.rows.get(self._key(email))
        if row is not None:
            row["provision_error"] = error


class FakeAuthUsersDB:
    """Committed ``auth_users`` state plus a transactional UoW factory."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    def seed(self, **row: Any) -> None:
        self.rows[row["email"].strip().casefold()] = dict(row)

    def row_for(self, email: str) -> Optional[Dict[str, Any]]:
        return self.rows.get(email.strip().casefold())

    def uow_factory(self):
        db = self

        @asynccontextmanager
        async def factory():
            uow = FakeAuthUsersUnitOfWork(copy.deepcopy(db.rows))
            yield uow
            # Commit last: staged rows are discarded when the body raises.
            db.rows = uow.rows

        return factory


class FakeSuperTokensAdmin:
    """In-memory stand-in for the managed SuperTokens core."""

    def __init__(self, *, fail_on_metadata: bool = False) -> None:
        self.users: Dict[str, str] = {}  # email.casefold() -> user id
        self.roles: Dict[str, List[str]] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}
        self.create_calls: List[str] = []
        self.fail_on_metadata = fail_on_metadata

    def seed_user(self, email: str, user_id: str, roles: Sequence[str]) -> None:
        self.users[email.casefold()] = user_id
        self.roles[user_id] = list(roles)

    async def get_user_id_by_email(self, email: str) -> Optional[str]:
        return self.users.get(email.casefold())

    async def create_user(self, email: str) -> str:
        self.create_calls.append(email)
        user_id = f"st-{len(self.users) + 1}"
        self.users[email.casefold()] = user_id
        self.roles[user_id] = []
        return user_id

    async def set_user_roles(self, user_id: str, roles: Sequence[str]) -> None:
        self.roles[user_id] = list(roles)

    async def set_user_metadata(
        self, user_id: str, metadata: Dict[str, Any]
    ) -> None:
        if self.fail_on_metadata:
            raise RuntimeError("supertokens metadata write failed")
        self.metadata[user_id] = dict(metadata)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx(
    tenant_id: str = "tenant-A",
    roles: Optional[List[str]] = None,
    user_id: str = "admin-1",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        has_pii_access=False,
        roles=roles if roles is not None else ["admin"],
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


def _make_driver(driver_id: str = "drv-001", tenant_id: str = "tenant-A") -> Driver:
    return Driver(
        driver_id=driver_id,
        tenant_id=tenant_id,
        driver_name="Test Driver",
        status="active",
        last_event_timestamp=_NOW,
        source_schema_version="1.0",
        trace_id=f"drv_{driver_id}",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _build_client(
    *,
    roles: Optional[List[str]] = None,
    repo: Optional[FakeDriverRepository] = None,
    db: Optional[FakeAuthUsersDB] = None,
    admin: Optional[FakeSuperTokensAdmin] = None,
    revoked: Optional[List[str]] = None,
):
    repo = repo if repo is not None else FakeDriverRepository()
    db = db if db is not None else FakeAuthUsersDB()
    admin = admin if admin is not None else FakeSuperTokensAdmin()
    revoked = revoked if revoked is not None else []

    async def _revoker(st_user_id: str) -> None:
        revoked.append(st_user_id)

    configure_driver_endpoints(
        driver_repository=repo,
        app_access_uow_factory=db.uow_factory(),
        supertokens_admin=admin,
        session_revoker=_revoker,
    )

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _handler(request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_dict()})

    app.dependency_overrides[get_tenant_context] = lambda: _tenant_ctx(roles=roles)
    return TestClient(app), repo, db, admin, revoked


# ---------------------------------------------------------------------------
# Authorization + preconditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("roles", [["dispatcher"], ["driver"], []])
def test_non_admin_grant_is_rejected(roles):
    """Only admins may grant app access (Req 1.23)."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    client, _, db, admin, _ = _build_client(roles=roles, repo=repo)

    resp = client.post(
        "/api/ops/drivers/drv-001/app-access", json={"email": "d@example.com"}
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"
    assert db.rows == {}
    assert admin.create_calls == []


def test_non_admin_revoke_is_rejected():
    """Only admins may revoke app access (Req 1.23)."""
    client, _, _, _, _ = _build_client(roles=["dispatcher"])

    resp = client.delete("/api/ops/drivers/drv-001/app-access")

    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"


def test_unknown_driver_returns_404_with_no_supertokens_write():
    """No ``drivers_current`` record → 404 and no provisioning (Req 1.24)."""
    client, _, db, admin, _ = _build_client()

    resp = client.post(
        "/api/ops/drivers/ghost/app-access", json={"email": "d@example.com"}
    )

    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "RESOURCE_NOT_FOUND"
    assert db.rows == {}
    assert admin.create_calls == []


def test_cross_tenant_driver_returns_404():
    """A driver in another tenant is not visible to the caller (Req 1.24)."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver(driver_id="drv-b1", tenant_id="tenant-B"))
    client, _, db, admin, _ = _build_client(repo=repo)

    resp = client.post(
        "/api/ops/drivers/drv-b1/app-access", json={"email": "d@example.com"}
    )

    assert resp.status_code == 404
    assert db.rows == {}
    assert admin.create_calls == []


# ---------------------------------------------------------------------------
# Grant
# ---------------------------------------------------------------------------


def test_grant_creates_user_role_and_link():
    """Grant provisions the user, adds ``driver``, links ``driver_id``."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    client, _, db, admin, _ = _build_client(repo=repo)

    resp = client.post(
        "/api/ops/drivers/drv-001/app-access",
        json={"email": "Driver@Example.com", "has_pii_access": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["provision_status"] == "created"
    assert body["driver_id"] == "drv-001"
    assert body["tenant_id"] == "tenant-A"
    assert body["st_user_id"] == admin.users["driver@example.com"]

    row = db.row_for("driver@example.com")
    assert row["driver_id"] == "drv-001"
    assert row["tenant_id"] == "tenant-A"
    assert row["has_pii_access"] is True
    assert "driver" in row["roles"]
    assert row["st_user_id"] == body["st_user_id"]

    st_user_id = body["st_user_id"]
    assert admin.roles[st_user_id] == ["driver"]
    assert admin.metadata[st_user_id]["driver_id"] == "drv-001"


def test_grant_for_existing_user_reports_updated_and_keeps_other_roles():
    """An existing SuperTokens user is reused and its roles are preserved."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    db = FakeAuthUsersDB()
    db.seed(
        email="ops@example.com",
        tenant_id="tenant-A",
        roles=["dispatcher"],
        has_pii_access=False,
        driver_id=None,
        st_user_id="st-existing",
    )
    admin = FakeSuperTokensAdmin()
    admin.seed_user("ops@example.com", "st-existing", ["dispatcher"])

    client, _, db, admin, _ = _build_client(repo=repo, db=db, admin=admin)

    resp = client.post(
        "/api/ops/drivers/drv-001/app-access", json={"email": "ops@example.com"}
    )

    assert resp.status_code == 200
    assert resp.json()["provision_status"] == "updated"
    assert admin.create_calls == []
    assert admin.roles["st-existing"] == ["dispatcher", "driver"]
    assert db.row_for("ops@example.com")["roles"] == ["dispatcher", "driver"]


def test_repeated_grant_is_idempotent():
    """Repeating the grant returns 200 with the mapping unchanged (Req 1.20)."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    client, _, db, admin, _ = _build_client(repo=repo)

    first = client.post(
        "/api/ops/drivers/drv-001/app-access", json={"email": "d@example.com"}
    )
    snapshot = copy.deepcopy(db.row_for("d@example.com"))

    second = client.post(
        "/api/ops/drivers/drv-001/app-access", json={"email": "d@example.com"}
    )

    assert first.json()["provision_status"] == "created"
    assert second.status_code == 200
    assert second.json()["provision_status"] == "updated"
    assert second.json()["st_user_id"] == first.json()["st_user_id"]
    assert len(admin.create_calls) == 1
    assert db.row_for("d@example.com") == snapshot


def test_grant_rejects_driver_already_linked_to_another_email():
    """A ``driver_id`` linked elsewhere → 409 with no SuperTokens write."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    db = FakeAuthUsersDB()
    db.seed(
        email="first@example.com",
        tenant_id="tenant-A",
        roles=["driver"],
        has_pii_access=False,
        driver_id="drv-001",
        st_user_id="st-1",
    )
    client, _, db, admin, _ = _build_client(repo=repo, db=db)

    resp = client.post(
        "/api/ops/drivers/drv-001/app-access", json={"email": "second@example.com"}
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "APP_ACCESS_ALREADY_LINKED"
    assert db.row_for("second@example.com") is None
    assert db.row_for("first@example.com")["driver_id"] == "drv-001"
    assert admin.create_calls == []


def test_failed_grant_leaves_no_link_and_removes_the_driver_role():
    """A mid-grant failure rolls back and compensates (Req 1.18)."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    admin = FakeSuperTokensAdmin(fail_on_metadata=True)
    client, _, db, admin, _ = _build_client(repo=repo, admin=admin)

    with pytest.raises(RuntimeError):
        client.post(
            "/api/ops/drivers/drv-001/app-access", json={"email": "d@example.com"}
        )

    # No observable link was committed…
    assert db.row_for("d@example.com") is None
    # …and the role the attempt added was taken back.
    st_user_id = admin.users["d@example.com"]
    assert admin.roles[st_user_id] == []


def test_failed_grant_keeps_a_pre_existing_driver_role():
    """Compensation never removes a role the user already held (Req 1.18)."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    db = FakeAuthUsersDB()
    db.seed(
        email="d@example.com",
        tenant_id="tenant-A",
        roles=["driver"],
        has_pii_access=False,
        driver_id="drv-999",
        st_user_id="st-1",
    )
    admin = FakeSuperTokensAdmin(fail_on_metadata=True)
    admin.seed_user("d@example.com", "st-1", ["driver"])
    client, _, db, admin, _ = _build_client(repo=repo, db=db, admin=admin)

    with pytest.raises(RuntimeError):
        client.post(
            "/api/ops/drivers/drv-001/app-access", json={"email": "d@example.com"}
        )

    assert db.row_for("d@example.com")["driver_id"] == "drv-999"
    assert admin.roles["st-1"] == ["driver"]


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


def test_revoke_removes_role_clears_link_and_revokes_sessions():
    """Revoke reverses the grant and leaves ``drivers_current`` alone (Req 1.25)."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    db = FakeAuthUsersDB()
    db.seed(
        email="d@example.com",
        tenant_id="tenant-A",
        roles=["dispatcher", "driver"],
        has_pii_access=False,
        driver_id="drv-001",
        st_user_id="st-1",
    )
    admin = FakeSuperTokensAdmin()
    admin.seed_user("d@example.com", "st-1", ["dispatcher", "driver"])
    client, repo, db, admin, revoked = _build_client(repo=repo, db=db, admin=admin)

    resp = client.delete("/api/ops/drivers/drv-001/app-access")

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "d@example.com"
    assert body["st_user_id"] == "st-1"
    assert body["provision_status"] == "updated"

    row = db.row_for("d@example.com")
    assert row["driver_id"] is None
    assert row["roles"] == ["dispatcher"]
    assert admin.roles["st-1"] == ["dispatcher"]
    assert revoked == ["st-1"]
    # drivers_current is untouched.
    assert repo.count() == 1


def test_revoke_without_a_link_returns_404():
    """Nothing linked → 404 ``RESOURCE_NOT_FOUND``."""
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    client, _, _, _, revoked = _build_client(repo=repo)

    resp = client.delete("/api/ops/drivers/drv-001/app-access")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "RESOURCE_NOT_FOUND"
    assert revoked == []
