"""Callers of ``update_document`` must pass the partial doc unwrapped.

``ElasticsearchService.update_document`` hands its payload straight to
:meth:`PostgresDocumentStore.update_document`, which does a shallow
``merged.update(partial_doc)``. A caller that wraps the payload in the ES
``{"doc": ...}`` envelope therefore writes a top-level ``doc`` key and leaves
``status`` / ``load_plan_id`` / ``warnings`` untouched (finding C2). These tests
run the real callers against the real store and read the row back.

Real PostgreSQL required — see ``conftest.py``.
"""
from __future__ import annotations

from typing import Any, Dict

from compliance.services.terminal_bol_edi_parser import create_default_registry
from compliance.services.terminal_bol_ingestion_service import (
    TerminalBOLIngestionService,
)

TENANT = "demo-tenant"


class _IndexFacade:
    """Route every index name to the per-test index so rows stay under the prefix."""

    def __init__(self, store, index_name: str) -> None:
        self._store = store
        self._index = index_name

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100, **kw):
        return await self._store.search_documents(self._index, query, size, **kw)

    async def update_document(self, index: str, doc_id: str, partial: Dict[str, Any]):
        return await self._store.update_document(self._index, doc_id, partial)

    async def index_document(self, index: str, doc_id: str, doc: Dict[str, Any], **kw):
        return await self._store.index_document(self._index, doc_id, doc)

    async def get_document(self, index: str, doc_id: str):
        return await self._store.get_document(self._index, doc_id)


def _bol_doc(bol_id: str, status: str) -> Dict[str, Any]:
    return {
        "bol_id": bol_id,
        "tenant_id": TENANT,
        "load_number": "PENDING" if status == "pending_confirmation" else f"QA-LOAD-{bol_id}",
        "product_code": "UNL87",
        "gross_gallons": 8000.0,
        "net_gallons": 7955.6,
        "observed_temperature_f": 72.0,
        "api_gravity": 35.0,
        "supplier_name": "QA Supplier",
        "terminal_name": "QA Terminal",
        "driver_id": "QA-DRV-1",
        "timestamp": "2026-10-01T10:00:00+00:00",
        "status": status,
        "needs_operator_confirmation": status == "pending_confirmation",
    }


def _service(store, index_name) -> TerminalBOLIngestionService:
    return TerminalBOLIngestionService(
        es_service=_IndexFacade(store, index_name),
        edi_parser_registry=create_default_registry(),
    )


async def test_confirm_manual_bol_changes_top_level_fields(store, index_name):
    await store.index_document(index_name, "QA-BOL-1", _bol_doc("QA-BOL-1", "pending_confirmation"))

    await _service(store, index_name).confirm_manual_bol(
        TENANT, "QA-BOL-1", {"load_number": "QA-LOAD-100", "supplier_name": "Marathon"}
    )

    stored = await store.get_document(index_name, "QA-BOL-1")
    assert "doc" not in stored
    assert stored["status"] == "ingested"
    assert stored["needs_operator_confirmation"] is False
    assert stored["load_number"] == "QA-LOAD-100"
    assert stored["supplier_name"] == "Marathon"


async def test_link_to_load_plan_changes_top_level_fields(store, index_name):
    await store.index_document(index_name, "QA-BOL-2", _bol_doc("QA-BOL-2", "ingested"))

    await _service(store, index_name).link_to_load_plan("QA-BOL-2", "QA-PLAN-9", tenant_id=TENANT)

    stored = await store.get_document(index_name, "QA-BOL-2")
    assert "doc" not in stored
    assert stored["status"] == "linked"
    assert stored["load_plan_id"] == "QA-PLAN-9"


async def test_invoice_warning_payload_lands_at_top_level(store, index_name):
    """The payload shape ``InvoiceService`` sends for a calibration warning."""
    await store.index_document(index_name, "QA-INV-1", {"tenant_id": TENANT, "invoice_id": "QA-INV-1"})
    warnings = [{"code": "meter.calibration_expired", "message": "expired"}]

    await store.update_document(index_name, "QA-INV-1", {"warnings": warnings})

    stored = await store.get_document(index_name, "QA-INV-1")
    assert "doc" not in stored
    assert stored["warnings"] == warnings
