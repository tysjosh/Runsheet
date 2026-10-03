"""
Staging finding F7: ``/openapi.json`` (284 paths), ``/docs`` and ``/redoc``
were served without a session on staging.

The schema and both doc UIs are now mounted only in development and test.
CI's ``endpoint-registry`` job imports the app with ``ENVIRONMENT=test`` and
must still see them, and ``app.openapi()`` must keep working everywhere for
in-process tooling (``scripts/check_api_types.py``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config.settings as settings_module


def _stub(is_local: bool):
    return SimpleNamespace(is_local_environment=is_local)


def test_docs_served_under_environment_test():
    from main import app

    assert app.openapi_url == "/openapi.json"
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    # No ``with`` block: the lifespan (service bootstrap) is not needed here.
    assert TestClient(app).get("/openapi.json").status_code == 200


@pytest.mark.parametrize("is_local", [True], ids=["development-or-test"])
def test_local_environments_keep_fastapi_defaults(is_local):
    assert settings_module.api_docs_kwargs(_stub(is_local)) == {}


def test_non_local_environment_hides_schema_and_doc_uis():
    app = FastAPI(**settings_module.api_docs_kwargs(_stub(False)))

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path
    # In-process schema generation still works (scripts/check_api_types.py).
    assert "/ping" in app.openapi()["paths"]


def test_registry_generator_under_test_still_lists_docs_routes():
    from main import app
    from scripts.generate_endpoint_registry import generate_registry

    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/openapi.json", "/docs", "/redoc"} <= paths
    registry = generate_registry()
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert path in registry, path
