"""
Legacy chat session memory is scoped to (tenant, user, session) and clear works.

Staging finding F1, second vector: ``LogisticsAgent`` persisted conversation
history in the session store keyed on the client-supplied ``session_id`` alone,
so tenant B sending tenant A's session id got A's history loaded into its turn.
``/api/chat/clear`` cleared a throwaway agent and fire-and-forgot an unscoped
store delete.

These tests go through the real ``/api/chat/fallback`` and ``/api/chat/clear``
handlers and the real ``LogisticsAgent`` on a real Strands loop. Only the LLM
(``RecordingModel``) and Redis (``FakeSessionStore``, a dict that serializes like
Redis and accepts any key format) are replaced, so the key the code chooses is
exactly what decides whether history crosses tenants.

Two users of one tenant who send the same session id don't share history
either (OI-38): the verified ``user_id`` is part of the key, and a missing user
means the store isn't touched at all.

Validates: Requirements 8.2, 8.3; staging finding F1; OI-38.
"""
from __future__ import annotations

import asyncio
import copy
import importlib
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands.models import Model

import inline_endpoints
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


class RecordingModel(Model):
    """Fake Strands model: records the messages of every call, answers "ok"."""

    def __init__(self) -> None:
        self.calls: List[list] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        await asyncio.sleep(0.01)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "ok"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _texts(call: list) -> List[str]:
    return [block["text"] for m in call for block in m["content"] if "text" in block]


class FakeSessionStore:
    """In-memory stand-in for ``RedisSessionStore``. Deep-copies on get/set to
    mimic serialization, and stores under whatever key it is given."""

    def __init__(self) -> None:
        self.data: Dict[str, dict] = {}
        self.accessed: List[str] = []

    async def connect(self) -> None:
        pass

    async def get(self, key: str):
        self.accessed.append(key)
        value = self.data.get(key)
        return copy.deepcopy(value) if value is not None else None

    async def set(self, key: str, value: dict, ttl=None) -> None:
        self.accessed.append(key)
        self.data[key] = copy.deepcopy(value)

    async def delete(self, key: str) -> None:
        self.accessed.append(key)
        self.data.pop(key, None)


@pytest.fixture
def session_store():
    return FakeSessionStore()


@pytest.fixture
def harness(monkeypatch, session_store):
    model = RecordingModel()
    store = session_store
    current = {"tenant": "tenant-A", "user": "user-1"}

    # Patch the module objects in ``sys.modules``: that is what the handlers'
    # call-time ``from Agents.mainagent import LogisticsAgent`` resolves to.
    # A dotted-string target walks package attributes instead, and after other
    # tests evict and re-import these modules the two can differ. Then the patch
    # missed, the real RedisSessionStore wrote session:tenant-*:s1 into the local
    # Redis, and the next full run within the TTL failed on that history.
    mainagent = importlib.import_module("Agents.mainagent")
    model_provider = importlib.import_module("Agents.model_provider")

    # Makes setup_gemini_credentials return early; no network either way.
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    monkeypatch.setattr(model_provider, "build_agent_model", lambda settings: model)
    monkeypatch.setattr(mainagent, "_orchestrator", None)
    monkeypatch.setattr(mainagent, "_get_session_store", lambda: store)

    async def _tenant() -> TenantContext:
        return TenantContext(
            tenant_id=current["tenant"],
            user_id=current["user"],
            has_pii_access=False,
            roles=["dispatcher"],
        )

    app = FastAPI()
    app.include_router(inline_endpoints.router)
    register_exception_handlers(app)
    app.dependency_overrides[get_tenant_context] = _tenant
    client = TestClient(app)

    def turn(tenant: str, session_id: str, message: str, user: str = "user-1") -> list:
        current["tenant"] = tenant
        current["user"] = user
        before = len(model.calls)
        resp = client.post(
            "/api/chat/fallback", json={"message": message, "session_id": session_id}
        )
        assert resp.status_code == 200, resp.text
        assert len(model.calls) == before + 1
        return model.calls[-1]

    def clear(tenant: str, session_id: str, user: str = "user-1"):
        current["tenant"] = tenant
        current["user"] = user
        return client.post("/api/chat/clear", json={"session_id": session_id})

    return turn, clear


def test_other_tenant_with_same_session_id_sees_nothing(harness):
    turn, _ = harness
    turn("tenant-A", "s1", "My truck is QA-TRUCK-AG-01")
    call = turn("tenant-B", "s1", "hello")

    assert len(call) == 1, f"tenant B's turn carried {len(call)} messages"
    assert not any("QA-TRUCK-AG-01" in t for t in _texts(call))


def test_two_sessions_in_one_tenant_do_not_share(harness):
    turn, _ = harness
    turn("tenant-A", "s1", "session one secret")
    call = turn("tenant-A", "s2", "hello")

    assert len(call) == 1
    assert not any("session one secret" in t for t in _texts(call))


def test_clear_empties_only_the_callers_history(harness):
    turn, clear = harness
    turn("tenant-A", "s1", "alpha-secret")
    b_first = turn("tenant-B", "s1", "bravo-secret")
    assert not any("alpha-secret" in t for t in _texts(b_first))

    resp = clear("tenant-A", "s1")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Chat memory cleared successfully", "session_id": "s1"}

    a_after = turn("tenant-A", "s1", "anything left?")
    assert len(a_after) == 1, "tenant A's history survived /api/chat/clear"

    b_after = turn("tenant-B", "s1", "still there?")
    b_texts = _texts(b_after)
    assert any("bravo-secret" in t for t in b_texts), "A's clear wiped B's history"
    assert not any("alpha-secret" in t for t in b_texts)


def test_same_session_retains_context(harness):
    """The legitimate per-session memory survives the fix."""
    turn, _ = harness
    turn("tenant-A", "s1", "My truck is QA-TRUCK-AG-01")
    call = turn("tenant-A", "s1", "which truck?")

    assert any("QA-TRUCK-AG-01" in t for t in _texts(call))


def test_clear_reports_failure_when_store_delete_fails(harness, monkeypatch):
    """A failed store delete must not be reported as a successful clear.

    Before this, ``/api/chat/clear`` ignored ``clear_memory``'s result and
    returned 200 even though the history was still stored, so the next turn
    reloaded it.
    """
    turn, clear = harness
    turn("tenant-A", "s1", "still-stored-secret")

    async def _failing_delete(self, key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(FakeSessionStore, "delete", _failing_delete)
    resp = clear("tenant-A", "s1")

    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "SESSION_STORE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# OI-38: history is per user within a tenant
# ---------------------------------------------------------------------------


def test_two_users_in_one_tenant_with_same_session_id_do_not_share(harness, session_store):
    turn, _ = harness
    turn("tenant-A", "s1", "user one secret", user="user-1")
    call = turn("tenant-A", "s1", "hello", user="user-2")

    assert len(call) == 1, f"user-2's turn carried {len(call)} messages"
    assert not any("user one secret" in t for t in _texts(call))
    assert set(session_store.data) == {"tenant-A:user-1:s1", "tenant-A:user-2:s1"}


def test_missing_user_never_touches_the_store(harness, session_store):
    turn, _ = harness
    turn("tenant-A", "s1", "anonymous secret", user="")

    assert session_store.accessed == []
    assert session_store.data == {}


def test_clear_deletes_only_the_callers_key(harness, session_store):
    turn, clear = harness
    turn("tenant-A", "s1", "one", user="user-1")
    turn("tenant-A", "s1", "two", user="user-2")

    resp = clear("tenant-A", "s1", user="user-1")

    assert resp.status_code == 200, resp.text
    assert set(session_store.data) == {"tenant-A:user-2:s1"}
