"""The agent control surface enforces a role, and the right one per route.

The regression: exactly one of the fourteen routes in ``agent_endpoints.py`` had a
role check — ``PATCH /config/autonomy``, via an inline
``if "admin" not in tenant.roles``. Verified against a running server before this
gate, the ``driver`` account got HTTP 200 from ``GET /api/agent/health``,
``GET /api/agent/approvals``, ``GET /api/agent/config/autonomy``, and — the one
that matters — ``POST /api/agent/{agent_id}/pause``, which stopped a live
autonomous agent.

Two audiences, one router, and the split is the thing worth pinning:

* **Reads and the approval queue: admin + dispatcher.** Agents *propose* work; the
  human-in-the-loop who accepts or rejects is the dispatcher running the shift.
  Three non-admin surfaces already depend on the reads (``DispatchCockpit`` on
  ``/dashboard``, ``NotificationBell`` in the header, ``OperationsControlView`` on
  ``/ops/control``), and both owning nav items carry
  ``["admin", "dispatcher"]``.
* **Policy and lifecycle: admin.** Autonomy level, agent pause/resume, and memory
  deletion are tenant-wide and outlive a shift.

Making approve/reject admin-only would leave dispatchers holding a queue they
cannot action, which defeats the human-in-the-loop design instead of securing it.
That asymmetry is why the admin set is pinned by path below rather than left to
whoever edits the router next.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

import agent_endpoints
from Agents.api_authz import (
    AGENT_ADMIN_ROLES,
    AGENT_OPS_ROLES,
    agent_admin_dependency,
    agent_ops_dependency,
)
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

OPS_GATE = "require_roles_admin_or_dispatcher"
ADMIN_GATE = "require_roles_admin"

#: Routes that change tenant-wide agent policy or lifecycle. Pinned as data so
#: adding a privileged route without deciding its audience fails here.
EXPECTED_ADMIN_ONLY: set[tuple[str, str]] = {
    ("PATCH", "/api/agent/config/autonomy"),
    ("DELETE", "/api/agent/memory/{memory_id}"),
    ("POST", "/api/agent/{agent_id}/pause"),
    ("POST", "/api/agent/{agent_id}/resume"),
}


def _ctx(*roles: str) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=list(roles),
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


def _gate_names(dependant) -> set[str]:
    """Every role-gate name reachable from a route's dependency tree."""
    found: set[str] = set()
    for sub in dependant.dependencies:
        name = getattr(sub.call, "__name__", "")
        if name.startswith("require_roles"):
            found.add(name)
        found |= _gate_names(sub)
    return found


def _routes() -> list:
    return [r for r in agent_endpoints.router.routes if hasattr(r, "dependant")]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestAgentPolicy:
    def test_ops_audience_is_admin_and_dispatcher(self) -> None:
        assert AGENT_OPS_ROLES == ("admin", "dispatcher")

    def test_policy_audience_is_admin_only(self) -> None:
        assert AGENT_ADMIN_ROLES == ("admin",)

    def test_admin_is_a_subset_of_ops(self) -> None:
        # The admin gate is layered on top of the router-level ops gate rather
        # than replacing it, so an admin must satisfy both to reach a policy
        # route. If these ever diverge, admins lock themselves out.
        assert set(AGENT_ADMIN_ROLES) <= set(AGENT_OPS_ROLES)

    def test_driver_is_in_neither_audience(self) -> None:
        assert "driver" not in AGENT_OPS_ROLES
        assert "driver" not in AGENT_ADMIN_ROLES


# ---------------------------------------------------------------------------
# Behaviour, through HTTP
# ---------------------------------------------------------------------------


def _app_with(dependency, *roles: str) -> TestClient:
    """A minimal app carrying only the gate, so nothing else explains a 403."""
    app = FastAPI()
    # The gate raises AppException; without the handlers it propagates as an
    # exception instead of becoming a 403 and the assertions check the wrong thing.
    register_exception_handlers(app)

    router = APIRouter(dependencies=[Depends(dependency)])

    @router.get("/probe")
    async def _probe() -> dict:
        return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: _ctx(*roles)
    return TestClient(app)


class TestOpsGateBehaviour:
    @pytest.mark.parametrize("role", ["admin", "dispatcher"])
    def test_operations_roles_allowed(self, role: str) -> None:
        assert _app_with(agent_ops_dependency, role).get("/probe").status_code == 200

    def test_driver_is_refused(self) -> None:
        # The account that paused a live autonomous agent before this gate.
        assert _app_with(agent_ops_dependency, "driver").get("/probe").status_code == 403

    def test_no_roles_is_refused(self) -> None:
        assert _app_with(agent_ops_dependency).get("/probe").status_code == 403

    @pytest.mark.parametrize("held", ["admin_ops", "lead-dispatcher", "dispatch"])
    def test_substring_neighbours_are_refused(self, held: str) -> None:
        assert _app_with(agent_ops_dependency, held).get("/probe").status_code == 403

    def test_rejection_does_not_echo_held_roles(self) -> None:
        response = _app_with(agent_ops_dependency, "some-internal-role").get("/probe")
        assert response.status_code == 403
        assert "some-internal-role" not in response.text


class TestAdminGateBehaviour:
    def test_admin_allowed(self) -> None:
        assert _app_with(agent_admin_dependency, "admin").get("/probe").status_code == 200

    def test_dispatcher_is_refused(self) -> None:
        # A dispatcher works the approval queue but does not set tenant policy.
        assert (
            _app_with(agent_admin_dependency, "dispatcher").get("/probe").status_code
            == 403
        )

    def test_driver_is_refused(self) -> None:
        assert (
            _app_with(agent_admin_dependency, "driver").get("/probe").status_code == 403
        )

    def test_refusal_is_403_so_the_ui_keeps_working(self) -> None:
        # AgentSettingsPage branches on `err.status === 403` to show its
        # "Admin access required" notice without mutating the displayed level
        # (Req 3.5). The gate replaced an inline check that raised `forbidden`;
        # this pins that the swap kept the status code.
        assert (
            _app_with(agent_admin_dependency, "dispatcher").get("/probe").status_code
            == 403
        )


# ---------------------------------------------------------------------------
# Drift guard — derived from the router, not a hand-maintained list
# ---------------------------------------------------------------------------


class TestNoAgentRouteIsUngated:
    def test_discovers_the_routes(self) -> None:
        # Guards the guard: an empty route list makes everything below vacuous.
        assert len(_routes()) >= 14, [r.path for r in _routes()]

    def test_router_carries_the_ops_gate(self) -> None:
        names = [getattr(d.dependency, "__name__", "") for d in agent_endpoints.router.dependencies]
        assert OPS_GATE in names, (
            "the agent router lost its router-level role gate; every route "
            "would default to ungated again"
        )

    def test_every_route_is_gated(self) -> None:
        ungated = [
            f"{sorted(r.methods)} {r.path}"
            for r in _routes()
            if not _gate_names(r.dependant)
        ]
        assert ungated == [], f"agent routes with no role gate: {ungated}"

    def test_every_route_inherits_the_ops_gate(self) -> None:
        missing = [
            f"{sorted(r.methods)} {r.path}"
            for r in _routes()
            if OPS_GATE not in _gate_names(r.dependant)
        ]
        assert missing == [], f"agent routes missing the ops gate: {missing}"

    def test_admin_only_routes_are_exactly_the_policy_routes(self) -> None:
        actual = {
            (method, r.path)
            for r in _routes()
            for method in r.methods
            if ADMIN_GATE in _gate_names(r.dependant)
        }
        assert actual == EXPECTED_ADMIN_ONLY, (
            "the admin-only set drifted. Added a tenant-wide agent control? Add "
            "it to EXPECTED_ADMIN_ONLY. Widened one to dispatchers? Removing it "
            f"here is the decision.\n  gained: {sorted(actual - EXPECTED_ADMIN_ONLY)}"
            f"\n  lost:   {sorted(EXPECTED_ADMIN_ONLY - actual)}"
        )

    def test_approval_queue_is_not_admin_only(self) -> None:
        # ApprovalQueuePanel renders approve/reject inside OperationsControlView,
        # an admin+dispatcher surface. Admin-only here would show dispatchers a
        # queue they cannot action.
        queue = [
            (method, r.path)
            for r in _routes()
            for method in r.methods
            if "/approvals/" in r.path
        ]
        assert queue, "expected approve/reject routes to exist"
        for method, path in queue:
            assert (method, path) not in EXPECTED_ADMIN_ONLY, (
                f"{method} {path} is admin-only, which strands the dispatcher's "
                "approval queue"
            )

    def test_no_inline_role_check_remains(self) -> None:
        # The autonomy handler used to hand-roll `if "admin" not in tenant.roles`.
        # One mechanism, so a reviewer has one place to look.
        #
        # Matched on the AST rather than the text: a substring search also hits
        # the docstring that explains the removal, which made this pass/fail on
        # prose instead of on code.
        tree = ast.parse(Path(agent_endpoints.__file__).read_text(encoding="utf-8"))
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
            )
            and any(
                isinstance(cmp, ast.Attribute) and cmp.attr == "roles"
                for cmp in node.comparators
            )
        ]
        assert offenders == [], (
            "an inline role check reappeared in agent_endpoints.py at line(s) "
            f"{offenders}; use roles_dependency so every router shares one "
            "mechanism"
        )
