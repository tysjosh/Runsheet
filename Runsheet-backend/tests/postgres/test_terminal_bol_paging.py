"""BOL list keyset paging against the real PG document store (finding C13).

Five BOLs, two sharing a ``created_at``, walked two at a time through the
endpoint. Every BOL must come back exactly once, in (created_at desc,
bol_id asc) order.

Real PostgreSQL required — see ``conftest.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

from compliance.api import terminal_bol_endpoints

TENANT = "demo-tenant"


class _Facade:
    def __init__(self, store, index_name):
        self._store = store
        self._index = index_name

    async def search_documents(self, index, query, size=100, **kw):
        return await self._store.search_documents(self._index, query, size, **kw)

    async def get_document(self, index, doc_id):
        return await self._store.get_document(self._index, doc_id)


async def test_pages_have_no_skips_or_repeats(store, index_name):
    created = {
        "QA-BOL-1": "2026-10-01T00:00:00+00:00",
        "QA-BOL-2": "2026-10-03T00:00:00+00:00",
        "QA-BOL-3": "2026-10-02T00:00:00+00:00",
        "QA-BOL-4": "2026-10-02T00:00:00+00:00",  # ties with QA-BOL-3
        "QA-BOL-5": "2026-09-30T00:00:00+00:00",
    }
    for bol_id, ts in created.items():
        await store.index_document(index_name, bol_id, {"bol_id": bol_id, "tenant_id": TENANT})
    # The store stamps created_at on write; set the test values afterwards.
    for bol_id, ts in created.items():
        await store.update_document(index_name, bol_id, {"created_at": ts})

    terminal_bol_endpoints.configure_terminal_bol_api(bol_service=object(), es_service=_Facade(store, index_name))
    request = SimpleNamespace(state=SimpleNamespace(request_id="QA-REQ"))
    tenant = SimpleNamespace(tenant_id=TENANT)
    try:
        seen, cursor = [], None
        for _ in range(5):
            page = await terminal_bol_endpoints.list_terminal_bols(
                request=request, tenant=tenant, status=None, driver_id=None,
                product_code=None, load_number=None, cursor=cursor, limit=2,
            )
            seen.extend(d["bol_id"] for d in page["data"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
    finally:
        terminal_bol_endpoints._bol_service = None
        terminal_bol_endpoints._es_service = None

    assert seen == ["QA-BOL-2", "QA-BOL-3", "QA-BOL-4", "QA-BOL-1", "QA-BOL-5"]
