"""Staff sessions can't use the portal (design §2.1 E5; XR-6).

[real-auth] Iterates every ``/api/portal/*`` route on ``main.app``
dynamically, so routes added by later FEATs are covered without editing this.
"""
from __future__ import annotations

import pytest

from auth.supertokens_init import STAFF_ROLES
from tests.portal.conftest import call, fill_path, portal_routes


def test_portal_routes_exist(portal_app):
    assert portal_routes(portal_app), "no /api/portal/* routes mounted"


@pytest.mark.parametrize("role", [*STAFF_ROLES, "platform_staff_bundle"])
def test_staff_roles_forbidden_on_portal(portal_app, client, portal_on, sessions, portal_fakes, role, handler_spy):
    staff = sessions.staff_bundle() if role == "platform_staff_bundle" else sessions.staff(role)
    failures = []
    for method, route in portal_routes(portal_app):
        resp = call(client, method, fill_path(route.path), staff, json={})
        code = resp.json().get("error_code")
        if resp.status_code != 403 or code != "INSUFFICIENT_ROLE":
            failures.append((method, route.path, resp.status_code, code))
    assert failures == []
    # The guard refused before the store was consulted.
    assert portal_fakes.grants.calls == []
