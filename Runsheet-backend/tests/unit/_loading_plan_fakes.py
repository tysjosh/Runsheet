"""Shared fakes for the loading-plan executor and MVP dispatch tests.

``InMemoryDocumentStore`` mirrors the parts of the ``ElasticsearchService``
facade (and ``PostgresDocumentStore`` behind it) that the order layer uses,
with the same semantics where they matter:

* ``atomic_update`` hands the transform a deep copy, treats ``None`` as a
  no-op, stamps ``updated_at`` with an ISO string on every applied write and
  returns ``(document, applied)``; a missing document with no ``upsert`` is
  ``(None, False)``.
* ``update_document`` is a shallow merge that stamps ``updated_at`` and raises
  on a missing document.
* ``upsert_if_newer`` discards an incoming ``last_event_timestamp`` that is
  lexically ``<=`` the stored one, like the Postgres store.
* ``search_documents`` evaluates the query with ``persistence.document_matcher``.

Nothing here touches Redis, Postgres or the network.

Fault and interleaving injection: ``store.hooks`` is a list of callables
``hook(op, index, doc_id)`` (sync or async) run before every operation. A hook
can raise to inject a store error, or call :meth:`poke` / :meth:`remove` to
simulate another writer (those bypass hooks and the op log).
"""

from __future__ import annotations

import copy
import inspect
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from fuel.order_models import FuelOrder
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from persistence.document_matcher import matches

ORDERS = FUEL_ORDERS_CURRENT_INDEX
EVENTS = FUEL_ORDER_EVENTS_INDEX

_WRITE_OPS = {"index_document", "update_document", "atomic_update", "upsert_if_newer", "delete_document"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryDocumentStore:
    """Document-store fake with real atomic_update semantics."""

    def __init__(self, *, clock: Optional[Callable[[], str]] = None) -> None:
        self.docs: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        self.ops: List[Tuple[str, str, Optional[str], bool]] = []
        self.hooks: List[Callable[[str, str, Optional[str]], Any]] = []
        self._clock = clock or _iso_now

    # -- helpers for tests ---------------------------------------------

    def seed(self, index: str, doc_id: str, document: Dict[str, Any]) -> None:
        self.docs[index][str(doc_id)] = copy.deepcopy(document)

    def doc(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        found = self.docs[index].get(str(doc_id))
        return copy.deepcopy(found) if found is not None else None

    def poke(self, index: str, doc_id: str, **fields: Any) -> None:
        """Another writer's partial update: merge and stamp, bypassing hooks."""
        current = self.docs[index][str(doc_id)]
        current.update(copy.deepcopy(fields))
        current["updated_at"] = self._clock()

    def remove(self, index: str, doc_id: str) -> None:
        self.docs[index].pop(str(doc_id), None)

    def writes(self, index: Optional[str] = None) -> List[Tuple[str, str, Optional[str]]]:
        """Applied write operations, optionally for one index."""
        return [
            (op, idx, doc_id)
            for op, idx, doc_id, applied in self.ops
            if op in _WRITE_OPS and applied and (index is None or idx == index)
        ]

    def calls(self, op: str, index: Optional[str] = None) -> List[Optional[str]]:
        return [
            doc_id
            for o, idx, doc_id, _applied in self.ops
            if o == op and (index is None or idx == index)
        ]

    def events(self, order_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [
            copy.deepcopy(e)
            for e in self.docs[EVENTS].values()
            if order_id is None or e.get("order_id") == order_id
        ]

    async def _before(self, op: str, index: str, doc_id: Optional[str]) -> None:
        for hook in list(self.hooks):
            result = hook(op, index, doc_id)
            if inspect.isawaitable(result):
                await result

    def _log(self, op: str, index: str, doc_id: Optional[str], applied: bool) -> None:
        self.ops.append((op, index, None if doc_id is None else str(doc_id), applied))

    # -- facade --------------------------------------------------------

    async def get_document(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        await self._before("get_document", index, doc_id)
        self._log("get_document", index, doc_id, False)
        return self.doc(index, doc_id)

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]) -> Dict[str, Any]:
        await self._before("index_document", index, doc_id)
        self.docs[index][str(doc_id)] = copy.deepcopy(document)
        self._log("index_document", index, doc_id, True)
        return {"_index": index, "_id": str(doc_id), "result": "created"}

    async def update_document(self, index: str, doc_id: str, partial_doc: Dict[str, Any]) -> Dict[str, Any]:
        await self._before("update_document", index, doc_id)
        current = self.docs[index].get(str(doc_id))
        if current is None:
            raise LookupError(f"document {index}/{doc_id} not found")
        current.update(copy.deepcopy(partial_doc))
        current["updated_at"] = self._clock()
        self._log("update_document", index, doc_id, True)
        return {"_index": index, "_id": str(doc_id), "result": "updated"}

    async def delete_document(self, index: str, doc_id: str) -> bool:
        await self._before("delete_document", index, doc_id)
        existed = self.docs[index].pop(str(doc_id), None) is not None
        self._log("delete_document", index, doc_id, existed)
        return existed

    async def atomic_update(
        self,
        index: str,
        doc_id: str,
        transform: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
        *,
        upsert: Optional[Dict[str, Any]] = None,
        **_ignored: Any,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        await self._before("atomic_update", index, doc_id)
        current = self.docs[index].get(str(doc_id))
        if current is None:
            if upsert is None:
                self._log("atomic_update", index, doc_id, False)
                return (None, False)
            document = copy.deepcopy(upsert)
            document.setdefault("created_at", self._clock())
            document["updated_at"] = self._clock()
            self.docs[index][str(doc_id)] = document
            self._log("atomic_update", index, doc_id, True)
            return (copy.deepcopy(document), True)
        updated = transform(copy.deepcopy(current))
        if updated is None:
            self._log("atomic_update", index, doc_id, False)
            return (copy.deepcopy(current), False)
        updated = copy.deepcopy(updated)
        updated["updated_at"] = self._clock()
        self.docs[index][str(doc_id)] = updated
        self._log("atomic_update", index, doc_id, True)
        return (copy.deepcopy(updated), True)

    async def upsert_if_newer(
        self,
        index: str,
        doc_id: str,
        document: Dict[str, Any],
        *,
        timestamp_field: str = "last_event_timestamp",
    ) -> bool:
        incoming = document.get(timestamp_field)

        def _transform(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            stored = current.get(timestamp_field)
            if stored is not None and incoming is not None and incoming <= stored:
                return None
            return {**current, **document}

        _doc, applied = await self.atomic_update(index, doc_id, _transform, upsert=dict(document))
        return applied

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 10) -> Dict[str, Any]:
        await self._before("search_documents", index, None)
        self._log("search_documents", index, None, False)
        body = (query or {}).get("query")
        hits = [
            {"_id": doc_id, "_source": copy.deepcopy(doc)}
            for doc_id, doc in self.docs[index].items()
            if matches(doc, body, doc_id=doc_id)
        ]
        for clause in reversed((query or {}).get("sort") or []):
            (field, spec), = clause.items()
            reverse = (spec.get("order") if isinstance(spec, dict) else spec) == "desc"
            hits.sort(key=lambda h: (h["_source"].get(field) is None, str(h["_source"].get(field) or "")), reverse=reverse)
        limit = (query or {}).get("size", size) or size
        offset = (query or {}).get("from", 0) or 0
        page = hits[offset : offset + limit]
        return {"hits": {"hits": page, "total": {"value": len(hits)}}}


def fuel_order_doc(
    order_id: str,
    *,
    tenant_id: str = "tenant-1",
    status: str = "confirmed",
    last_event_timestamp: str = "2026-07-29T12:00:00+00:00",
    **overrides: Any,
) -> Dict[str, Any]:
    """A valid stored fuel order document (``FuelOrder`` JSON dump)."""
    payload: Dict[str, Any] = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "customer_id": "customer-1",
        "customer_name": "Acme Fuels",
        "ship_to_address": "1 Depot Road",
        "ship_to_lat": 40.0,
        "ship_to_lon": -74.0,
        "customer_tank_id": f"tank-{order_id}",
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "call_type": "one_off",
        "delivery_window_start": "2026-07-30T08:00:00+00:00",
        "delivery_window_end": "2026-07-30T12:00:00+00:00",
        "intake_channel": "dispatcher",
        "intake_channel_id": "ch-1",
        "status": status,
        "source_schema_version": "1.0",
        "trace_id": "trace-1",
        "created_at": "2026-07-29T11:00:00+00:00",
        "updated_at": last_event_timestamp,
        "last_event_timestamp": last_event_timestamp,
    }
    payload.update(overrides)
    return FuelOrder(**payload).model_dump(mode="json")


__all__ = ["EVENTS", "InMemoryDocumentStore", "ORDERS", "fuel_order_doc"]
