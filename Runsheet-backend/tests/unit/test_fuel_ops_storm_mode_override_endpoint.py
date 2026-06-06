"""
Unit tests for Task 10.5 of the fuel-ops-hardening spec:

* ``POST /api/fuel/storm-mode/override`` — persist a dispatcher or
  admin Storm_Mode override to the ``storm_mode_overrides`` ES index
  (Req 9.4.2, 9.4.4).

The tests exercise the full router wiring (
:func:`configure_fuel_ops_endpoints` → ``storm_mode_overrides`` ES
index) with an in-memory ES stub so the persisted document shape can
be verified without a live cluster.

Validates: Requirements 9.4.2, 9.4.4.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    router,
)
from fuel.services.fuel_ops_es_mappings import STORM_MODE_OVERRIDES_INDEX
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeES:
    """In-memory ES stub capturing ``index_document`` calls.

    The override endpoint only calls ``index_document`` — no reads —
    so the stub keeps a flat list of writes so the test body can
    assert on the persisted shape.
    """

    def __init__(self) -> None:
        self.writes: List[Dict[str, Any]] = []
        self.raise_on_index: bool = False

    async def index_document(
        self, index: str, doc_id: str, doc: Dict[str, Any]
    ) -> None:
        if self.raise_on_index:
            raise RuntimeError("boom")
        self.writes.append({"index": index, "doc_id": doc_id, "doc": doc})


def _tenant_ctx_factory(
    *,
    tenant_id: str = "tenant-A",
    user_id: str = "user-1",
    roles: Optional[List[str]] = None,
):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id=user_id,
            has_pii_access=False,
            roles=list(roles if roles is not None else ["dispatcher"]),
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    *,
    tenant_id: str = "tenant-A",
    roles: Optional[List[str]] = None,
) -> tuple[FastAPI, _FakeES]:
    es = _FakeES()
    configure_fuel_ops_endpoints(es_service=es)

    app = FastAPI()
    app.include_router(router)

    # The shared Role_Authorizer raises AppException; register the same
    # structured handler the app uses in production so the role-gate
    # response renders as the canonical error envelope (nested under
    # ``detail`` to match the other error-mode assertions in this module).
    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.to_dict()}
        )

    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id, roles=roles
    )
    return app, es


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestStormModeOverrideEndpoint:
    def test_dispatcher_can_submit_activate_override(self):
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)

        expires = (
            datetime.now(timezone.utc) + timedelta(hours=6)
        ).isoformat()
        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "hurricane approaching",
                "expires_at": expires,
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["action"] == "activate"
        assert body["reason"] == "hurricane approaching"
        # actor_id is derived server-side from the verified session
        # (tenant.user_id), never from the request body (Req 5.5).
        assert body["actor_id"] == "user-1"
        assert body["tenant_id"] == "tenant-A"
        assert body["override_id"].startswith("smo_")
        assert body["expires_at"] is not None
        assert body["created_at"] is not None
        assert body["updated_at"] is not None

        # Persistence: exactly one write to the right index with the same
        # override_id as the response payload.
        assert len(es.writes) == 1
        write = es.writes[0]
        assert write["index"] == STORM_MODE_OVERRIDES_INDEX
        assert write["doc_id"] == body["override_id"]
        assert write["doc"]["tenant_id"] == "tenant-A"
        assert write["doc"]["action"] == "activate"
        assert write["doc"]["reason"] == "hurricane approaching"

    def test_admin_can_submit_deactivate_override(self):
        app, es = _build_app(roles=["admin"])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "deactivate",
                "reason": "storm cleared early",
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["action"] == "deactivate"
        assert body["actor_id"] == "user-1"
        assert body["expires_at"] is None
        assert len(es.writes) == 1

    def test_accepts_all_valid_actions(self):
        for action in ("activate", "deactivate", "snooze", "clear"):
            app, es = _build_app(roles=["dispatcher"])
            client = TestClient(app)
            resp = client.post(
                "/api/fuel/storm-mode/override",
                json={
                    "action": action,
                    "reason": "reason",
                },
            )
            assert resp.status_code == 201, f"{action}: {resp.text}"
            assert resp.json()["action"] == action

    def test_tenant_id_is_stamped_from_jwt_not_body(self):
        """The router ignores any tenant_id in the body (the model forbids
        it) and pulls it exclusively from the JWT context.
        """
        app, es = _build_app(tenant_id="tenant-X", roles=["dispatcher"])
        client = TestClient(app)

        # Try to spoof the tenant_id in the body — extra=forbid on the
        # request model rejects it.
        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "spoof attempt",
                "tenant_id": "tenant-EVIL",
            },
        )
        assert resp.status_code == 422

        # Clean payload — tenant_id comes from the JWT context.
        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "legit",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["tenant_id"] == "tenant-X"
        assert es.writes[0]["doc"]["tenant_id"] == "tenant-X"


# ---------------------------------------------------------------------------
# Role-restriction tests (Req 9.4.4)
# ---------------------------------------------------------------------------


class TestStormModeOverrideRoleGate:
    def test_rejects_caller_without_dispatcher_or_admin_role(self):
        app, es = _build_app(roles=["driver"])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "reason",
            },
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_code"] == "INSUFFICIENT_ROLE"
        assert es.writes == []

    def test_rejects_caller_with_empty_roles(self):
        app, es = _build_app(roles=[])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "reason",
            },
        )
        assert resp.status_code == 403
        assert es.writes == []

    def test_rejects_compound_role_names(self):
        """Exact-match security fix (Req 4.2): a tenant lexicon role like
        ``dispatcher_lead`` no longer satisfies the ``dispatcher`` gate.

        Previously the substring matcher accepted any role *containing*
        ``dispatcher``/``admin``; the shared Role_Authorizer requires exact
        membership, so a superstring role is now correctly rejected with
        HTTP 403 and no write is persisted.
        """
        app, es = _build_app(roles=["dispatcher_lead"])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "snooze",
                "reason": "reason",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"
        assert es.writes == []


# ---------------------------------------------------------------------------
# Validation tests (Req 9.4.2)
# ---------------------------------------------------------------------------


class TestStormModeOverrideValidation:
    def test_rejects_blank_reason(self):
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "",
            },
        )
        assert resp.status_code == 422
        assert es.writes == []

    def test_rejects_client_supplied_actor_id(self):
        """actor_id is server-derived from the verified session and is not
        an accepted request field; supplying one is rejected by the
        ``extra=forbid`` request model (Req 5.5).
        """
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "reason",
                "actor_id": "spoofed-actor",
            },
        )
        assert resp.status_code == 422
        assert es.writes == []

    def test_rejects_unknown_action(self):
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "pause",
                "reason": "reason",
            },
        )
        assert resp.status_code == 422
        assert es.writes == []


# ---------------------------------------------------------------------------
# Persistence-failure tests
# ---------------------------------------------------------------------------


class TestStormModeOverridePersistenceFailure:
    def test_surfaces_es_failure_as_503(self):
        app, es = _build_app(roles=["dispatcher"])
        es.raise_on_index = True
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "reason",
            },
        )
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["error_code"] == "storm_mode_override_persistence_failed"
