"""Execute the supported Elasticsearch aggregations over already-fetched documents.

Aggregations run in Python, over the rows the SQL filter already selected, rather
than being compiled to SQL. That is a deliberate split, and the reason is
measurable rather than aesthetic:

* Filtering and sorting go to SQL, because they need indexes and they decide which
  rows are read.
* Aggregation does not need an index. Every aggregating query in this codebase
  passes ``size: 0`` and is scoped to one tenant's slice of an index; the largest
  index in the whole cluster holds 988 documents. So the input to an aggregation
  is small and bounded.
* ``date_histogram`` with nested sub-aggregations, ``top_hits``, and ES's
  epoch-millis treatment of ``min``/``max`` on date fields are each awkward in SQL
  and simple over Python values. Compiling them would trade a lot of correctness
  risk for throughput nobody needs.

The bound is enforced, not assumed: :data:`MAX_AGGREGATION_ROWS` caps the input
and :class:`AggregationInputTooLarge` is raised past it. A silently truncated
aggregation reports a number that looks plausible and is wrong, which is the
failure this whole migration keeps finding.

Supported
---------
Buckets: ``terms``, ``date_histogram``, ``range``, ``filter``, ``filters``,
``missing``, each with nested ``aggs`` to any depth.
Metrics: ``sum``, ``avg``, ``min``, ``max``, ``value_count``, ``cardinality``,
``stats``, ``top_hits``.

Not supported
-------------
Pipeline aggregations — ``bucket_script``, ``avg_bucket``, ``stats_bucket``,
``derivative`` — and script-valued metrics. All of them raise
:class:`UnsupportedAggregationError`. They appear in one module
(``notifications/services/communication_metrics_service.py``, six uses) and one
script-valued ``sum`` (``inventory/service.py``). Emulating a painless expression
would mean shipping an interpreter for a language whose semantics we would be
guessing at; those reads stay on Elasticsearch until they are rewritten as Python
post-processing over the bucket output, which is the better shape for them anyway.

``min`` / ``max`` on a date field
--------------------------------
Elasticsearch returns epoch milliseconds in ``value`` and the formatted date in
``value_as_string`` for a ``date`` field, and callers do arithmetic on ``value``.
This module reproduces that when the values parse as ISO-8601 timestamps, and
returns the raw comparable otherwise. Without it, a caller subtracting two
``min`` results would get a ``TypeError`` on strings instead of a duration.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from persistence.document_matcher import extract_values, matches
from persistence.document_query import UnsupportedQueryError

__all__ = [
    "AggregationInputTooLarge",
    "UnsupportedAggregationError",
    "MAX_AGGREGATION_ROWS",
    "run_aggregations",
]

#: Hard cap on documents fed to an aggregation. Two orders of magnitude above the
#: largest index in the cluster (988 documents), so it cannot fire on today's
#: data — it exists so that growth produces a loud failure and a decision, rather
#: than a slow request that quietly becomes a wrong number when someone later
#: adds a LIMIT to make it fast again.
MAX_AGGREGATION_ROWS = 50_000


class UnsupportedAggregationError(NotImplementedError):
    """An aggregation type this engine does not implement."""

    def __init__(self, agg_type: str, name: str) -> None:
        super().__init__(
            f"Elasticsearch aggregation {agg_type!r} (as {name!r}) is not "
            "supported by the Postgres document store. Rewrite it as Python "
            "post-processing over the bucket output, or keep this read on "
            "Elasticsearch."
        )
        self.agg_type = agg_type
        self.name = name


class AggregationInputTooLarge(RuntimeError):
    """More documents matched than the in-Python aggregation engine will accept."""

    def __init__(self, count: int) -> None:
        super().__init__(
            f"{count} documents matched, over the {MAX_AGGREGATION_ROWS} limit "
            "for in-Python aggregation. Narrow the filter, or move this "
            "aggregation into SQL — do NOT raise the cap without checking that "
            "the result is still computed over the full match set."
        )
        self.count = count


_METRICS = {
    "sum", "avg", "min", "max", "value_count", "cardinality", "stats", "top_hits",
}
_BUCKETS = {"terms", "date_histogram", "range", "filter", "filters", "missing"}
_PIPELINES = {
    "bucket_script", "bucket_selector", "bucket_sort", "avg_bucket", "sum_bucket",
    "min_bucket", "max_bucket", "stats_bucket", "extended_stats_bucket",
    "percentiles_bucket", "derivative", "cumulative_sum", "moving_avg",
    "moving_fn", "serial_diff",
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_aggregations(
    documents: Sequence[Tuple[str, Dict[str, Any]]],
    aggs: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Compute ``aggs`` over ``documents``, returning the ES ``aggregations`` shape.

    Args:
        documents: ``(doc_id, document)`` pairs — the full match set, not a page.
            The id is carried because ``top_hits`` reports it.
        aggs: The ``aggs`` / ``aggregations`` body.
        now: Reference time for any date math inside a nested ``filter``.

    Raises:
        AggregationInputTooLarge: past :data:`MAX_AGGREGATION_ROWS`.
        UnsupportedAggregationError: for pipeline and script-valued aggregations.
    """
    if len(documents) > MAX_AGGREGATION_ROWS:
        raise AggregationInputTooLarge(len(documents))
    return _run(list(documents), aggs or {}, now=now)


def _run(
    documents: List[Tuple[str, Dict[str, Any]]],
    aggs: Dict[str, Any],
    *,
    now: Optional[datetime],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for name, body in (aggs or {}).items():
        if not isinstance(body, dict):
            raise UnsupportedAggregationError(str(type(body).__name__), name)
        sub_aggs = body.get("aggs") or body.get("aggregations") or {}
        types = [k for k in body if k not in ("aggs", "aggregations", "meta")]
        if len(types) != 1:
            raise UnsupportedAggregationError(f"{len(types)} types {sorted(types)}", name)
        agg_type = types[0]
        spec = body[agg_type] or {}

        if agg_type in _PIPELINES:
            raise UnsupportedAggregationError(agg_type, name)
        if agg_type in _METRICS:
            if isinstance(spec, dict) and "script" in spec:
                # A painless expression as the metric's value source. Emulating it
                # means interpreting painless; refusing is the honest answer.
                raise UnsupportedAggregationError(f"{agg_type} with script", name)
            result[name] = _metric(agg_type, spec, documents)
            continue
        if agg_type in _BUCKETS:
            result[name] = _bucket(agg_type, spec, documents, sub_aggs, name, now=now)
            continue
        raise UnsupportedAggregationError(agg_type, name)
    return result


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _numeric_values(documents, field: str) -> List[float]:
    out: List[float] = []
    for _doc_id, doc in documents:
        for value in extract_values(doc, field):
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                out.append(float(value))
            elif isinstance(value, str):
                # ES coerces a numeric string on a numeric field. Non-numeric
                # strings are skipped, matching ES's behaviour of ignoring
                # documents whose value is not of the field's type.
                try:
                    out.append(float(value))
                except ValueError:
                    continue
    return out


def _comparable_values(documents, field: str) -> List[Any]:
    """Values for min/max: numbers as floats, ISO timestamps as epoch millis."""
    out: List[Any] = []
    for _doc_id, doc in documents:
        for value in extract_values(doc, field):
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                out.append(float(value))
            elif isinstance(value, str):
                millis = _iso_to_millis(value)
                out.append(millis if millis is not None else value)
    return out


def _iso_to_millis(value: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000.0


def _millis_to_iso(millis: float) -> str:
    return (
        datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _metric(agg_type: str, spec: Dict[str, Any], documents) -> Dict[str, Any]:
    if agg_type == "top_hits":
        return _top_hits(spec, documents)

    field = spec.get("field")
    if not field:
        raise UnsupportedAggregationError(f"{agg_type} without a field", agg_type)

    if agg_type == "value_count":
        # Counts VALUES, not documents: an array field contributes one per
        # element, which is what ES does.
        return {"value": sum(len(extract_values(d, field)) for _i, d in documents)}
    if agg_type == "cardinality":
        distinct = set()
        for _doc_id, doc in documents:
            for value in extract_values(doc, field):
                distinct.add(_hashable(value))
        return {"value": len(distinct)}

    if agg_type in ("min", "max"):
        values = _comparable_values(documents, field)
        if not values:
            # ES reports null, not 0, for an empty min/max. Reporting 0 would
            # read as a real measurement.
            return {"value": None}
        numeric = [v for v in values if isinstance(v, float)]
        if numeric and len(numeric) == len(values):
            chosen = min(numeric) if agg_type == "min" else max(numeric)
            out: Dict[str, Any] = {"value": chosen}
            if _looks_like_millis(documents, field):
                out["value_as_string"] = _millis_to_iso(chosen)
            return out
        strings = [str(v) for v in values]
        return {"value": min(strings) if agg_type == "min" else max(strings)}

    values = _numeric_values(documents, field)
    if agg_type == "sum":
        return {"value": math.fsum(values)}
    if agg_type == "avg":
        return {"value": (math.fsum(values) / len(values)) if values else None}
    if agg_type == "stats":
        if not values:
            return {"count": 0, "min": None, "max": None, "avg": None, "sum": 0.0}
        return {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "avg": math.fsum(values) / len(values),
            "sum": math.fsum(values),
        }
    raise UnsupportedAggregationError(agg_type, agg_type)


def _looks_like_millis(documents, field: str) -> bool:
    """Whether every value at ``field`` was an ISO-8601 timestamp string.

    Drives the ``value_as_string`` companion that ES emits for a date field. Kept
    a value-shape test rather than a mapping lookup because the document store
    holds no mapping — and because a field that is sometimes a timestamp and
    sometimes a number should not be reported as a date.
    """
    seen = False
    for _doc_id, doc in documents:
        for value in extract_values(doc, field):
            if isinstance(value, str) and _iso_to_millis(value) is not None:
                seen = True
            elif value is not None and not isinstance(value, bool):
                return False
    return seen


def _hashable(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        import json

        return json.dumps(value, sort_keys=True)
    return value


def _top_hits(spec: Dict[str, Any], documents) -> Dict[str, Any]:
    from persistence.document_query import apply_source_filter, resolve_source_filter

    unsupported = set(spec) - {"size", "_source", "sort", "from"}
    if unsupported:
        raise UnsupportedAggregationError(f"top_hits options {sorted(unsupported)}", "top_hits")
    size = int(spec.get("size", 3))
    ordered = _sort_documents(documents, spec.get("sort"))
    source_spec = resolve_source_filter(spec.get("_source"))
    hits = [
        {"_id": doc_id, "_score": None, "_source": apply_source_filter(doc, source_spec)}
        for doc_id, doc in ordered[: max(size, 0)]
    ]
    return {
        "hits": {
            "total": {"value": len(documents), "relation": "eq"},
            "max_score": None,
            "hits": hits,
        }
    }


def _sort_documents(documents, sort: Any):
    """Sort ``(doc_id, document)`` pairs by an ES sort spec, in Python.

    Only used inside ``top_hits``; the outer query's sort is done in SQL.
    """
    if not sort:
        return list(documents)
    entries: List[Tuple[str, bool]] = []
    raw = sort if isinstance(sort, (list, tuple)) else [sort]
    for item in raw:
        if isinstance(item, str):
            entries.append((item, False))
        elif isinstance(item, dict):
            for field, spec in item.items():
                order = spec.get("order", "asc") if isinstance(spec, dict) else spec
                entries.append((field, str(order).lower() == "desc"))
        else:
            raise UnsupportedAggregationError(
                f"top_hits sort entry {type(item).__name__}", "top_hits"
            )
    ordered = list(documents)
    # Stable successive sorts, least significant key first, so the composite
    # ordering matches a multi-key ORDER BY.
    for field, descending in reversed(entries):
        if field in ("_score", "_doc"):
            continue
        ordered.sort(
            key=lambda pair, f=field: _sort_key(pair[1], f), reverse=descending
        )
    return ordered


def _sort_key(document: Dict[str, Any], field: str) -> Tuple[int, Any]:
    """A total order that never compares str with float, and puts missing last."""
    values = extract_values(document, field)
    if not values:
        return (2, "")
    value = values[0]
    if isinstance(value, bool):
        return (1, str(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    return (1, str(value))


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


def _bucket(
    agg_type: str,
    spec: Dict[str, Any],
    documents,
    sub_aggs: Dict[str, Any],
    name: str,
    *,
    now,
) -> Dict[str, Any]:
    if agg_type == "filter":
        kept = [
            (doc_id, doc) for doc_id, doc in documents
            if matches(doc, spec, doc_id=doc_id, now=now)
        ]
        out = {"doc_count": len(kept)}
        out.update(_run(kept, sub_aggs, now=now))
        return out

    if agg_type == "filters":
        filters = spec.get("filters")
        if not isinstance(filters, dict):
            raise UnsupportedAggregationError("filters without a named map", name)
        buckets: Dict[str, Any] = {}
        for key, clause in filters.items():
            kept = [
                (doc_id, doc) for doc_id, doc in documents
                if matches(doc, clause, doc_id=doc_id, now=now)
            ]
            entry = {"doc_count": len(kept)}
            entry.update(_run(kept, sub_aggs, now=now))
            buckets[key] = entry
        return {"buckets": buckets}

    if agg_type == "missing":
        field = spec.get("field")
        if not field:
            raise UnsupportedAggregationError("missing without a field", name)
        kept = [
            (doc_id, doc) for doc_id, doc in documents
            if not extract_values(doc, field)
        ]
        out = {"doc_count": len(kept)}
        out.update(_run(kept, sub_aggs, now=now))
        return out

    if agg_type == "terms":
        return _terms_bucket(spec, documents, sub_aggs, name, now=now)

    if agg_type == "range":
        return _range_bucket(spec, documents, sub_aggs, name, now=now)

    if agg_type == "date_histogram":
        return _date_histogram(spec, documents, sub_aggs, name, now=now)

    raise UnsupportedAggregationError(agg_type, name)


def _terms_bucket(spec, documents, sub_aggs, name, *, now) -> Dict[str, Any]:
    field = spec.get("field")
    if not field:
        raise UnsupportedAggregationError("terms without a field", name)
    unsupported = set(spec) - {"field", "size", "order", "min_doc_count", "missing", "include", "exclude"}
    if unsupported:
        raise UnsupportedAggregationError(f"terms options {sorted(unsupported)}", name)

    size = int(spec.get("size", 10))
    min_doc_count = int(spec.get("min_doc_count", 1))
    missing = spec.get("missing")

    grouped: Dict[Any, List[Tuple[str, Dict[str, Any]]]] = {}
    for doc_id, doc in documents:
        values = extract_values(doc, field)
        if not values and missing is not None:
            values = [missing]
        for value in values:
            grouped.setdefault(_hashable(value), []).append((doc_id, doc))

    order = spec.get("order") or {"_count": "desc"}
    entries = [
        (key, group) for key, group in grouped.items() if len(group) >= min_doc_count
    ]
    entries = _order_buckets(entries, order, sub_aggs, name, now=now)

    buckets = []
    for key, group in entries[: size if size > 0 else None]:
        entry: Dict[str, Any] = {"key": key, "doc_count": len(group)}
        entry.update(_run(group, sub_aggs, now=now))
        buckets.append(entry)

    total_docs = sum(len(g) for _k, g in entries)
    shown = sum(b["doc_count"] for b in buckets)
    return {
        # ES reports these two so a caller can tell a truncated terms aggregation
        # from a complete one. Reproduced rather than zeroed, because a caller
        # that checks them is checking for exactly the truncation this engine can
        # also produce via ``size``.
        "doc_count_error_upper_bound": 0,
        "sum_other_doc_count": total_docs - shown,
        "buckets": buckets,
    }


def _order_buckets(entries, order, sub_aggs, name, *, now):
    """Apply a terms ``order`` spec: ``_count``, ``_key``, or a sub-agg value."""
    if isinstance(order, list):
        if len(order) != 1:
            raise UnsupportedAggregationError(f"terms order with {len(order)} keys", name)
        order = order[0]
    if not isinstance(order, dict) or len(order) != 1:
        raise UnsupportedAggregationError(f"terms order {order!r}", name)
    key, direction = next(iter(order.items()))
    descending = str(direction).lower() == "desc"

    if key == "_count":
        # Elasticsearch orders a terms aggregation by count in the requested
        # direction and breaks ties by key **ascending**, regardless of that
        # direction. Sorting on the tuple with ``reverse=descending`` reversed
        # both, so equal-count buckets came back key-descending — visible against
        # the live cluster as ``[C4, C3, C2, C1, C5]`` where ES returns
        # ``[C1, C2, C3, C4, C5]``. Same counts, different order, and any caller
        # taking "the top bucket" from a tie got a different answer.
        entries.sort(key=lambda pair: _order_key(pair[0]))
        entries.sort(key=lambda pair: len(pair[1]), reverse=descending)
    elif key == "_key":
        entries.sort(key=lambda pair: _order_key(pair[0]), reverse=descending)
    else:
        # Order by a sub-aggregation's value, e.g. ``{"total": "desc"}``.
        if key not in (sub_aggs or {}):
            raise UnsupportedAggregationError(f"terms order by unknown agg {key!r}", name)

        def sort_value(pair):
            computed = _run(pair[1], {key: sub_aggs[key]}, now=now)
            value = computed.get(key, {}).get("value")
            return (value is None, value if value is not None else 0)

        entries.sort(key=sort_value, reverse=descending)
    return entries


def _order_key(key: Any) -> Tuple[int, Any]:
    if isinstance(key, bool):
        return (1, str(key))
    if isinstance(key, (int, float)):
        return (0, float(key))
    return (1, str(key))


def _range_bucket(spec, documents, sub_aggs, name, *, now) -> Dict[str, Any]:
    field = spec.get("field")
    ranges = spec.get("ranges")
    if not field or not isinstance(ranges, list):
        raise UnsupportedAggregationError("range without a field and ranges", name)
    keyed = bool(spec.get("keyed", False))

    buckets: Any = {} if keyed else []
    for spec_entry in ranges:
        lower = spec_entry.get("from")
        upper = spec_entry.get("to")
        key = spec_entry.get("key") or _range_key(lower, upper)
        kept = []
        for doc_id, doc in documents:
            for value in extract_values(doc, field):
                numeric = _as_float(value)
                if numeric is None:
                    continue
                # ES ranges are [from, to): inclusive lower, exclusive upper.
                if lower is not None and numeric < float(lower):
                    continue
                if upper is not None and numeric >= float(upper):
                    continue
                kept.append((doc_id, doc))
                break
        entry: Dict[str, Any] = {"doc_count": len(kept)}
        if lower is not None:
            entry["from"] = float(lower)
        if upper is not None:
            entry["to"] = float(upper)
        entry.update(_run(kept, sub_aggs, now=now))
        if keyed:
            buckets[key] = entry
        else:
            entry["key"] = key
            buckets.append(entry)
    return {"buckets": buckets}


def _range_key(lower, upper) -> str:
    left = "*" if lower is None else f"{float(lower):g}"
    right = "*" if upper is None else f"{float(upper):g}"
    return f"{left}-{right}"


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        millis = _iso_to_millis(value)
        if millis is not None:
            return millis
        try:
            return float(value)
        except ValueError:
            return None
    return None


_CALENDAR_INTERVALS = {
    "minute": timedelta(minutes=1), "1m": timedelta(minutes=1),
    "hour": timedelta(hours=1), "1h": timedelta(hours=1),
    "day": timedelta(days=1), "1d": timedelta(days=1),
    "week": timedelta(weeks=1), "1w": timedelta(weeks=1),
}
_FIXED_UNIT = {
    "ms": timedelta(milliseconds=1), "s": timedelta(seconds=1),
    "m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1),
}


def _date_histogram(spec, documents, sub_aggs, name, *, now) -> Dict[str, Any]:
    field = spec.get("field")
    if not field:
        raise UnsupportedAggregationError("date_histogram without a field", name)
    unsupported = set(spec) - {
        "field", "calendar_interval", "fixed_interval", "interval", "min_doc_count",
        "format", "time_zone", "extended_bounds", "order",
    }
    if unsupported:
        raise UnsupportedAggregationError(
            f"date_histogram options {sorted(unsupported)}", name
        )

    interval = _resolve_interval(spec, name)
    min_doc_count = int(spec.get("min_doc_count", 0))

    grouped: Dict[float, List[Tuple[str, Dict[str, Any]]]] = {}
    for doc_id, doc in documents:
        for value in extract_values(doc, field):
            millis = _iso_to_millis(value) if isinstance(value, str) else _as_float(value)
            if millis is None:
                continue
            grouped.setdefault(_floor_to(millis, interval), []).append((doc_id, doc))
            break

    if not grouped:
        return {"buckets": []}

    step = interval.total_seconds() * 1000.0
    keys = sorted(grouped)
    if min_doc_count == 0:
        # ES fills the gaps between the first and last bucket when
        # ``min_doc_count`` is 0 (its default), and callers plot the result as a
        # time series — a missing interval would be drawn as a join between two
        # non-adjacent points rather than as a zero.
        filled: List[float] = []
        current = keys[0]
        while current <= keys[-1]:
            filled.append(current)
            current += step
        keys = filled

    buckets = []
    for key in keys:
        group = grouped.get(key, [])
        if len(group) < min_doc_count:
            continue
        entry: Dict[str, Any] = {
            "key": int(key),
            "key_as_string": _millis_to_iso(key),
            "doc_count": len(group),
        }
        entry.update(_run(group, sub_aggs, now=now))
        buckets.append(entry)
    return {"buckets": buckets}


def _resolve_interval(spec: Dict[str, Any], name: str) -> timedelta:
    raw = spec.get("calendar_interval") or spec.get("fixed_interval") or spec.get("interval")
    if raw is None:
        raise UnsupportedAggregationError("date_histogram without an interval", name)
    raw = str(raw)
    if raw in _CALENDAR_INTERVALS:
        return _CALENDAR_INTERVALS[raw]
    # ``month``/``quarter``/``year`` are calendar-aware and cannot be a fixed
    # timedelta. Refused rather than approximated: a 30-day "month" silently
    # shifts every bucket boundary after the first.
    if raw in ("month", "quarter", "year", "1M", "1q", "1y"):
        raise UnsupportedAggregationError(
            f"date_histogram calendar_interval {raw!r} (calendar-aware)", name
        )
    for suffix, unit in sorted(_FIXED_UNIT.items(), key=lambda kv: -len(kv[0])):
        if raw.endswith(suffix):
            amount = raw[: -len(suffix)] or "1"
            try:
                return unit * int(amount)
            except ValueError:
                break
    raise UnsupportedAggregationError(f"date_histogram interval {raw!r}", name)


def _floor_to(millis: float, interval: timedelta) -> float:
    step = interval.total_seconds() * 1000.0
    return math.floor(millis / step) * step


def collect_aggregation_fields(aggs: Optional[Dict[str, Any]]) -> List[str]:
    """Every document field an aggregation body reads, at any nesting depth.

    Feeds :mod:`persistence.document_field_policy`. An aggregation is as much a
    read of a field as a filter is — Elasticsearch errors outright when asked to
    aggregate a ``binary`` field — so the same refusal has to cover both, or the
    guard is bypassed by asking for a ``terms`` bucket instead of a ``term``
    filter.
    """
    from persistence.document_query import collect_query_fields

    found: List[str] = []
    for body in (aggs or {}).values():
        if not isinstance(body, dict):
            continue
        for agg_type, spec in body.items():
            if agg_type in ("aggs", "aggregations"):
                found.extend(collect_aggregation_fields(spec))
                continue
            if agg_type == "meta":
                continue
            if agg_type == "filter":
                found.extend(collect_query_fields(spec))
                continue
            if agg_type == "filters":
                for clause in (spec or {}).get("filters", {}).values():
                    found.extend(collect_query_fields(clause))
                continue
            if isinstance(spec, dict):
                if spec.get("field"):
                    found.append(spec["field"])
                # ``top_hits`` can carry its own sort.
                for entry in spec.get("sort") or ():
                    if isinstance(entry, str):
                        found.append(entry)
                    elif isinstance(entry, dict):
                        found.extend(entry.keys())
    return [f for f in found if f not in ("_score", "_doc", "_count", "_key")]
