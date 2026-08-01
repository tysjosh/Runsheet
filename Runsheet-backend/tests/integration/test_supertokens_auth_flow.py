"""
Integration tests for the SuperTokens SDK auth flow (task 4.3).

These are a small set of **representative example** integration tests (not
property tests) that exercise the ``auth/supertokens_init.py`` SDK wiring for
the four behaviors called out by the task:

* sign-in establishes a session                 (Req 1.2)
* session refresh rotates the session token      (Req 2.3)
* anti-CSRF is enforced on state-changing requests (Req 2.5)
* every canonical UserRole exists                (Req 4.4)

Deployment is the SuperTokens **managed SaaS core** (design OQ1), which is not
reachable from CI or a typical local checkout. So the file is split in two:

1. **Configuration assertions** (always run). ``init_supertokens`` does not call
   the core, so after initialization we can assert the *intent* of each
   behavior directly against the live SDK recipe configuration — the Session
   recipe enforces ``anti_csrf="VIA_TOKEN"`` and ``cookie_secure=True`` (Req
   2.5/2.2), the EmailPassword sign-in and Session refresh routes are wired
   under ``/auth`` (Req 1.2/2.3), and the canonical roles are declared
   (Req 4.4).

2. **Live-core examples** (skipped unless a core is reachable). When a real
   SuperTokens core is configured via ``SUPERTOKENS_CONNECTION_URI`` and answers
   its ``/hello`` health probe, these drive the real end-to-end flow through a
   ``TestClient`` against the SDK-owned ``/auth`` routes and assert the same
   four behaviors against the running core.

Validates: Requirements 1.2, 2.3, 2.5, 4.4
"""

from __future__ import annotations

import importlib
import os
import uuid
from typing import Iterator, Optional

import pytest

from auth.supertokens_init import (
    CANONICAL_ROLES,
    init_supertokens,
    is_supertokens_initialized,
)
from config.settings import get_settings

# A placeholder core endpoint used for the offline configuration assertions.
# ``init_supertokens`` never contacts the core, so this URI is only a syntactic
# requirement for initialization — it is never dialed by the config tests.
_PLACEHOLDER_CONNECTION_URI = "http://localhost:3567"

# Every SuperTokens recipe module that exposes a ``reset()`` test hook. The SDK
# auto-initializes several internal recipes (account-linking, oauth2 provider,
# multitenancy, ...) alongside the three we register, so a clean re-init for
# test isolation must reset all of them, not just ours.
_RECIPE_MODULES = (
    "accountlinking",
    "dashboard",
    "emailpassword",
    "emailverification",
    "jwt",
    "multifactorauth",
    "multitenancy",
    "oauth2provider",
    "openid",
    "passwordless",
    "saml",
    "session",
    "thirdparty",
    "totp",
    "usermetadata",
    "userroles",
    "webauthn",
)


def _reset_supertokens() -> None:
    """Reset all SuperTokens recipe singletons and the SDK core instance.

    Lets the process re-initialize the SDK from a clean slate so these tests do
    not leak initialization state into the rest of the suite (which does not
    init SuperTokens). Requires ``SUPERTOKENS_ENV=testing`` for the SDK's reset
    guard, which the fixture sets.
    """
    from supertokens_python import Supertokens

    for name in _RECIPE_MODULES:
        try:
            module = importlib.import_module(
                f"supertokens_python.recipe.{name}.recipe"
            )
        except Exception:  # pragma: no cover - optional recipe not present
            continue
        for attr in dir(module):
            if not attr.endswith("Recipe"):
                continue
            obj = getattr(module, attr)
            reset = getattr(obj, "reset", None)
            if isinstance(obj, type) and callable(reset):
                try:
                    reset()
                except Exception:  # pragma: no cover - already-unset recipe
                    pass
    try:
        Supertokens.reset()
    except Exception:  # pragma: no cover - SDK not initialized
        pass


@pytest.fixture
def initialized_supertokens() -> Iterator[object]:
    """Initialize the SuperTokens SDK for the duration of one test.

    Uses the configured ``supertokens_connection_uri`` when present (so live
    tests reach a real core) and falls back to a placeholder otherwise (config
    assertions never dial the core). Always resets the SDK on teardown.

    Yields the loaded :class:`Settings` used for initialization.
    """
    # The SDK's reset hook refuses to run unless this is set.
    os.environ.setdefault("SUPERTOKENS_ENV", "testing")

    settings = get_settings()
    connection_uri = (
        settings.supertokens_connection_uri or _PLACEHOLDER_CONNECTION_URI
    )
    settings = settings.model_copy(
        update={"supertokens_connection_uri": connection_uri}
    )

    # Start from a clean slate in case a previous test left the SDK initialized.
    _reset_supertokens()
    init_supertokens(settings)
    try:
        yield settings
    finally:
        _reset_supertokens()


def _core_is_reachable(settings: object) -> bool:
    """Return whether the configured managed SuperTokens core answers /hello.

    The core exposes a ``GET /hello`` health endpoint that returns ``Hello``.
    A short, best-effort probe keeps the live-core tests fast to skip when no
    core is configured/reachable (the common CI case).
    """
    connection_uri = (getattr(settings, "supertokens_connection_uri", "") or "").strip()
    if not connection_uri or connection_uri == _PLACEHOLDER_CONNECTION_URI:
        # No real core configured — treat as unreachable so live tests skip.
        if connection_uri != _PLACEHOLDER_CONNECTION_URI:
            return False
    api_key = (getattr(settings, "supertokens_api_key", "") or "").strip()

    import httpx

    headers = {"api-key": api_key} if api_key else {}
    try:
        response = httpx.get(
            f"{connection_uri.rstrip('/')}/hello",
            headers=headers,
            timeout=3.0,
        )
    except Exception:
        return False
    return response.status_code == 200


@pytest.fixture
def live_core(initialized_supertokens):
    """Skip the test unless a real managed SuperTokens core is reachable."""
    settings = initialized_supertokens
    if not _core_is_reachable(settings):
        pytest.skip(
            "No reachable SuperTokens managed core "
            "(set SUPERTOKENS_CONNECTION_URI/SUPERTOKENS_API_KEY to a live "
            "core to run the end-to-end auth-flow examples)."
        )
    return settings


# ===========================================================================
# Configuration assertions — always run (init never contacts the core).
# ===========================================================================


def test_signin_and_refresh_routes_are_wired_under_auth(initialized_supertokens):
    """The SDK serves the sign-in and session-refresh routes (Req 1.2, 2.3).

    Sign-in establishing a session (Req 1.2) and refresh rotating the token
    (Req 2.3) are both served by SDK-owned routes mounted under the ``/auth``
    base path. Assert those routes are registered by the EmailPassword and
    Session recipes respectively.
    """
    from supertokens_python.recipe.emailpassword.recipe import EmailPasswordRecipe
    from supertokens_python.recipe.session.recipe import SessionRecipe

    assert is_supertokens_initialized()

    ep_paths = {
        api.path_without_api_base_path.get_as_string_dangerous()
        for api in EmailPasswordRecipe.get_instance().get_apis_handled()
    }
    # Sign-in is the route that establishes a session (Req 1.2).
    assert "/signin" in ep_paths

    session_paths = {
        api.path_without_api_base_path.get_as_string_dangerous()
        for api in SessionRecipe.get_instance().get_apis_handled()
    }
    # Refresh is the route that rotates the session token (Req 2.3).
    assert "/session/refresh" in session_paths

    # Routes are mounted under the /auth base path (design §Auth_Backend).
    assert (
        EmailPasswordRecipe.get_instance()
        .get_app_info()
        .api_base_path.get_as_string_dangerous()
        == "/auth"
    )


def test_session_recipe_enforces_anti_csrf_and_secure_cookies(
    initialized_supertokens,
):
    """The Session recipe enforces anti-CSRF and Secure cookies (Req 2.5, 2.2).

    Anti-CSRF enforcement on state-changing requests (Req 2.5) is a property of
    the Session recipe configuration: ``anti_csrf="VIA_TOKEN"`` makes the SDK
    require the anti-CSRF token on non-idempotent requests, and
    ``cookie_secure=True`` keeps the session cookie ``Secure`` (Req 2.2).
    """
    from supertokens_python.recipe.session.recipe import SessionRecipe

    config = SessionRecipe.get_instance().config

    assert config.anti_csrf_function_or_string == "VIA_TOKEN"
    assert config.cookie_secure is True


def test_canonical_user_roles_are_declared(initialized_supertokens):
    """The canonical UserRoles are declared and the recipe is live (Req 4.4).

    The platform represents ``admin`` / ``dispatcher`` / ``ops_manager`` /
    ``driver`` / ``platform_admin`` as SuperTokens roles. Assert the canonical
    set the provisioning script creates is exactly those and that the UserRoles
    recipe is initialized to back them.
    """
    from supertokens_python.recipe.userroles.recipe import UserRolesRecipe

    assert set(CANONICAL_ROLES) == {
        "admin",
        "dispatcher",
        "ops_manager",
        "driver",
        # Runsheet-staff role. Added because staff sign in through the same app
        # as customers, so "may act outside my own tenant" needed a role that
        # ``admin`` (which is tenant-scoped) could not express. Without it the
        # feature-flag endpoints had no way to distinguish a customer
        # administrator from support staff.
        "platform_admin",
    }
    # No duplicates / no extras in the declared tuple.
    assert len(CANONICAL_ROLES) == 5
    # The UserRoles recipe is registered so the roles can exist in the core.
    assert UserRolesRecipe.get_instance().get_recipe_id() == "userroles"


# ===========================================================================
# Live-core examples — skipped unless a managed core is reachable.
# ===========================================================================


def _form_fields(email: str, password: str) -> dict:
    return {
        "formFields": [
            {"id": "email", "value": email},
            {"id": "password", "value": password},
        ]
    }


def _session_cookie_value(client, name: str) -> Optional[str]:
    """Return the value of a session cookie by name from a TestClient jar."""
    for cookie_name in (name, name.lower()):
        value = client.cookies.get(cookie_name)
        if value:
            return value
    return None


def _build_auth_app():
    """Build a minimal FastAPI app wired with the SuperTokens middleware.

    Includes a single ``verify_session``-protected POST route so the anti-CSRF
    behavior on state-changing requests can be exercised end-to-end.
    """
    from fastapi import Depends, FastAPI
    from supertokens_python.framework.fastapi import get_middleware
    from supertokens_python.recipe.session import SessionContainer
    from supertokens_python.recipe.session.framework.fastapi import verify_session

    app = FastAPI()
    app.add_middleware(get_middleware())

    @app.post("/protected/echo")
    async def protected_echo(  # pragma: no cover - exercised only with live core
        session: SessionContainer = Depends(verify_session()),
    ):
        return {"user_id": session.get_user_id()}

    return app


@pytest.mark.integration
def test_signin_establishes_session(live_core):
    """End-to-end: a valid sign-in establishes a SuperTokens session (Req 1.2)."""
    from starlette.testclient import TestClient

    app = _build_auth_app()
    email = f"itest+{uuid.uuid4().hex}@runsheet.test"
    password = "Testpass123!"

    with TestClient(app) as client:
        signup = client.post("/auth/signup", json=_form_fields(email, password))
        assert signup.status_code == 200, signup.text
        assert signup.json().get("status") == "OK"

        signin = client.post("/auth/signin", json=_form_fields(email, password))
        assert signin.status_code == 200, signin.text
        assert signin.json().get("status") == "OK"

        # A session was established: the SDK set an access-token session cookie
        # and returned a front-token describing it (Req 1.2, 2.1).
        access_token = _session_cookie_value(client, "sAccessToken")
        assert access_token, "expected an sAccessToken session cookie after sign-in"
        assert "front-token" in {k.lower() for k in signin.headers.keys()}


@pytest.mark.integration
def test_refresh_rotates_session_token(live_core):
    """End-to-end: refreshing a session rotates the access token (Req 2.3)."""
    from starlette.testclient import TestClient

    app = _build_auth_app()
    email = f"itest+{uuid.uuid4().hex}@runsheet.test"
    password = "Testpass123!"

    with TestClient(app) as client:
        client.post("/auth/signup", json=_form_fields(email, password))
        signin = client.post("/auth/signin", json=_form_fields(email, password))
        assert signin.status_code == 200, signin.text
        original_access = _session_cookie_value(client, "sAccessToken")
        assert original_access

        refresh = client.post("/auth/session/refresh")
        assert refresh.status_code == 200, refresh.text

        rotated_access = _session_cookie_value(client, "sAccessToken")
        assert rotated_access, "expected a new access-token cookie after refresh"
        # Refresh issues a rotated token without re-entering credentials (Req 2.3).
        assert rotated_access != original_access


@pytest.mark.integration
def test_anti_csrf_enforced_on_state_changing_request(live_core):
    """End-to-end: a state-changing POST without the anti-CSRF token is rejected
    while the same request with it succeeds (Req 2.5)."""
    from starlette.testclient import TestClient

    app = _build_auth_app()
    email = f"itest+{uuid.uuid4().hex}@runsheet.test"
    password = "Testpass123!"

    with TestClient(app) as client:
        client.post("/auth/signup", json=_form_fields(email, password))
        signin = client.post("/auth/signin", json=_form_fields(email, password))
        assert signin.status_code == 200, signin.text

        # The anti-CSRF token is returned out-of-band (header), not in a cookie,
        # so a forged cross-site POST that replays only the cookies lacks it.
        anti_csrf = signin.headers.get("anti-csrf")
        assert anti_csrf, "expected an anti-csrf token on session creation"

        # Without the anti-CSRF header the state-changing POST is rejected.
        rejected = client.post("/protected/echo")
        assert rejected.status_code == 401

        # With the anti-CSRF header the same POST is authorized.
        allowed = client.post(
            "/protected/echo", headers={"anti-csrf": anti_csrf}
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json().get("user_id")


@pytest.mark.integration
async def test_canonical_roles_exist_in_core(live_core):
    """End-to-end: every canonical UserRole exists in the core (Req 4.4)."""
    from supertokens_python.recipe.userroles.asyncio import (
        create_new_role_or_add_permissions,
        get_all_roles,
    )

    # Creating the canonical roles is idempotent, mirroring the provisioning
    # script (task 2.3); run it so the assertion holds on a fresh core.
    for role in CANONICAL_ROLES:
        await create_new_role_or_add_permissions(role, [])

    all_roles = set((await get_all_roles()).roles)
    # Assert against CANONICAL_ROLES rather than a literal set: the loop above
    # creates every canonical role, so a hardcoded subset silently stops
    # covering whatever is added to the tuple next.
    assert set(CANONICAL_ROLES).issubset(all_roles)
