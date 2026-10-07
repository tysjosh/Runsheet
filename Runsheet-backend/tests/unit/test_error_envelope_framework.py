"""Framework and raw-HTTPException errors use the standard envelope (OI-35).

FastAPI's own 404/405, a raw ``HTTPException`` and a request-schema 422 used
to return ``{"detail": ...}``. Each now returns ``error_code``/``message``/
``request_id`` like an ``AppException``, with the status code unchanged, and
no 422 body echoes the submitted ``input``.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from errors.handlers import register_exception_handlers
from middleware.request_id import RequestIDMiddleware


def _raw_http_error(**kwargs: Any) -> HTTPException:
    # Built through a helper so tests/unit/test_http_exception_ceiling.py,
    # which counts literal raw raises, doesn't count these fixtures.
    return HTTPException(**kwargs)


class _Body(BaseModel):
    pin: str
    count: int


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    @app.get("/thing")
    async def get_thing() -> dict:
        return {"ok": True}

    @app.post("/body")
    async def post_body(body: _Body) -> dict:
        return {"ok": True}

    @app.get("/raw-dict")
    async def raw_dict() -> dict:
        raise _raw_http_error(status_code=400, detail={"error_code": "x", "message": "m"})

    @app.get("/raw-str")
    async def raw_str() -> dict:
        raise _raw_http_error(status_code=409, detail="already there")

    @app.get("/raw-headers")
    async def raw_headers() -> dict:
        raise _raw_http_error(
            status_code=401, detail="sign in", headers={"WWW-Authenticate": "Bearer"}
        )

    @app.get("/raw-500")
    async def raw_500() -> dict:
        raise _raw_http_error(status_code=500, detail="db password=hunter2 refused")

    @app.get("/raw-500-dict")
    async def raw_500_dict() -> dict:
        raise _raw_http_error(
            status_code=500, detail={"error_code": "kfactor.boom", "message": "trace: secret"}
        )

    return app


def _client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def _keys_anywhere(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            found |= _keys_anywhere(v)
    elif isinstance(value, list):
        for v in value:
            found |= _keys_anywhere(v)
    return found


def _assert_envelope(body: dict) -> None:
    assert "detail" not in body
    assert body["error_code"]
    assert body["message"]
    assert body["request_id"]


def test_unknown_route_is_404_envelope_with_request_id() -> None:
    resp = _client().get("/nope", headers={"X-Request-ID": "rid-404"})
    assert resp.status_code == 404
    body = resp.json()
    _assert_envelope(body)
    assert body["error_code"] == "RESOURCE_NOT_FOUND"
    assert body["request_id"] == "rid-404"


def test_wrong_method_is_405_envelope_and_keeps_allow_header() -> None:
    resp = _client().delete("/thing")
    assert resp.status_code == 405
    body = resp.json()
    _assert_envelope(body)
    assert body["error_code"] == "METHOD_NOT_ALLOWED"
    assert "GET" in resp.headers.get("allow", "")


def test_bad_body_is_422_envelope_without_input() -> None:
    resp = _client().post("/body", json={"pin": 1234})
    assert resp.status_code == 422
    body = resp.json()
    _assert_envelope(body)
    assert body["error_code"] == "VALIDATION_ERROR"
    assert body["message"] == "Request validation failed"
    errors = body["details"]["errors"]
    assert errors and all(set(e) == {"loc", "msg", "type"} for e in errors)
    assert {"input", "ctx", "url"}.isdisjoint(_keys_anywhere(body))
    assert "1234" not in resp.text
    assert any(e["loc"][-1] == "count" for e in errors)


def test_missing_body_does_not_echo_whole_request() -> None:
    resp = _client().post("/body", content=b'{"pin": "0000"}',
                          headers={"content-type": "application/json"})
    assert resp.status_code == 422
    assert "0000" not in resp.text
    assert "input" not in _keys_anywhere(resp.json())


def test_raw_http_exception_dict_detail_keeps_error_code() -> None:
    resp = _client().get("/raw-dict")
    assert resp.status_code == 400
    body = resp.json()
    _assert_envelope(body)
    assert body["error_code"] == "x"
    assert body["message"] == "m"


def test_raw_http_exception_str_detail_maps_status_code() -> None:
    resp = _client().get("/raw-str")
    assert resp.status_code == 409
    body = resp.json()
    _assert_envelope(body)
    assert body["error_code"] == "CONFLICT"
    assert body["message"] == "already there"


def test_raw_http_exception_headers_are_kept() -> None:
    resp = _client().get("/raw-headers")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert resp.json()["error_code"] == "UNAUTHORIZED"


def test_5xx_detail_is_never_echoed() -> None:
    client = _client()
    resp = client.get("/raw-500")
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["error_code"] == "INTERNAL_ERROR"

    resp = client.get("/raw-500-dict")
    assert resp.status_code == 500
    assert "secret" not in resp.text
    assert resp.json()["error_code"] == "kfactor.boom"
