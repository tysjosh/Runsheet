"""Margin API gate: flag 404 first, then admin-only 403 (AC-28, AC-29, AC-30, D1).

Parameterized over ``margin_endpoints.router.routes``, so a route added later
is covered automatically. The customer case uses a session holding only the
``customer`` role (the portal's central deny is not on this branch yet; the
router's own guard must refuse it).
"""
from __future__ import annotations

import pytest

from commerce.api import margin_endpoints
from config.settings import clear_settings_cache

from ._margin_api import BASE, call, concrete, error_code, route_list

ROUTES = route_list()
ROUTE_IDS = [f"{m} {p}" for m, p in ROUTES]


def test_router_covers_every_designed_endpoint():
    expected = {
        ("GET", f"{BASE}/cost-entries"),
        ("POST", f"{BASE}/cost-entries"),
        ("POST", f"{BASE}/cost-entries/import"),
        ("POST", f"{BASE}/cost-entries/{{entry_id}}/supersede"),
        ("POST", f"{BASE}/cost-entries/{{entry_id}}/void"),
        ("GET", f"{BASE}/cost-basis"),
        ("GET", f"{BASE}/records"),
        ("GET", f"{BASE}/records/export"),
        ("GET", f"{BASE}/records/{{record_id}}"),
        ("GET", f"{BASE}/summary"),
        ("POST", f"{BASE}/preview"),
        ("POST", f"{BASE}/recompute"),
        ("GET", f"{BASE}/recompute/{{run_id}}"),
        ("GET", f"{BASE}/alerts"),
        ("POST", f"{BASE}/alerts/{{alert_id}}/acknowledge"),
        ("POST", f"{BASE}/alerts/{{alert_id}}/approve"),
        ("POST", f"{BASE}/alerts/{{alert_id}}/dismiss"),
        ("GET", f"{BASE}/reports"),
        ("GET", f"{BASE}/settings"),
        ("PUT", f"{BASE}/settings"),
    }
    assert set(ROUTES) == expected


def test_admin_gate_is_a_router_level_dependency():
    """A route added later inherits the gate without remembering it."""
    deps = [d.dependency for d in margin_endpoints.router.dependencies]
    assert margin_endpoints.require_margin_admin in deps
    for route in margin_endpoints.router.routes:
        route_deps = [d.dependency for d in route.dependencies]
        assert margin_endpoints.require_margin_admin in route_deps, route.path


@pytest.mark.parametrize("roles", [["admin"], ["dispatcher"], ["driver"]], ids=["admin", "dispatcher", "driver"])
@pytest.mark.parametrize("method,path", ROUTES, ids=ROUTE_IDS)
async def test_flag_off_is_404_for_staff_roles(margin_api_flag_off, method, path, roles):
    resp = await call(margin_api_flag_off.as_(*roles), method, path)
    assert resp.status_code == 404, resp.text
    assert error_code(resp) == "COMMERCE_DISABLED"


@pytest.mark.parametrize("method,path", ROUTES, ids=ROUTE_IDS)
async def test_persistence_off_is_404(margin_api, monkeypatch, method, path):
    monkeypatch.setattr(margin_endpoints, "is_persistence_enabled", lambda: False)
    resp = await call(margin_api.as_("admin"), method, path)
    assert resp.status_code == 404, resp.text
    assert error_code(resp) == "COMMERCE_DISABLED"


@pytest.mark.parametrize("method,path", ROUTES, ids=ROUTE_IDS)
async def test_backbone_off_is_404(margin_api, monkeypatch, method, path):
    monkeypatch.setenv("COMMERCE_BACKBONE_ENABLED", "false")
    clear_settings_cache()
    resp = await call(margin_api.as_("admin"), method, path)
    assert resp.status_code == 404, resp.text
    assert error_code(resp) == "COMMERCE_DISABLED"


@pytest.mark.parametrize(
    "roles",
    [["dispatcher"], ["driver"], ["platform_admin"], ["customer"], ["dispatcher", "platform_admin"], []],
    ids=["dispatcher", "driver", "platform_admin_alone", "customer_only", "dispatcher_plus_staff", "no_roles"],
)
@pytest.mark.parametrize("method,path", ROUTES, ids=ROUTE_IDS)
async def test_flag_on_non_admin_is_403(margin_api, method, path, roles):
    resp = await call(margin_api.as_(*roles), method, path)
    assert resp.status_code == 403, resp.text
    assert error_code(resp) == "INSUFFICIENT_ROLE"


@pytest.mark.parametrize("method,path", ROUTES, ids=ROUTE_IDS)
async def test_flag_on_admin_passes_the_gate(margin_api, method, path):
    """Admin is never refused by the gate (the handler may still 404/422 a dummy id)."""
    resp = await call(margin_api.as_("admin"), method, path)
    assert resp.status_code != 403, resp.text
    assert error_code(resp) not in ("COMMERCE_DISABLED", "INSUFFICIENT_ROLE")


async def test_staff_holding_admin_too_is_served(margin_api):
    resp = await margin_api.as_("admin", "platform_admin").client.get(f"{BASE}/settings")
    assert resp.status_code == 200, resp.text


def test_export_route_declared_before_record_detail():
    paths = [r.path for r in margin_endpoints.router.routes]
    assert paths.index(f"{BASE}/records/export") < paths.index(f"{BASE}/records/{{record_id}}")


async def test_records_export_resolves_to_the_export_handler(margin_api):
    from starlette.routing import Match

    scope = {"type": "http", "method": "GET", "path": concrete(f"{BASE}/records/export")}
    matched = next(
        r for r in margin_api.app.router.routes if r.matches(scope)[0] == Match.FULL
    )
    assert matched.name == "export_records"
    resp = await margin_api.as_("admin").client.get(f"{BASE}/records/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
