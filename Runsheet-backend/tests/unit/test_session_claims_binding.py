"""
Regression tests for staging finding F1(b): session claims are bound to the
SuperTokens user id, never to the email address.

Before the fix, ``_claims_for_user`` resolved the user's email through the core
and read ``auth_users WHERE email = :email`` without comparing
``st_user_id``. Anyone who registered a SuperTokens user under a provisioned
(but not yet bound) email inherited that row's tenant and roles.

The fakes below answer both query shapes — ``st_user_id = :user_id`` and
``email = :email`` — and ``supertokens_python.asyncio.get_user`` is patched to
return the attacker's email, so the same tests run (and fail by assertion)
against the old email-keyed code.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import auth.supertokens_init as st_init

_PROVISIONED_EMAIL = "newhire@example.com"


class _Result:
    def __init__(self, rows: List[tuple]) -> None:
        self._rows = rows

    def first(self) -> Optional[tuple]:
        return self._rows[0] if self._rows else None

    def all(self) -> List[tuple]:
        return list(self._rows)


class _FakeDB:
    """Filters an in-memory ``auth_users`` table by whichever key is bound."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    async def execute(self, _query, params: Dict[str, Any]) -> _Result:
        if "user_id" in params:
            hits = [r for r in self._rows if r["st_user_id"] == params["user_id"]]
        else:
            hits = [
                r
                for r in self._rows
                if r["email"].casefold() == params["email"].casefold()
            ]
        return _Result(
            [
                (r["tenant_id"], r["roles"], r["has_pii_access"], r["driver_id"])
                for r in hits
            ]
        )


def _row(st_user_id: Optional[str], email: str = _PROVISIONED_EMAIL) -> Dict[str, Any]:
    return {
        "email": email,
        "st_user_id": st_user_id,
        "tenant_id": "tenant-A",
        "roles": ["admin"],
        "has_pii_access": True,
        "driver_id": None,
    }


@pytest.fixture
def auth_users(monkeypatch):
    """Install a fake ``auth_users`` table; returns the mutable row list."""
    import persistence.database as database
    import supertokens_python.asyncio as st_asyncio

    rows: List[Dict[str, Any]] = []

    @asynccontextmanager
    async def _session_scope():
        yield _FakeDB(rows)

    monkeypatch.setattr(database, "is_persistence_enabled", lambda: True)
    monkeypatch.setattr(database, "session_scope", _session_scope)

    # Every SuperTokens user in these tests carries the provisioned email, so
    # an email-keyed lookup would always find the row.
    async def _get_user(user_id, *_args, **_kwargs):
        return SimpleNamespace(id=user_id, emails=[_PROVISIONED_EMAIL])

    monkeypatch.setattr(st_asyncio, "get_user", _get_user)
    return rows


async def test_bound_row_yields_claims(auth_users):
    auth_users.append(_row("st-1"))

    claims = await st_init._claims_for_user("st-1")

    assert claims == {
        "tenant_id": "tenant-A",
        "roles": ["admin"],
        "has_pii_access": True,
    }


async def test_unbound_row_yields_no_claims_for_preregistered_user(auth_users):
    # Provisioned row, never bound (st_user_id NULL); the attacker signed up
    # with that email first.
    auth_users.append(_row(None))

    assert await st_init._claims_for_user("st-attacker") == {}


async def test_row_bound_to_another_user_yields_no_claims(auth_users):
    auth_users.append(_row("st-other"))

    assert await st_init._claims_for_user("st-attacker") == {}


async def test_ambiguous_binding_fails_closed(auth_users):
    auth_users.append(_row("st-1", email="a@example.com"))
    auth_users.append(_row("st-1", email="b@example.com"))

    assert await st_init._claims_for_user("st-1") == {}


async def test_dormant_persistence_yields_no_claims(monkeypatch):
    import persistence.database as database

    monkeypatch.setattr(database, "is_persistence_enabled", lambda: False)

    assert await st_init._claims_for_user("st-1") == {}


async def test_session_payload_carries_no_tenant_for_unbound_user(auth_users):
    auth_users.append(_row(None))
    captured: Dict[str, Any] = {}

    async def _original_create_new_session(user_id, recipe_user_id, payload, *_rest):
        captured["payload"] = payload
        return "session"

    impl = SimpleNamespace(create_new_session=_original_create_new_session)
    st_init._override_session_functions(impl)

    await impl.create_new_session(
        "st-attacker", None, {"client": "x"}, None, None, "public", {}
    )

    assert "tenant_id" not in captured["payload"]
    assert "roles" not in captured["payload"]
    assert captured["payload"]["client"] == "x"
