"""Shared wiring for the loading-plan integration tests (FEAT-004).

The real ``agent_endpoints.router`` (with ``register_exception_handlers`` and
the router-level ``agent_ops_dependency``) over the real approval service,
confirmation protocol, executor, order repository and order service from
``tests.unit._loading_plan_fakes.ApprovalHarness``, all on one
``InMemoryDocStore``. The activity log is the real ``ActivityLogService`` and
the WS manager is a real ``AgentActivityWSManager`` with one fake socket per
tenant. The approval comes from ``CompartmentLoadingAgent.evaluate`` plus
``_route_proposal(proposal, "active_gated")``, as in production.

Requests go through ``httpx.ASGITransport`` on the test's own event loop, so
the in-process plan lock and the WS manager lock stay on one loop. Nothing
touches Redis, Postgres or an LLM.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx
from fastapi import FastAPI

import agent_endpoints
from Agents.activity_log_service import ActivityLogService
from Agents.agent_ws_manager import AgentActivityWSManager
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit._loading_plan_fakes import (
    APPROVALS,
    ApprovalHarness,
    loading_agent,
    priority_list_for,
    seed_fleet,
)

TENANT = "t1"
OTHER_TENANT = "t2"
DISPATCHER = "user-dispatcher-1"
RUN_ID = "run-1"
TRUCK_ID = "truck-1"
ACTIVITY_INDEX = ActivityLogService.INDEX


class FakeSocket:
    """A WebSocket stand-in: ``accept`` succeeds, ``send_json`` records."""

    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, data: Dict[str, Any]) -> None:
        self.messages.append(data)

    def types(self) -> List[str]:
        return [m.get("type") for m in self.messages]

    def non_handshake(self) -> List[Dict[str, Any]]:
        return [m for m in self.messages if m.get("type") != "connection"]


def no_llm(monkeypatch) -> None:
    """Every model entry point raises: the approve flow must not reach an LLM."""
    import Agents.model_provider as model_provider

    def _boom(*_args: Any, **_kwargs: Any):
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr(model_provider, "build_agent_model", _boom)
    monkeypatch.setattr(model_provider, "resolve_agent_model_spec", _boom)


class World:
    """One tenant's loading pipeline plus the HTTP surface."""

    def __init__(
        self,
        monkeypatch,
        orders: Sequence[Dict[str, Any]],
        *,
        tanks: Optional[Dict[str, Tuple[float, float]]] = None,
    ) -> None:
        self.ws = AgentActivityWSManager()
        self.sock_t1 = FakeSocket()
        self.sock_t2 = FakeSocket()
        self._monkeypatch = monkeypatch
        self._tanks = tanks
        self._orders = list(orders)
        self.roles: List[str] = ["dispatcher"]
        self.h: Optional[ApprovalHarness] = None

    async def start(self) -> "World":
        await self.ws.connect(self.sock_t1, tenant_id=TENANT)
        await self.ws.connect(self.sock_t2, tenant_id=OTHER_TENANT)
        from tests.unit._loading_plan_fakes import InMemoryDocStore

        store = InMemoryDocStore()
        self.activity = ActivityLogService(store, ws_manager=self.ws)
        self.h = ApprovalHarness(
            self._orders, tenant_id=TENANT, store=store, ws=self.ws, activity=self.activity
        )
        seed_fleet(store, tenant_id=TENANT, trucks=(TRUCK_ID,))
        self.agent = loading_agent(
            store, confirmation_protocol=self.h.protocol, tanks=self._tanks
        )
        self._monkeypatch.setattr(agent_endpoints, "_approval_queue_service", self.h.svc)
        self.app = FastAPI()
        register_exception_handlers(self.app)
        self.app.include_router(agent_endpoints.router)
        self.app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
            tenant_id=TENANT,
            user_id=DISPATCHER,
            has_pii_access=False,
            roles=list(self.roles),
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )
        return self

    @property
    def store(self):
        return self.h.store

    async def propose(self, order_ids: Sequence[str]) -> List[Any]:
        """Run the loader on a priority list and route every proposal."""
        self.agent._priority_buffer.append(
            priority_list_for(order_ids, tenant_id=TENANT, run_id=RUN_ID)
        )
        proposals = await self.agent.evaluate([])
        for proposal in proposals:
            await self.agent._route_proposal(proposal, "active_gated")
        return proposals

    def entries(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return [
            e for e in self.store.docs[APPROVALS].values()
            if status is None or e.get("status") == status
        ]

    def activity_entries(self, action_type: str) -> List[Dict[str, Any]]:
        return [
            e for e in self.store.docs[ACTIVITY_INDEX].values()
            if e.get("action_type") == action_type
        ]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def approve(self, action_id: str, **params: Any) -> httpx.Response:
        async with self.client() as client:
            return await client.post(
                f"/api/agent/approvals/{action_id}/approve", params=params or None
            )

    async def list_approvals(self, **params: Any) -> httpx.Response:
        async with self.client() as client:
            return await client.get("/api/agent/approvals", params=params or None)
