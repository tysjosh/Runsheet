"""
Property-based test for storm-mode audit actor identity.

# Feature: supertokens-auth-migration, Property 8: Audit actor identity comes from the verified user

**Validates: Requirements 5.5**

Property 8: Audit actor identity comes from the verified user — the persisted
``actor_id`` on a storm-mode override always equals the verified session's
``user_id`` (``tenant.user_id``), and is never a body-supplied ``actor_id``.

This exercises the storm-mode override handler in
``fuel/api/fuel_ops_endpoints.py`` (``POST /api/fuel/storm-mode/override``)
through the full router wiring with an in-memory ES stub and a dependency
override that injects the verified ``TenantContext``. The
``StormModeOverrideCreateRequest`` model now forbids a client-supplied
``actor_id`` (``model_config = ConfigDict(extra="forbid")``), so the property
holds two ways:

  * with a clean body, the persisted actor equals ``tenant.user_id`` for any
    server-derived user id and any action; and
  * with a body that attempts to spoof ``actor_id`` (or ``tenant_id`` /
    ``override_id``), the request is rejected (422) and nothing is persisted —
    so a body-supplied actor is never written to the audit trail.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Keep importing the fuel-ops module side-effect free (no real ES connection).
sys.modules.setdefault("services.elasticsearch_service", MagicMock())

from errors.exceptions import AppException  # noqa: E402
from fuel.api.fuel_ops_endpoints import (  # noqa: E402
    configure_fuel_ops_endpoints,
    router,
)
from fuel.services.fuel_ops_es_mappings import (  # noqa: E402
    STORM_MODE_OVERRIDES_INDEX,
)
from ops.middleware.tenant_guard import (  # noqa: E402
    TenantContext,
    get_tenant_context,
)


# ---------------------------------------------------------------------------
# Fakes / harness
# ---------------------------------------------------------------------------
class _FakeES:
    """In-memory ES stub capturing ``index_document`` writes.

    The override endpoint only writes, so the stub keeps a flat list so the
    test body can assert on the persisted ``actor_id``.
    """

    def __init__(self) -> None:
        self.writes: List[Dict[str, Any]] = []

    async def index_document(
        self, index: str, doc_id: str, doc: Dict[str, Any]
    ) -> None:
        self.writes.append({"index": index, "doc_id": doc_id, "doc": doc})


def _build_app(
    *, tenant_id: str, user_id: str, roles: List[str]
) -> tuple[FastAPI, _FakeES]:
    """Wire the fuel-ops router with a stub ES and a verified TenantContext.

    The injected ``TenantContext`` is the *only* source of the audit actor —
    it mirrors a verified SuperTokens session whose ``user_id`` claim the
    client cannot influence.
    """
    es = _FakeES()
    configure_fuel_ops_endpoints(es_service=es)

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.to_dict()}
        )

    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id=user_id,
            has_pii_access=False,
            roles=list(roles),
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    app.dependency_overrides[get_tenant_context] = _factory
    return app, es


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Server-derived verified user ids. Must be non-blank after stripping because
# StormModeOverride.actor_id has min_length=1 and strips required strings.
_user_ids = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_\-@.]{0,31}", fullmatch=True)

# Client-supplied spoof actor candidates: deliberately different identities,
# the empty string, whitespace, path-traversal-ish text, and arbitrary text.
_spoof_actors = st.one_of(
    st.just("attacker"),
    st.just("admin"),
    st.just(""),
    st.just("   "),
    st.just("../root"),
    st.text(max_size=48),
    st.from_regex(r"user-[A-Za-z0-9_\-]{1,12}", fullmatch=True),
)

_actions = st.sampled_from(["activate", "deactivate", "snooze", "clear"])
_reasons = st.from_regex(r"[A-Za-z0-9 ]{1,40}", fullmatch=True).filter(
    lambda s: s.strip() != ""
)
_dispatch_roles = st.sampled_from(["dispatcher", "admin"])


# ---------------------------------------------------------------------------
# Property 8a — clean body: persisted actor == verified session user_id
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 8: Audit actor identity comes from the verified user
class TestAuditActorComesFromVerifiedUser:
    """**Validates: Requirements 5.5**"""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        user_id=_user_ids,
        action=_actions,
        reason=_reasons,
        role=_dispatch_roles,
    )
    def test_persisted_actor_equals_session_user_id(
        self, user_id: str, action: str, reason: str, role: str
    ):
        """With a clean request body, the persisted ``actor_id`` always equals
        the verified session ``user_id`` — never anything the client supplied
        (the client supplied no actor at all)."""
        app, es = _build_app(
            tenant_id="tenant-A", user_id=user_id, roles=[role]
        )
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={"action": action, "reason": reason},
        )

        assert resp.status_code == 201, resp.text
        # Response-level actor is the verified user.
        assert resp.json()["actor_id"] == user_id
        # Persisted document actor is the verified user.
        assert len(es.writes) == 1
        assert es.writes[0]["index"] == STORM_MODE_OVERRIDES_INDEX
        assert es.writes[0]["doc"]["actor_id"] == user_id


# ---------------------------------------------------------------------------
# Property 8b — body-supplied actor_id is never honored
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 8: Audit actor identity comes from the verified user
class TestBodySuppliedActorNeverHonored:
    """**Validates: Requirements 5.5**"""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        user_id=_user_ids,
        spoof_actor=_spoof_actors,
        action=_actions,
        reason=_reasons,
        role=_dispatch_roles,
    )
    def test_spoofed_actor_id_in_body_is_rejected_and_not_persisted(
        self,
        user_id: str,
        spoof_actor: str,
        action: str,
        reason: str,
        role: str,
    ):
        """A request that tries to supply ``actor_id`` in the body is rejected
        by the ``extra="forbid"`` request model (HTTP 422) and nothing is
        persisted, so a body-supplied actor never reaches the audit trail.

        Even in the (impossible-by-construction) event that a write occurs, the
        persisted actor must equal the verified session ``user_id`` and never
        the spoofed value."""
        app, es = _build_app(
            tenant_id="tenant-A", user_id=user_id, roles=[role]
        )
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/storm-mode/override",
            json={
                "action": action,
                "reason": reason,
                "actor_id": spoof_actor,
            },
        )

        # The request model forbids a client-supplied actor_id.
        assert resp.status_code == 422, resp.text
        # Nothing was persisted — the spoofed actor never reached ES.
        assert es.writes == []
        # Defense-in-depth: no persisted doc carries the spoofed actor.
        for write in es.writes:  # pragma: no cover - empty by assertion above
            assert write["doc"]["actor_id"] == user_id
            assert write["doc"]["actor_id"] != spoof_actor


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
