"""Flag off answers 404 in both directions (design §2.1 E4, DV7; XR-8, AC8).

[real-auth] Parametrized over (portal off, backbone on) and (portal on,
backbone off). Iterates every portal and portal-admin route dynamically.
"""
from __future__ import annotations

import logging

import pytest

from auth.supertokens_init import STAFF_ROLES
from config.settings import get_settings
from tests.portal.conftest import call, fill_path, portal_admin_routes, portal_routes


@pytest.mark.parametrize(
    "portal,backbone",
    [(False, True), (True, False)],
    ids=["portal_off", "backbone_off"],
)
def test_flag_off_everything_404(
    portal_app, client, set_flags, sessions, portal_fakes, cA, portal, backbone, caplog
):
    caplog.set_level(logging.WARNING, logger="config.settings")
    set_flags(portal=portal, backbone=backbone)
    get_settings()  # build once so the validator runs under caplog

    staff = [sessions.staff(role) for role in STAFF_ROLES] + [sessions.staff_bundle()]
    failures = []
    for method, route in portal_routes(portal_app):
        path = fill_path(route.path)
        for session in [cA, *staff]:
            resp = call(client, method, path, session, json={})
            code = resp.json().get("error_code")
            if resp.status_code != 404 or code != "PORTAL_DISABLED":
                failures.append((method, path, session.claims["roles"], resp.status_code, code))
    for method, route in portal_admin_routes(portal_app):
        path = fill_path(route.path)
        for session in staff:
            resp = call(client, method, path, session, json={})
            if resp.status_code != 404:
                failures.append((method, path, session.claims["roles"], resp.status_code))
        # The central deny comes before the flag for a customer (DV7).
        resp = call(client, method, path, cA, json={})
        if resp.json().get("error_code") != "PORTAL_ROUTE_FORBIDDEN":
            failures.append((method, path, "customer", resp.status_code))
    assert failures == []

    warned = any(
        "customer_portal_enabled without commerce_backbone_enabled" in r.getMessage()
        for r in caplog.records
    )
    assert warned is (portal and not backbone)


def test_portal_enabled_needs_both_flags():
    from types import SimpleNamespace

    from portal.scope import portal_enabled

    def s(p, b):
        return SimpleNamespace(customer_portal_enabled=p, commerce_backbone_enabled=b)

    assert portal_enabled(s(True, True)) is True
    assert portal_enabled(s(True, False)) is False
    assert portal_enabled(s(False, True)) is False
