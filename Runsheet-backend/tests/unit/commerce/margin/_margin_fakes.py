"""In-memory fakes shared by the cost-entry and cost-basis tests.

``FakeDocStore`` interprets the subset of the Elasticsearch query DSL the
margin modules emit (``bool`` must/filter/should, ``term``, ``terms``,
``range``, ``exists``, ``match_all``, ``sort``, ``search_after``) and records
every call, so tests can count store queries and assert that each one carries
the caller's tenant filter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


def _field(doc: Mapping[str, Any], name: str) -> Any:
    value: Any = doc
    for part in name.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _comparable(value: Any) -> Tuple[int, Any]:
    if isinstance(value, bool):
        return (2, str(value))
    if isinstance(value, (int, float)):
        return (0, value)
    if isinstance(value, datetime):
        return (1, value.astimezone(timezone.utc))
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return (2, value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (1, parsed.astimezone(timezone.utc))
    return (3, str(value))


def _matches(doc: Mapping[str, Any], query: Optional[Mapping[str, Any]]) -> bool:
    if not query:
        return True
    (kind, body), = query.items()
    if kind == "match_all":
        return True
    if kind == "bool":
        for clause in list(body.get("must") or []) + list(body.get("filter") or []):
            if not _matches(doc, clause):
                return False
        for clause in body.get("must_not") or []:
            if _matches(doc, clause):
                return False
        should = body.get("should") or []
        if should:
            needed = body.get("minimum_should_match", 1 if not (body.get("must") or body.get("filter")) else 0)
            if sum(1 for clause in should if _matches(doc, clause)) < needed:
                return False
        return True
    if kind == "term":
        (name, value), = body.items()
        if isinstance(value, Mapping):
            value = value.get("value")
        return _field(doc, name) == value
    if kind == "terms":
        (name, values), = body.items()
        return _field(doc, name) in set(values)
    if kind == "exists":
        value = _field(doc, body["field"])
        return value is not None and value != ""
    if kind == "range":
        (name, bounds), = body.items()
        value = _field(doc, name)
        if value is None:
            return False
        left = _comparable(value)
        for op, bound in bounds.items():
            right = _comparable(bound)
            if left[0] != right[0]:
                return False
            if op == "gte" and not left[1] >= right[1]:
                return False
            if op == "gt" and not left[1] > right[1]:
                return False
            if op == "lte" and not left[1] <= right[1]:
                return False
            if op == "lt" and not left[1] < right[1]:
                return False
        return True
    raise AssertionError(f"FakeDocStore does not understand {kind!r}")


def _sort_spec(sort: Any) -> List[Tuple[str, bool]]:
    spec: List[Tuple[str, bool]] = []
    for item in sort or []:
        if isinstance(item, str):
            spec.append((item, False))
            continue
        (name, order), = item.items()
        if isinstance(order, Mapping):
            order = order.get("order", "asc")
        spec.append((name, order == "desc"))
    return spec


class _Desc:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: "_Desc") -> bool:
        return other.value < self.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Desc) and self.value == other.value


def _key(doc: Mapping[str, Any], spec: Sequence[Tuple[str, bool]]) -> tuple:
    return tuple(_Desc(_comparable(_field(doc, n))) if desc else _comparable(_field(doc, n)) for n, desc in spec)


def tenant_of_query(body: Mapping[str, Any]) -> Optional[str]:
    """The tenant a query is scoped to (``inject_tenant_filter`` or a bool must term)."""

    query = body.get("query") or {}
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if "term" in node and isinstance(node["term"], Mapping) and "tenant_id" in node["term"]:
                value = node["term"]["tenant_id"]
                found.append(value.get("value") if isinstance(value, Mapping) else value)
            bool_body = node.get("bool")
            if isinstance(bool_body, Mapping):
                for key in ("must", "filter"):
                    for clause in bool_body.get(key) or []:
                        walk(clause)

    walk(query)
    return found[0] if len(set(found)) == 1 else None


class FakeDocStore:
    """A tiny ES stand-in: ``search_documents(index, body, size)``."""

    def __init__(self) -> None:
        self.indices: Dict[str, List[Dict[str, Any]]] = {}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.raise_on: set[str] = set()

    def add(self, index: str, *docs: Dict[str, Any]) -> None:
        self.indices.setdefault(index, []).extend(dict(d) for d in docs)

    def calls_to(self, index: str) -> List[Dict[str, Any]]:
        return [body for name, body in self.calls if name == index]

    def reset_calls(self) -> None:
        self.calls.clear()

    async def search_documents(self, index: str, body: Dict[str, Any], size: int = 100, request_timeout: int = 10):
        self.calls.append((index, body))
        if index in self.raise_on:
            raise RuntimeError(f"store unavailable for {index}")
        docs = [d for d in self.indices.get(index, []) if _matches(d, body.get("query"))]
        spec = _sort_spec(body.get("sort"))
        if spec:
            docs.sort(key=lambda d: _key(d, spec))
        total = len(docs)
        after = body.get("search_after")
        if after is not None:
            marker = tuple(
                _Desc(_comparable(v)) if desc else _comparable(v) for v, (_, desc) in zip(after, spec)
            )
            docs = [d for d in docs if marker < _key(d, spec)]
        limit = int(body.get("size", size))
        page = docs[:limit]
        hits = []
        for doc in page:
            hit: Dict[str, Any] = {"_id": doc.get("bol_id") or doc.get("rack_price_id"), "_source": dict(doc)}
            if spec:
                hit["sort"] = [_field(doc, n) for n, _ in spec]
            hits.append(hit)
        return {"hits": {"total": {"value": total, "relation": "eq"}, "hits": hits}}


class CountingEntries:
    """Wraps a ``MarginRepository`` and counts the resolver's entry reads."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo
        self.calls: List[Tuple[str, str]] = []

    async def active_effective_entries(self, tenant_id: str, **kwargs: Any):
        self.calls.append(("active_effective_entries", tenant_id))
        return await self._repo.active_effective_entries(tenant_id, **kwargs)

    async def active_purchase_lots(self, tenant_id: str, **kwargs: Any):
        self.calls.append(("active_purchase_lots", tenant_id))
        return await self._repo.active_purchase_lots(tenant_id, **kwargs)

    async def active_entries_for_bols(self, tenant_id: str, bol_ids):
        self.calls.append(("active_entries_for_bols", tenant_id))
        return await self._repo.active_entries_for_bols(tenant_id, bol_ids)


class FakeTerminals:
    """``TerminalRepository.get(tenant_id, terminal_id)`` over a dict, counted."""

    def __init__(self, known: Mapping[str, Sequence[str]]) -> None:
        self._known = {tenant: set(ids) for tenant, ids in known.items()}
        self.calls: List[Tuple[str, str]] = []

    async def get(self, tenant_id: str, terminal_id: str):
        self.calls.append((tenant_id, terminal_id))
        if terminal_id in self._known.get(tenant_id, set()):
            return {"terminal_id": terminal_id, "tenant_id": tenant_id}
        return None


class CapturingTelemetry:
    """Records ``log_audit_event`` calls; ``fail=True`` makes the sink raise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: List[Dict[str, Any]] = []
        self.fail = fail

    def log_audit_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)
        if self.fail:
            raise RuntimeError("audit sink down")
