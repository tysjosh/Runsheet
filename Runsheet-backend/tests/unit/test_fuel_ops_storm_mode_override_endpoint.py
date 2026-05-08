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
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
                "actor_id": "dispatcher-42",
                "expires_at": expires,
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["action"] == "activate"
        assert body["reason"] == "hurricane approaching"
        assert body["actor_id"] == "dispatcher-42"
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
                "actor_id": "admin-ops-1",
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["action"] == "deactivate"
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
                    "actor_id": "actor",
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
                "actor_id": "actor",
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
                "actor_id": "actor",
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
                "actor_id": "actor",
            },
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_code"] == "forbidden_role"
        assert es.writes == []

    def test_rejects_caller_with_empty_roles(self):
        app, es = _build_app(roles=[])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "reason",
                "actor_id": "actor",
            },
        )
        assert resp.status_code == 403
        assert es.writes == []

    def test_accepts_compound_role_names(self):
        """Tenants with lexicons like ``dispatcher_lead`` still pass."""
        app, es = _build_app(roles=["dispatcher_lead"])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "snooze",
                "reason": "reason",
                "actor_id": "actor",
            },
        )
        assert resp.status_code == 201
        assert len(es.writes) == 1


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
                "actor_id": "actor",
            },
        )
        assert resp.status_code == 422
        assert es.writes == []

    def test_rejects_blank_actor_id(self):
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": "activate",
                "reason": "reason",
                "actor_id": "",
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
                "actor_id": "actor",
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
                "actor_id": "actor",
            },
        )
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["error_code"] == "storm_mode_override_persistence_failed"
