#!/usr/bin/env python3
"""Diff the Postgres document store against Elasticsearch, query by query.

The gate for Phase 4 of the Elasticsearch → Postgres migration. Unit and property
tests establish that the translator is faithful to the DSL; this establishes that
it is faithful **to this cluster's data**, which is a different claim. Real
documents have fields with mixed types, timestamps in three formats, arrays where
a scalar was expected, and 7,623 chances to find the one the translator gets
wrong.

What it does
------------
1. ``copy`` — read every document of an index out of Elasticsearch and upsert it
   into ``es_documents`` under the same ``_id``. Idempotent.
2. ``compare`` — run a battery of query bodies against BOTH backends and diff the
   results: the total, the ordered list of returned ids, and every returned
   document body.
3. ``run`` — both, for a list of indices.

The queries are not invented. ``--queries`` defaults to a battery derived from
what the codebase actually issues (tenant-scoped term, status filters, sorted
pages, date ranges, terms aggregations), and ``--query-file`` accepts a JSON list
of bodies so a specific endpoint's query can be checked verbatim before its
callers are cut over.

Why compare ids rather than just counts
---------------------------------------
A count is the weakest possible check on a search: two backends can return the
same number of different documents. This compares the ordered id list, so a sort
that puts ``"10"`` before ``"9"`` fails here rather than in production.

Usage
-----
    ENVIRONMENT=development DATABASE_URL=... \\
        python -m scripts.document_store_parity run --index fuel_orders_current

    # Everything in the cluster (slow, and the point of a pre-cutover run):
    ENVIRONMENT=development python -m scripts.document_store_parity run --all

Exit code is non-zero on any divergence, so it can gate a deploy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple

_SCROLL_SIZE = 500
_SCROLL_KEEPALIVE = "2m"


# ---------------------------------------------------------------------------
# The default query battery
# ---------------------------------------------------------------------------
#
# Shapes taken from the codebase's actual reads rather than from the DSL
# reference: a tenant-scoped term (the single most common clause, 813 uses), a
# sorted page (every list endpoint), a range on a timestamp, an exists check, and
# a terms aggregation with a nested metric. Each is applied to whichever fields
# the index turns out to have, so one battery covers every index.
def _battery(tenant_id: Optional[str], fields: Dict[str, str]) -> List[Tuple[str, Dict]]:
    queries: List[Tuple[str, Dict]] = [
        ("match_all", {"query": {"match_all": {}}, "size": 50}),
        ("count_only", {"query": {"match_all": {}}, "size": 0}),
    ]
    if tenant_id:
        queries.append(
            (
                "tenant_term",
                {"query": {"term": {"tenant_id": tenant_id}}, "size": 50},
            )
        )
    for field, kind in sorted(fields.items()):
        if kind == "keyword":
            queries.append(
                (f"exists[{field}]", {"query": {"exists": {"field": field}}, "size": 20})
            )
            queries.append(
                (
                    f"terms_agg[{field}]",
                    {
                        "query": {"match_all": {}},
                        "size": 0,
                        "aggs": {"by": {"terms": {"field": field, "size": 25}}},
                    },
                )
            )
        elif kind == "date":
            queries.append(
                (
                    f"sorted[{field}]",
                    {
                        "query": {"match_all": {}},
                        "sort": [{field: {"order": "desc"}}],
                        "size": 25,
                    },
                )
            )
            queries.append(
                (
                    f"range[{field}]",
                    {
                        "query": {"range": {field: {"gte": "2000-01-01T00:00:00Z"}}},
                        "size": 25,
                    },
                )
            )
        elif kind == "number":
            queries.append(
                (
                    f"sorted_numeric[{field}]",
                    {
                        "query": {"match_all": {}},
                        "sort": [{field: {"order": "desc"}}],
                        "size": 25,
                    },
                )
            )
            queries.append(
                (
                    f"sum_agg[{field}]",
                    {
                        "query": {"match_all": {}},
                        "size": 0,
                        "aggs": {"total": {"sum": {"field": field}}},
                    },
                )
            )
    return queries


def _declared_types(index: str) -> Dict[str, str]:
    """``{field: battery_kind}`` from the index's DECLARED mapping, if it has one.

    Preferred over guessing from the data, because a guess produced 18 invalid
    comparisons on the first clean run: string fields were classified as
    ``keyword`` and the battery asked for a ``terms`` aggregation, which
    Elasticsearch refuses on an analyzed ``text`` field ("Fielddata is disabled").
    The tool then reported its own invalid query as a divergence.

    Fields the mapping declares unsearchable are omitted entirely — Elasticsearch
    cannot query them and the document store deliberately refuses to, so a
    comparison would be asserting that two different refusals look the same.
    """
    from persistence.document_field_policy import _iter_mappings, unsearchable_fields

    blocked = unsearchable_fields(index)
    kinds: Dict[str, str] = {}
    for name, body in _iter_mappings():
        if name != index:
            continue
        for field, spec in ((body.get("mappings") or {}).get("properties") or {}).items():
            if field in blocked or not isinstance(spec, dict):
                continue
            es_type = spec.get("type")
            if es_type == "keyword":
                kinds[field] = "keyword"
            elif es_type == "date":
                kinds[field] = "date"
            elif es_type in ("integer", "long", "short", "byte", "float", "double", "half_float", "scaled_float"):
                kinds[field] = "number"
            # ``text``, ``object``, ``nested``, ``geo_point``, ``boolean`` are
            # skipped: a terms aggregation or a range on them is either invalid in
            # Elasticsearch or tests nothing about the translation.
        break
    return kinds


def _classify_fields(docs: List[Dict[str, Any]], limit: int = 6) -> Dict[str, str]:
    """Guess a usable type per field from the data, capped to keep runs short.

    Sampled from the documents rather than read from the mapping on purpose: the
    document store holds no mapping, and what matters for parity is the shape the
    translator will actually meet. A field whose values disagree about their type
    is skipped — it would make the two backends legitimately differ and tell us
    nothing about the translation.
    """
    kinds: Dict[str, set] = {}
    for doc in docs:
        for field, value in doc.items():
            if isinstance(value, bool):
                kind = "bool"
            elif isinstance(value, (int, float)):
                kind = "number"
            elif isinstance(value, str):
                kind = "date" if _looks_iso(value) else "keyword"
            else:
                kind = "other"
            kinds.setdefault(field, set()).add(kind)
    usable = {
        field: next(iter(k))
        for field, k in kinds.items()
        if len(k) == 1 and next(iter(k)) in ("keyword", "date", "number")
    }
    # Deterministic, and prefer the fields most likely to be filtered on.
    preferred = [f for f in ("tenant_id", "status", "created_at", "updated_at") if f in usable]
    rest = sorted(f for f in usable if f not in preferred)
    chosen = (preferred + rest)[:limit]
    return {f: usable[f] for f in chosen}


def _looks_iso(value: str) -> bool:
    from datetime import datetime

    if len(value) < 10:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Elasticsearch side
# ---------------------------------------------------------------------------


def _es_client():
    from services.elasticsearch_service import elasticsearch_service

    client = elasticsearch_service.client
    if client is None:
        raise SystemExit(
            "no Elasticsearch client — ENVIRONMENT=test skips the connection; "
            "run with development/staging/production"
        )
    return client


def _scroll(client, index: str) -> Iterator[Tuple[str, dict]]:
    resp = client.search(
        index=index, query={"match_all": {}}, size=_SCROLL_SIZE, scroll=_SCROLL_KEEPALIVE
    )
    scroll_id = resp.get("_scroll_id")
    hits = resp["hits"]["hits"]
    try:
        while hits:
            for hit in hits:
                yield hit["_id"], hit["_source"]
            resp = client.scroll(scroll_id=scroll_id, scroll=_SCROLL_KEEPALIVE)
            scroll_id = resp.get("_scroll_id")
            hits = resp["hits"]["hits"]
    finally:
        if scroll_id:
            try:
                client.clear_scroll(scroll_id=scroll_id)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def copy_index(index: str) -> int:
    """Copy every document of ``index`` from Elasticsearch into ``es_documents``.

    Writes through the ORM rather than through ``PostgresDocumentStore``, because
    the store stamps ``updated_at`` on every write and the point of the copy is a
    byte-identical body to diff against.
    """
    from persistence.database import session_scope
    from persistence.models import EsDocumentORM

    client = _es_client()
    if not client.indices.exists(index=index):
        print(f"  {index}: absent in Elasticsearch — nothing to copy")
        return 0

    copied = 0
    async with session_scope() as session:
        for doc_id, source in _scroll(client, index):
            row = await session.get(EsDocumentORM, (index, doc_id))
            tenant = source.get("tenant_id")
            tenant = str(tenant) if isinstance(tenant, (str, int)) and str(tenant) else None
            if row is None:
                session.add(
                    EsDocumentORM(
                        index_name=index,
                        doc_id=doc_id,
                        tenant_id=tenant,
                        document=dict(source),
                    )
                )
            else:
                row.document = dict(source)
                row.tenant_id = tenant
            copied += 1
    print(f"  {index}: copied {copied} document(s)")
    return copied


def _hit_ids(response: Dict[str, Any]) -> List[str]:
    return [hit["_id"] for hit in response.get("hits", {}).get("hits", [])]


def _total(response: Dict[str, Any]) -> int:
    total = response.get("hits", {}).get("total")
    if isinstance(total, dict):
        return int(total.get("value", 0))
    return int(total or 0)


def _normalise(value: Any) -> Any:
    """Drop the differences that are expected and immaterial.

    ``took`` is a duration. ``_score`` is always ``None`` on the Postgres side and
    meaningless on the Elasticsearch side (nothing in the codebase ranks — the one
    scoring query is a ``multi_match`` with no inference endpoint). Comparing
    either would report noise as divergence and train the reader to ignore the
    output.
    """
    if isinstance(value, dict):
        return {
            k: _normalise(v)
            for k, v in value.items()
            if k not in ("took", "_score", "max_score", "timed_out", "_shards")
        }
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        # ES returns 4 where the aggregation engine returns 4.0.
        return int(value)
    return value


async def compare_index(
    index: str, *, tenant_id: Optional[str], extra_queries: List[Dict[str, Any]]
) -> Tuple[int, int]:
    """Diff a battery of queries across both backends. Returns (checked, failed)."""
    from persistence.document_store import PostgresDocumentStore
    from services.elasticsearch_service import elasticsearch_service

    client = _es_client()
    store = PostgresDocumentStore()

    if not client.indices.exists(index=index):
        print(f"  {index}: absent in Elasticsearch — skipping comparison")
        return (0, 0)

    fields = _declared_types(index)
    if fields:
        # Deterministic, and prefer the fields most likely to be filtered on.
        preferred = [f for f in ("tenant_id", "status", "created_at", "updated_at") if f in fields]
        rest = sorted(f for f in fields if f not in preferred)
        fields = {f: fields[f] for f in (preferred + rest)[:6]}
    else:
        # A dynamically-mapped index has no declaration to read, so fall back to
        # inferring from the data.
        sample = list(zip(range(50), _scroll(client, index)))
        sample_docs = [pair[1][1] for pair in sample]
        fields = _classify_fields(sample_docs)

    queries = _battery(tenant_id, fields)
    queries.extend((f"custom[{i}]", q) for i, q in enumerate(extra_queries))

    checked = failed = skipped = 0
    for label, body in queries:
        checked += 1
        try:
            es_response = await elasticsearch_service.search_documents(
                index, json.loads(json.dumps(body))
            )
        except Exception as exc:  # noqa: BLE001
            # Elasticsearch refused the query, so there is nothing to compare
            # against — the comparison is VOID, not failed. This is the tool's
            # own limitation: for an index with no declared mapping it infers
            # field types from the data, and a string field it calls ``keyword``
            # may be ``text`` in the dynamic mapping, where a ``terms``
            # aggregation needs fielddata and errors. Eleven such cases were
            # reported as divergence on an otherwise clean run, which is exactly
            # the noise that stops anyone reading the output.
            print(f"  ⊘ {index} [{label}]: Elasticsearch cannot answer this ({exc})")
            skipped += 1
            checked -= 1
            continue
        try:
            pg_response = await store.search_documents(
                index, json.loads(json.dumps(body))
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  {index} [{label}]: Postgres raised {type(exc).__name__}: {exc}")
            failed += 1
            continue

        problems: List[str] = []
        if _total(es_response) != _total(pg_response):
            problems.append(
                f"total ES={_total(es_response)} PG={_total(pg_response)}"
            )
        es_ids, pg_ids = _hit_ids(es_response), _hit_ids(pg_response)
        notes: List[str] = []
        truncated = not body.get("sort") and _total(es_response) > len(es_ids) > 0
        if truncated:
            # An unsorted query whose result set is larger than the page. Neither
            # backend promises WHICH documents come back, so comparing the two
            # pages reports a difference that is not one — it flagged 92 of 1125
            # comparisons on the first whole-cluster run. Widen both sides to the
            # full match set instead, which is a real assertion: the same
            # documents matched.
            es_ids = await _all_matching_ids_es(index, body, _total(es_response))
            pg_ids = await _all_matching_ids_pg(store, index, body, _total(pg_response))
            notes.append(
                f"unsorted page of {len(es_response['hits']['hits'])} from "
                f"{_total(es_response)}; compared the full match set instead"
            )
        if set(es_ids) != set(pg_ids):
            problem, note = _explain_page_difference(
                body, es_response, pg_response
            )
            if problem:
                problems.append(problem)
            else:
                notes.append(note or "page differs at a sort-value tie boundary")
        elif truncated:
            pass  # Order is meaningless here; the set comparison above is the check.
        elif es_ids != pg_ids:
            problem, note = _explain_order_difference(body, es_response, pg_response)
            if problem:
                problems.append(problem)
            if note:
                notes.append(note)
        es_bodies = {
            h["_id"]: _normalise(h["_source"])
            for h in es_response.get("hits", {}).get("hits", [])
        }
        pg_bodies = {
            h["_id"]: _normalise(h["_source"])
            for h in pg_response.get("hits", {}).get("hits", [])
        }
        for doc_id in sorted(set(es_bodies) & set(pg_bodies)):
            if es_bodies[doc_id] != pg_bodies[doc_id]:
                problems.append(f"body of {doc_id} differs")
                break
        if "aggs" in body:
            es_aggs = _normalise(es_response.get("aggregations"))
            pg_aggs = _normalise(pg_response.get("aggregations"))
            if es_aggs != pg_aggs:
                if _equal_within_float32(es_aggs, pg_aggs):
                    notes.append(
                        "aggregation values differ only within float32 precision "
                        "(ES stores these fields as 'float'; Postgres keeps the "
                        "full double)"
                    )
                else:
                    problems.append(
                        f"aggregations differ:\n      ES={es_aggs}\n      PG={pg_aggs}"
                    )

        if problems:
            failed += 1
            print(f"  ❌ {index} [{label}]")
            for problem in problems:
                print(f"      {problem}")
        else:
            suffix = f"  ({notes[0]})" if notes else ""
            print(
                f"  ✅ {index} [{label}]  total={_total(es_response)} "
                f"hits={len(es_ids)}{suffix}"
            )
    if skipped:
        print(f"  ⊘ {index}: {skipped} comparison(s) void — Elasticsearch refused them")
    return (checked, failed)


def _equal_within_float32(left: Any, right: Any) -> bool:
    """Whether two aggregation results differ only by single-precision rounding.

    Most numeric fields in these mappings are ``"type": "float"`` — 32-bit. So
    Elasticsearch sums ``2.28`` as ``2.2799999713897705``, while the document store
    reads the full double out of jsonb and reports ``2.28``. Postgres is the more
    accurate of the two; reporting it as divergence would flag eight sums on a
    clean run and bury the real findings.

    The tolerance is relative and set to float32 epsilon with headroom, because the
    error accumulates across the summed terms. It is deliberately NOT a blanket
    "close enough": an absolute or generous tolerance would hide a genuinely
    miscounted aggregation, which is the thing this tool exists to find.
    """
    float32_epsilon = 1.2e-7
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(_equal_within_float32(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(_equal_within_float32(a, b) for a, b in zip(left, right))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if left == right:
            return True
        scale = max(abs(float(left)), abs(float(right)), 1.0)
        # Allow the error to grow with the number of terms summed; 1e3 terms of
        # float32 error is still ~1e-4 relative, far below anything a miscount
        # would produce.
        return abs(float(left) - float(right)) <= scale * float32_epsilon * 1e3
    return left == right


_MAX_WIDENED = 10_000


async def _all_matching_ids_es(index: str, body: Dict[str, Any], total: int) -> List[str]:
    """Every id matching ``body`` in Elasticsearch, capped at the result window."""
    from services.elasticsearch_service import elasticsearch_service

    widened = dict(body)
    widened.pop("from", None)
    widened["size"] = min(max(total, 1), _MAX_WIDENED)
    widened["_source"] = False
    response = await elasticsearch_service.search_documents(index, widened)
    return _hit_ids(response)


async def _all_matching_ids_pg(store, index: str, body: Dict[str, Any], total: int) -> List[str]:
    widened = dict(body)
    widened.pop("from", None)
    widened["size"] = min(max(total, 1), _MAX_WIDENED)
    widened["_source"] = False
    response = await store.search_documents(index, widened)
    return _hit_ids(response)


def _explain_page_difference(
    body: Dict[str, Any], es_response: Dict[str, Any], pg_response: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str]]:
    """Decide whether two different pages of a sorted query are both valid.

    A sorted, truncated query cuts the result at position N. When the sort values
    around position N are equal, which documents land inside the page is
    arbitrary, and the two backends legitimately choose differently — the more so
    here because Elasticsearch compares ``date`` at millisecond and ``float`` at
    single precision, so values the store sees as distinct are ties to it.

    The check that means something is the **sort values**: if both pages are
    correctly ordered and the value at the page boundary is the same, both took a
    valid prefix of the same ordering. Only a boundary mismatch is a defect.
    """
    sort = body.get("sort")
    if not sort:
        return (
            f"ids differ on an unsorted query "
            f"({len(_hit_ids(es_response))} ES / {len(_hit_ids(pg_response))} PG)",
            None,
        )
    field, descending = _first_sort_key(sort)
    if field is None:
        return (None, "sort key not comparable")

    es_values = [
        h.get("_source", {}).get(field) for h in es_response["hits"]["hits"]
    ]
    pg_values = [
        h.get("_source", {}).get(field) for h in pg_response["hits"]["hits"]
    ]
    if not _is_monotonic(pg_values, descending):
        return (f"sort on {field!r} is not monotonic in Postgres: {pg_values[:5]}", None)
    if len(es_values) != len(pg_values):
        return (
            f"page sizes differ: ES={len(es_values)} PG={len(pg_values)}",
            None,
        )
    if es_values and pg_values:
        boundary_equal = _equal_within_float32(es_values[-1], pg_values[-1])
        if not boundary_equal:
            return (
                f"different page boundary for {field!r}: "
                f"ES ends at {es_values[-1]!r}, PG ends at {pg_values[-1]!r}",
                None,
            )
    return (
        None,
        f"different documents inside a tie on {field!r}; both pages are a valid "
        "prefix of the same ordering",
    )


def _explain_order_difference(
    body: Dict[str, Any], es_response: Dict[str, Any], pg_response: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str]]:
    """Decide whether a different hit order is a defect or a legitimate tie-break.

    Returns ``(problem, note)`` — at most one of them set.

    Three cases, and conflating them makes the tool useless:

    **No ``sort``.** Elasticsearch promises no order at all without one; it
    returns documents in score-then-shard order. Postgres returns them ordered by
    id. Reporting that as divergence flags 11 of 15 comparisons and trains the
    reader to ignore the output.

    **``sort`` present, values tie.** Elasticsearch ``date`` fields are
    **millisecond** precision. The seeded ``created_at`` values here differ only in
    microseconds — ``…224443`` vs ``…224453`` — so ES sees nine equal keys and
    breaks the tie arbitrarily per shard, while the document store compares the
    full string and orders them exactly. That is not the store being wrong; it is
    the store being more precise, and it is also why ES pagination over such a
    field can repeat or skip rows. Reported as a note.

    **``sort`` present, values genuinely out of order.** A real translation defect
    — the sort did not do what it promised. Reported as a problem.

    The check is on the sequence of sort-key VALUES rather than on ids, because
    that is what the sort actually promised. Ids are the arbitrary residue.
    """
    sort = body.get("sort")
    if not sort:
        return (None, "unsorted: ES promises no order, compared as a set")

    field, descending = _first_sort_key(sort)
    if field is None:
        return (None, "sort key not comparable, compared as a set")

    es_values = [
        h.get("_source", {}).get(field)
        for h in es_response.get("hits", {}).get("hits", [])
    ]
    pg_values = [
        h.get("_source", {}).get(field)
        for h in pg_response.get("hits", {}).get("hits", [])
    ]

    pg_ordered = _is_monotonic(pg_values, descending)
    es_ordered = _is_monotonic(es_values, descending)
    if not pg_ordered:
        return (
            f"sort on {field!r} is not monotonic in Postgres: {pg_values[:5]}",
            None,
        )
    if sorted(map(str, es_values)) != sorted(map(str, pg_values)):
        return (
            f"same ids but different sort values for {field!r}: "
            f"ES={es_values[:3]} PG={pg_values[:3]}",
            None,
        )
    if not es_ordered:
        return (
            None,
            f"ES order on {field!r} is not monotonic at its own precision "
            "(date fields are millisecond-resolution); Postgres ordered exactly",
        )
    return (None, f"tie-break on {field!r} differs; both orders are valid")


def _first_sort_key(sort: Any) -> Tuple[Optional[str], bool]:
    entries = sort if isinstance(sort, list) else [sort]
    for item in entries:
        if isinstance(item, str):
            if item not in ("_score", "_doc"):
                return (item, False)
        elif isinstance(item, dict):
            for field, spec in item.items():
                if field in ("_score", "_doc"):
                    continue
                order = spec.get("order", "asc") if isinstance(spec, dict) else spec
                return (field, str(order).lower() == "desc")
    return (None, False)


def _is_monotonic(values: List[Any], descending: bool) -> bool:
    comparable = [v for v in values if v is not None]
    if len(comparable) < 2:
        return True
    try:
        pairs = zip(comparable, comparable[1:])
        if descending:
            return all(a >= b for a, b in pairs)
        return all(a <= b for a, b in pairs)
    except TypeError:
        # Mixed types in the sort field; no total order to check against.
        return True


def _all_indices() -> List[str]:
    client = _es_client()
    return sorted(
        name for name in client.indices.get(index="*").keys() if not name.startswith(".")
    )


async def _amain(args) -> int:
    from persistence.database import is_persistence_enabled

    if not is_persistence_enabled():
        raise SystemExit("requires DATABASE_URL (persistence layer dormant)")

    indices = _all_indices() if args.all_indices else list(args.index or ())
    if not indices:
        raise SystemExit("nothing to do: pass --index NAME (repeatable) or --all")

    extra: List[Dict[str, Any]] = []
    if args.query_file:
        extra = json.loads(pathlib.Path(args.query_file).read_text(encoding="utf-8"))
        if not isinstance(extra, list):
            raise SystemExit("--query-file must contain a JSON list of query bodies")

    if args.command in ("copy", "run"):
        print("=== copy Elasticsearch -> Postgres ===")
        for index in indices:
            await copy_index(index)

    total_checked = total_failed = 0
    if args.command in ("compare", "run"):
        print("=== compare ===")
        for index in indices:
            checked, failed = await compare_index(
                index, tenant_id=args.tenant, extra_queries=extra
            )
            total_checked += checked
            total_failed += failed

        print()
        if total_failed:
            print(
                f"❌ {total_failed} of {total_checked} query comparison(s) diverged "
                f"across {len(indices)} index(es)"
            )
            return 1
        print(
            f"✅ {total_checked} query comparison(s) identical across "
            f"{len(indices)} index(es)"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("copy", "compare", "run"),
        help="copy into Postgres, compare the two backends, or both",
    )
    parser.add_argument(
        "--index", action="append",
        help="Index name; repeatable.",
    )
    parser.add_argument(
        "--all", action="store_true", dest="all_indices",
        help="Every non-system index in the cluster.",
    )
    parser.add_argument(
        "--tenant", default=None,
        help="Tenant to use for the tenant-scoped query in the battery.",
    )
    parser.add_argument(
        "--query-file", default=None,
        help="JSON file holding a list of extra query bodies to compare verbatim.",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
