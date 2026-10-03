"""
Regression tests for staging finding F1: the public sign-up and email-exists
HTTP APIs must not be served.

``POST /auth/signup`` let anyone create users in the SuperTokens core, and
``GET /auth/signup/email/exists`` was an account-enumeration oracle (F4).
``auth/supertokens_init.py`` disables both through an EmailPassword APIs
override. Sign-in and the password-reset APIs stay served, and the
provisioner's recipe-level ``sign_up`` is unaffected (it does not go through
the HTTP API).

None of these tests dial a core: ``init_supertokens`` never contacts it, and a
disabled API is never matched by the SDK middleware.
"""

from __future__ import annotations

import pytest
from supertokens_python.recipe.emailpassword.api.implementation import (
    APIImplementation,
)

import auth.supertokens_init as st_init

# Reuse the SDK init/reset fixture so these tests leave no SDK state behind.
from tests.integration.test_supertokens_auth_flow import (  # noqa: F401
    _build_auth_app,
    _form_fields,
    initialized_supertokens,
)


def test_override_disables_only_signup_and_email_exists():
    api = st_init._override_emailpassword_apis(APIImplementation())

    assert api.disable_sign_up_post is True
    assert api.disable_email_exists_get is True
    # Sign-in and forgot/reset password stay served.
    assert api.disable_sign_in_post is False
    assert api.disable_generate_password_reset_token_post is False
    assert api.disable_password_reset_post is False


def test_initialized_recipe_marks_signup_and_email_exists_disabled(
    initialized_supertokens,
):
    from supertokens_python.recipe.emailpassword.recipe import EmailPasswordRecipe

    disabled = {}
    for api in EmailPasswordRecipe.get_instance().get_apis_handled():
        path = api.path_without_api_base_path.get_as_string_dangerous()
        disabled[(api.method, path)] = api.disabled

    assert disabled[("post", "/signup")] is True
    assert disabled[("get", "/signup/email/exists")] is True
    assert disabled[("get", "/emailpassword/email/exists")] is True
    assert disabled[("post", "/signin")] is False
    assert disabled[("post", "/user/password/reset/token")] is False
    assert disabled[("post", "/user/password/reset")] is False


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/auth/signup"),
        ("get", "/auth/signup/email/exists?email=someone@example.com"),
        ("get", "/auth/emailpassword/email/exists?email=someone@example.com"),
    ],
)
def test_disabled_routes_fall_through_to_404(initialized_supertokens, method, path):
    from starlette.testclient import TestClient

    client = TestClient(_build_auth_app())
    headers = {"rid": "emailpassword"}
    if method == "post":
        resp = client.post(
            path,
            json=_form_fields("someone@example.com", "Testpass123!"),
            headers=headers,
        )
    else:
        resp = client.get(path, headers=headers)

    assert resp.status_code == 404, resp.text
