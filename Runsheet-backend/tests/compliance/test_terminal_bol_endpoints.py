"""HTTP-level tests for ``/api/compliance/terminal-bols``.

Built on the real ``TerminalBOLIngestionService`` with the standard EDI
registry, over a mocked document store, so the request goes through the same
parser and error mapping production uses.

* C1: a valid pipe-delimited BOL ingests (201).
* C8: an unparseable payload is the client's fault (422), not a 500.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compliance.api import terminal_bol_endpoints
from compliance.services.terminal_bol_edi_parser import create_default_registry
from compliance.services.terminal_bol_ingestion_service import (
    TerminalBOLIngestionService,
)
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"
URL = "/api/compliance/terminal-bols"

_PIPE_HEADER = (
    "load_number|product_code|gross_gallons|net_gallons|observed_temperature|"
    "api_gravity|supplier_name|terminal_name|driver_id|timestamp"
)
_PIPE_ROW = (
    "QA-LOAD-001|UNL87|8500.0|8450.5|72.5|58.2|Marathon Petroleum|"
    "Pasadena Terminal|DRV-100|2024-01-15T10:30:00"
)


def _mock_es(hits: List[Dict[str, Any]] | None = None) -> MagicMock:
    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": hits or [], "total": {"value": len(hits or [])}}}
    )
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.get_document = AsyncMock(return_value=None)
    return es


@pytest.fixture
def es():
    return _mock_es()


@pytest.fixture
def client(es):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(terminal_bol_endpoints.router)
    install_test_auth(app)
    terminal_bol_endpoints.configure_terminal_bol_api(
        bol_service=TerminalBOLIngestionService(
            es_service=es, edi_parser_registry=create_default_registry()
        ),
        es_service=es,
    )
    yield TestClient(app)
    terminal_bol_endpoints._bol_service = None
    terminal_bol_endpoints._es_service = None


def _post_raw(client, body: bytes):
    return client.post(
        URL,
        content=body,
        headers={**auth_headers(TENANT, roles=["admin"]), "Content-Type": "text/plain"},
    )


def test_valid_pipe_bol_is_ingested(client, es):
    resp = _post_raw(client, f"{_PIPE_HEADER}\n{_PIPE_ROW}\n".encode())
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["load_number"] == "QA-LOAD-001"
    es.index_document.assert_awaited()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not an EDI document", id="unrecognised"),
        pytest.param(f"{_PIPE_HEADER}\n".encode(), id="pipe-header-only"),
    ],
)
def test_malformed_edi_is_422(client, body):
    resp = _post_raw(client, body)
    assert resp.status_code == 422, resp.text
    assert "terminal_bols.invalid_edi" in json.dumps(resp.json())


def test_list_pages_on_created_at_then_bol_id(es, client):
    """Finding C13: the list sorts by created_at but paged on ``bol_id > cursor``.

    Three BOLs whose created_at order (c, a, b) differs from their bol_id
    order (a, b, c). The mock honours sort + search_after like the store does.
    """
    docs = {
        "QA-BOL-a": {"bol_id": "QA-BOL-a", "tenant_id": TENANT, "created_at": "2026-10-02T00:00:00+00:00"},
        "QA-BOL-b": {"bol_id": "QA-BOL-b", "tenant_id": TENANT, "created_at": "2026-10-01T00:00:00+00:00"},
        "QA-BOL-c": {"bol_id": "QA-BOL-c", "tenant_id": TENANT, "created_at": "2026-10-03T00:00:00+00:00"},
    }
    ordered = sorted(docs.values(), key=lambda d: d["bol_id"])
    ordered = sorted(ordered, key=lambda d: d["created_at"], reverse=True)
    key = lambda d: (d["created_at"], d["bol_id"])  # noqa: E731

    async def _search(index, query, size=100, **kw):
        rows = ordered
        after = query.get("search_after")
        if after:
            # created_at desc, bol_id asc: rows strictly after the boundary.
            rows = [
                d for d in ordered
                if d["created_at"] < after[0] or (d["created_at"] == after[0] and d["bol_id"] > after[1])
            ]
        rows = rows[: query.get("size", size)]
        return {"hits": {"hits": [{"_source": d} for d in rows], "total": {"value": len(rows)}}}

    es.search_documents = AsyncMock(side_effect=_search)
    es.get_document = AsyncMock(side_effect=lambda index, doc_id: docs.get(doc_id))
    headers = auth_headers(TENANT, roles=["admin"])

    first = client.get(URL, params={"limit": 2}, headers=headers).json()
    assert [d["bol_id"] for d in first["data"]] == ["QA-BOL-c", "QA-BOL-a"]
    assert first["next_cursor"] == "QA-BOL-a"

    second = client.get(URL, params={"limit": 2, "cursor": first["next_cursor"]}, headers=headers).json()
    assert [d["bol_id"] for d in second["data"]] == ["QA-BOL-b"]
    assert second["next_cursor"] is None
    sent = es.search_documents.call_args_list[-1].args[1]
    assert sent["search_after"] == [key(docs["QA-BOL-a"])[0], "QA-BOL-a"]
    assert "range" not in repr(sent["query"])


def test_unknown_cursor_is_400(es, client):
    es.get_document = AsyncMock(return_value=None)
    resp = client.get(URL, params={"cursor": "QA-BOL-gone"}, headers=auth_headers(TENANT, roles=["admin"]))
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("QA-BOL-NOPE/confirm", {"load_number": "QA-LOAD-9"}, id="confirm"),
        pytest.param("QA-BOL-NOPE/link", {"load_plan_id": "QA-PLAN-1"}, id="link"),
    ],
)
def test_unknown_bol_is_404(client, path, body):
    """Finding C11: an unknown BOL id was a 400 validation error."""
    resp = client.post(f"{URL}/{path}", json=body, headers=auth_headers(TENANT, roles=["admin"]))
    assert resp.status_code == 404, resp.text
    payload = resp.json()
    assert payload["error_code"] == "RESOURCE_NOT_FOUND"
    assert payload.get("request_id")
