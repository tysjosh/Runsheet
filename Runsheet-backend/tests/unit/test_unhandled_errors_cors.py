"""
Unexpected 500s keep their CORS headers (UI revamp task 0.1).

Before ``middleware/unhandled_errors.py`` the only handler for a bare
``Exception`` ran in Starlette's ``ServerErrorMiddleware``, outside
``CORSMiddleware``, so the browser saw a 500 with no
``Access-Control-Allow-Origin`` and reported "Failed to fetch"
(``.agents/tasks/ui-revamp-2026-10-08/audit.md`` §(i-b)).

The test app mirrors ``main.py``'s order: exception handlers, RequestID,
the new middleware, then CORS outermost. ``bootstrap.middleware
.register_at_import`` adds it, and ``main.py`` calls that immediately before
CORS. A last test pins that order on the real ``main.app``.
"""

import pytest
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from errors.exceptions import resource_not_found
from errors.handlers import register_exception_handlers
from middleware.request_id import RequestIDMiddleware
from middleware.unhandled_errors import GENERIC_MESSAGE, UnhandledErrorMiddleware
from persistence.document_query import InvalidQueryValueError

ALLOWED_ORIGIN = "https://app.staging.runsheetops.com"
SECRET = "db password is hunter2"


def _build_app(*, with_request_id: bool = True) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError(SECRET)

    @app.get("/not-found")
    async def not_found():
        raise resource_not_found("Order not found", details={"order_id": "QA-1"})

    @app.get("/validated")
    async def validated(limit: int = Query(...)):
        return {"limit": limit}

    @app.get("/bad-filter")
    async def bad_filter():
        try:
            raise InvalidQueryValueError()
        except InvalidQueryValueError as exc:
            raise RuntimeError("wrapped") from exc

    @app.get("/stream-boom")
    async def stream_boom():
        async def body():
            yield b"partial"
            raise RuntimeError(SECRET)

        return StreamingResponse(body())

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    if with_request_id:
        app.add_middleware(RequestIDMiddleware)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["Content-Type"],
    )
    return app


@pytest.fixture()
def client():
    return TestClient(_build_app())


class TestUnexpected500:
    def test_allowed_origin_gets_envelope_and_cors_header(self, client):
        resp = client.get("/boom", headers={"Origin": ALLOWED_ORIGIN})

        assert resp.status_code == 500
        assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert resp.headers["access-control-allow-credentials"] == "true"
        body = resp.json()
        assert body == {
            "error_code": "INTERNAL_ERROR",
            "message": GENERIC_MESSAGE,
            "details": {},
            "request_id": body["request_id"],
        }
        assert body["request_id"]
        assert resp.headers["x-request-id"] == body["request_id"]

    def test_exception_text_is_not_leaked(self, client):
        resp = client.get("/boom", headers={"Origin": ALLOWED_ORIGIN})
        assert SECRET not in resp.text
        assert "RuntimeError" not in resp.text

    def test_disallowed_origin_gets_no_cors_header(self, client):
        resp = client.get("/boom", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 500
        assert "access-control-allow-origin" not in resp.headers
        assert resp.json()["error_code"] == "INTERNAL_ERROR"

    def test_request_id_from_request_id_middleware_is_used(self, client):
        resp = client.get(
            "/boom", headers={"Origin": ALLOWED_ORIGIN, "X-Request-ID": "req-qa-500"}
        )
        assert resp.json()["request_id"] == "req-qa-500"
        assert resp.headers["x-request-id"] == "req-qa-500"

    def test_error_is_logged_with_request_id(self, client, caplog):
        with caplog.at_level("ERROR", logger="middleware.unhandled_errors"):
            client.get("/boom", headers={"X-Request-ID": "req-qa-log"})
        records = [r for r in caplog.records if r.name == "middleware.unhandled_errors"]
        assert records, "the unexpected error was not logged"
        assert records[0].request_id == "req-qa-log"
        assert records[0].exception_type == "RuntimeError"
        assert records[0].exc_info is not None

    def test_request_id_falls_back_to_header_then_uuid(self):
        bare = TestClient(_build_app(with_request_id=False))
        from_header = bare.get("/boom", headers={"X-Request-ID": "req-from-header"})
        assert from_header.json()["request_id"] == "req-from-header"
        generated = bare.get("/boom")
        assert len(generated.json()["request_id"]) == 36


class TestOtherPathsUnchanged:
    def test_app_exception_keeps_its_status_and_envelope(self, client):
        resp = client.get("/not-found", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.status_code == 404
        assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        body = resp.json()
        assert body["error_code"] == "RESOURCE_NOT_FOUND"
        assert body["message"] == "Order not found"
        assert body["details"] == {"order_id": "QA-1"}

    def test_validation_error_is_still_422(self, client):
        resp = client.get("/validated", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "VALIDATION_ERROR"
        assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN

    def test_query_value_error_under_a_wrapper_is_still_400(self, client):
        resp = client.get("/bad-filter", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "VALIDATION_ERROR"
        assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN

    def test_success_passes_through(self, client):
        resp = client.get("/ok", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_error_after_response_started_is_reraised(self):
        # Headers are already on the wire, so a new response can't be sent.
        strict = TestClient(_build_app())
        with pytest.raises(RuntimeError):
            strict.get("/stream-boom")

    def test_lifespan_scope_passes_through(self):
        with TestClient(_build_app()) as lifespan_client:
            assert lifespan_client.get("/ok").status_code == 200


def test_main_app_registers_it_immediately_inside_cors():
    from starlette.middleware.cors import CORSMiddleware as StarletteCORS

    import main

    classes = [m.cls for m in main.app.user_middleware]
    assert classes[0] is StarletteCORS
    assert classes[1] is UnhandledErrorMiddleware
