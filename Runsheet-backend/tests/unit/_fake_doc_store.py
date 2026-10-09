"""A tiny in-memory document store for the data-export endpoint tests.

Evaluates the query subset those endpoints build (``match_all``, ``bool`` with
``must`` / ``filter`` / ``should`` + ``minimum_should_match``, ``term``,
``range``), multi-key ``sort`` with ``search_after``, and ``from`` / ``size``.
``total`` covers the query scope, excluding ``search_after``, as the real
store does. Range comparison is typed by the bound, like
``persistence.document_query._compare``: numeric bounds compare numerically
(missing/None never matches), string bounds compare as text.

It deliberately does NOT enforce tenant scope itself, so a test can prove the
endpoint's query carries the tenant filter, and seed a mislabelled document to
prove the per-row re-check drops it.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


def _get(doc: Dict[str, Any], field: str) -> Any:
    if field.endswith(".keyword"):
        field = field[: -len(".keyword")]
    return doc.get(field)


def _matches(doc: Dict[str, Any], q: Optional[Dict[str, Any]]) -> bool:
    if not q or "match_all" in q:
        return True
    if "bool" in q:
        b = q["bool"]
        for clause in list(b.get("must", [])) + list(b.get("filter", [])):
            if not _matches(doc, clause):
                return False
        should = b.get("should") or []
        if should:
            need = int(b.get("minimum_should_match", 1))
            if sum(1 for c in should if _matches(doc, c)) < need:
                return False
        return True
    if "term" in q:
        (field, value), = q["term"].items()
        if isinstance(value, dict):
            value = value.get("value")
        return _get(doc, field) == value
    if "range" in q:
        (field, bounds), = q["range"].items()
        actual = _get(doc, field)
        for op, bound in bounds.items():
            if actual is None:
                return False
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                try:
                    left: Any = float(actual)
                except (TypeError, ValueError):
                    return False
            else:
                left = str(actual)
            ok = {
                "gte": left >= bound, "gt": left > bound,
                "lte": left <= bound, "lt": left < bound,
            }[op]
            if not ok:
                return False
        return True
    raise AssertionError(f"unsupported clause in fake store: {q}")


class FakeDocStore:
    def __init__(self) -> None:
        self.docs: Dict[str, List[Dict[str, Any]]] = {}
        self.queries: List[Dict[str, Any]] = []

    def seed(self, index: str, docs: List[Dict[str, Any]]) -> None:
        self.docs.setdefault(index, []).extend(copy.deepcopy(d) for d in docs)

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100, request_timeout: int = 10):
        self.queries.append(copy.deepcopy(query))
        scope = [d for d in self.docs.get(index, []) if _matches(d, query.get("query"))]
        sort = query.get("sort") or []
        keys = [(next(iter(s)), next(iter(s.values())).get("order", "asc")) for s in sort]

        def key_values(doc):
            return [_get(doc, f) for f, _ in keys]

        # Multi-key sort: apply from the last key backwards (stable sort).
        ordered = list(scope)
        for field, order in reversed(keys):
            ordered.sort(key=lambda d, f=field: (_get(d, f) is None, _get(d, f)), reverse=(order == "desc"))
        if "search_after" in query:
            after = list(query["search_after"])
            assert len(after) == len(keys)

            def is_after(doc):
                for (field, order), boundary in zip(keys, after):
                    v = _get(doc, field)
                    if v == boundary:
                        continue
                    return v < boundary if order == "desc" else v > boundary
                return False

            ordered = [d for d in ordered if is_after(d)]
        start = int(query.get("from", 0) or 0)
        page_size = int(query.get("size", size))
        page = ordered[start:start + page_size]
        hits = []
        for d in page:
            hit = {"_id": d.get("_id"), "_source": copy.deepcopy(d)}
            if keys:
                hit["sort"] = key_values(d)
            hits.append(hit)
        return {"hits": {"total": {"value": len(scope), "relation": "eq"}, "hits": hits}}
