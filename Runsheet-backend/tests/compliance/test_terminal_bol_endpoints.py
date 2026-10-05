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
