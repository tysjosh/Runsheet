"""Evaluate the supported Elasticsearch query DSL against a document in Python.

Companion to :mod:`persistence.document_query`, which compiles the same DSL to
SQL. Two callers need it for real:

* :mod:`persistence.document_aggregations` — the ``filter`` and ``filters``
  aggregations nest a query inside an aggregation, and re-issuing a SQL query per
  bucket would turn one request into dozens.
* the translator's property tests — generating random documents and random
  queries and asserting that Postgres and this module select the *same* set is
  the only verification that actually covers a DSL translation. A hand-written
  example per clause covers the clause; it does not cover the interactions
  (``must_not`` around a ``should`` around a ``range`` on a missing field) where
  translations go wrong.

The two implementations are deliberately independent — this one walks Python
values, the other emits SQL — so agreement between them is evidence rather than a
tautology. Keeping them in step is the job of the property test, not of shared
code.

Semantics follow :mod:`persistence.document_query` exactly, including the parts
that are easy to get wrong:

* ``term`` is exact and type-sensitive: ``1`` does not match ``"1"``, and ``True``
  does not match ``1``.
* ``term`` against a stored array matches if any element matches.
* a missing field never matches ``term`` / ``range`` / a text clause, and is not
  an error.
* ``should`` is required only when no ``must``/``filter`` is present.
* an unsupported clause raises, never silently passes or fails.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from persistence.document_query import UnsupportedQueryError, parse_date_math

__all__ = ["matches", "extract_values", "extract_value"]

_MISSING = object()


# ---------------------------------------------------------------------------
# Field access
# ---------------------------------------------------------------------------


def _resolve(document: Any, field: str) -> Any:
    """Walk a dotted path, returning :data:`_MISSING` when absent.

    A trailing ``.keyword`` is stripped for the same reason the SQL side strips
    it: it is a multi-field subfield of an analyzed field, and the stored value is
    the unanalyzed one.
    """
    if field.endswith(".keyword"):
        field = field[: -len(".keyword")]
    current = document
    for segment in field.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return _MISSING
    return current


def extract_values(document: Any, field: str) -> List[Any]:
    """Every value at ``field``, flattening one level of array.

    Elasticsearch treats a field holding ``[a, b]`` as the field having two
    values, which is why ``term`` matches either. Aggregations behave the same
    way: a ``terms`` aggregation on an array field counts each element. Returning
    a list keeps both callers honest about that rather than each remembering to
    handle it.
    """
    value = _resolve(document, field)
    if value is _MISSING or value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def extract_value(document: Any, field: str) -> Any:
    """The single value at ``field``, or ``None``. First element for arrays."""
    values = extract_values(document, field)
    return values[0] if values else None


# ---------------------------------------------------------------------------
# Comparison primitives
# ---------------------------------------------------------------------------


def _equal(stored: Any, wanted: Any) -> bool:
    """Exact, type-sensitive equality.

    ``bool`` is checked before ``int`` on purpose: in Python ``True == 1``, but
    Elasticsearch (and jsonb containment) treat a boolean and a number as
    different types, so letting Python's coercion through would make the SQL and
    Python paths disagree on ``{"term": {"enabled": 1}}``.
    """
    if isinstance(stored, bool) != isinstance(wanted, bool):
        return False
    if isinstance(stored, bool):
        return stored is wanted
    if isinstance(stored, (int, float)) and isinstance(wanted, (int, float)):
        return float(stored) == float(wanted)
    if isinstance(stored, str) != isinstance(wanted, str):
        return False
    return stored == wanted


def _compare(stored: Any, op: str, bound: Any) -> bool:
    """One range comparison, typed by the bound — matching the SQL translation.

    A numeric bound compares numerically; anything else compares as text. Stored
    values that cannot be coerced to the bound's type do not match, rather than
    raising: Elasticsearch skips a document whose field is the wrong type for the
    range, and a crash here would be a behaviour difference the SQL side does not
    have.
    """
    if isinstance(bound, (int, float)) and not isinstance(bound, bool):
        try:
            left: Any = float(stored)
        except (TypeError, ValueError):
            return False
        right: Any = float(bound)
    else:
        if isinstance(stored, bool) or not isinstance(stored, (str, int, float)):
            return False
        left = str(stored)
        right = str(bound)
    if op == "gte":
        return left >= right
    if op == "gt":
        return left > right
    if op == "lte":
        return left <= right
    if op == "lt":
        return left < right
    raise UnsupportedQueryError(f"range operator {op!r}", "range")


def _wildcard_to_regex(pattern: str) -> "re.Pattern":
    import re

    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.DOTALL)


# ---------------------------------------------------------------------------
# Clause evaluation
# ---------------------------------------------------------------------------


def matches(
    document: Dict[str, Any],
    query: Optional[Dict[str, Any]],
    *,
    doc_id: Optional[str] = None,
    now: Optional[datetime] = None,
    context: str = "query",
) -> bool:
    """Whether ``document`` satisfies ``query``.

    ``doc_id`` is required only for the ``ids`` clause; omitting it makes ``ids``
    match nothing rather than raise, which is the correct answer for a document
    with no id.
    """
    if not query:
        return True
    if not isinstance(query, dict):
        raise UnsupportedQueryError(f"{context} of type {type(query).__name__}", context)

    names = [k for k in query if not k.startswith("_")]
    if len(names) != 1:
        raise UnsupportedQueryError(
            f"{len(names)} clauses in one object ({sorted(names)})", context
        )
    name = names[0]
    body = query[name] or {}

    if name == "match_all":
        return True
    if name == "match_none":
        return False
    if name == "bool":
        return _bool(document, body, doc_id=doc_id, now=now)
    if name == "constant_score":
        return matches(
            document, body.get("filter"), doc_id=doc_id, now=now,
            context="constant_score.filter",
        )
    if name == "term":
        field, raw = _single_field(body, "term")
        wanted = raw.get("value") if isinstance(raw, dict) else raw
        if wanted is None:
            raise UnsupportedQueryError("term with a null value", "term")
        return any(_equal(v, wanted) for v in extract_values(document, field))
    if name == "terms":
        field, values = _single_field(body, "terms")
        if not isinstance(values, (list, tuple, set)):
            raise UnsupportedQueryError(
                f"terms with non-list value ({type(values).__name__})", "terms"
            )
        wanted_list = [v for v in values if v is not None]
        stored = extract_values(document, field)
        return any(_equal(s, w) for s in stored for w in wanted_list)
    if name == "ids":
        values = body.get("values")
        if not isinstance(values, (list, tuple, set)):
            raise UnsupportedQueryError("ids without a values list", "ids")
        return doc_id is not None and doc_id in set(values)
    if name == "exists":
        field = body.get("field")
        if not field:
            raise UnsupportedQueryError("exists without a field", "exists")
        return bool(extract_values(document, field))
    if name == "range":
        field, bounds = _single_field(body, "range")
        if not isinstance(bounds, dict):
            raise UnsupportedQueryError("range without a bounds object", "range")
        stored = extract_values(document, field)
        if not stored:
            return False
        for op, bound in bounds.items():
            if op in ("format", "time_zone", "boost", "relation"):
                continue
            if op not in ("gte", "gt", "lte", "lt"):
                raise UnsupportedQueryError(f"range operator {op!r}", "range")
            if isinstance(bound, bool):
                raise UnsupportedQueryError("range on a boolean bound", "range")
            resolved = parse_date_math(bound, now=now)
            # ES semantics for a multi-valued field: the document matches when
            # ANY value satisfies the bound. Each bound is checked independently
            # for that reason, which is what ES does too.
            if not any(_compare(s, op, resolved) for s in stored):
                return False
        return True
    if name in ("match", "match_phrase"):
        field, raw = _single_field(body, name)
        if isinstance(raw, dict):
            unsupported = set(raw) - {"query", "fuzziness", "operator", "boost", "type"}
            if unsupported:
                raise UnsupportedQueryError(f"match options {sorted(unsupported)}", "match")
            needle = raw.get("query")
        else:
            needle = raw
        if needle is None or needle == "":
            return True
        needle = str(needle).lower()
        return any(
            needle in str(v).lower() for v in extract_values(document, field)
        )
    if name == "multi_match":
        needle = body.get("query")
        fields = body.get("fields") or []
        if not fields:
            raise UnsupportedQueryError("multi_match without fields", "multi_match")
        if needle is None or needle == "":
            return True
        needle = str(needle).lower()
        for raw_field in fields:
            field = raw_field.split("^", 1)[0]
            if any(needle in str(v).lower() for v in extract_values(document, field)):
                return True
        return False
    if name == "wildcard":
        field, raw = _single_field(body, "wildcard")
        if isinstance(raw, dict):
            pattern = raw.get("value")
            case_insensitive = bool(raw.get("case_insensitive", False))
        else:
            pattern, case_insensitive = raw, False
        if pattern is None:
            raise UnsupportedQueryError("wildcard without a value", "wildcard")
        regex = _wildcard_to_regex(
            str(pattern).lower() if case_insensitive else str(pattern)
        )
        for value in extract_values(document, field):
            text = str(value).lower() if case_insensitive else str(value)
            if regex.match(text):
                return True
        return False
    if name == "prefix":
        field, raw = _single_field(body, "prefix")
        value = raw.get("value") if isinstance(raw, dict) else raw
        if value is None:
            raise UnsupportedQueryError("prefix without a value", "prefix")
        return any(
            str(v).startswith(str(value)) for v in extract_values(document, field)
        )
    raise UnsupportedQueryError(name, context)


def _bool(
    document: Dict[str, Any],
    body: Dict[str, Any],
    *,
    doc_id: Optional[str],
    now: Optional[datetime],
) -> bool:
    unsupported = set(body) - {
        "must", "filter", "should", "must_not", "minimum_should_match", "boost",
    }
    if unsupported:
        raise UnsupportedQueryError(f"bool options {sorted(unsupported)}", "bool")

    must = _as_list(body.get("must"), "bool.must")
    filt = _as_list(body.get("filter"), "bool.filter")
    should = _as_list(body.get("should"), "bool.should")
    must_not = _as_list(body.get("must_not"), "bool.must_not")

    for clause in (*must, *filt):
        if not matches(document, clause, doc_id=doc_id, now=now, context="bool"):
            return False
    for clause in must_not:
        if matches(document, clause, doc_id=doc_id, now=now, context="bool.must_not"):
            return False

    if should:
        satisfied = sum(
            1 for clause in should
            if matches(document, clause, doc_id=doc_id, now=now, context="bool.should")
        )
        minimum = body.get("minimum_should_match")
        if minimum is None:
            required = 0 if (must or filt) else 1
        elif isinstance(minimum, int):
            required = max(minimum, 0)
        else:
            raise UnsupportedQueryError(f"minimum_should_match={minimum!r}", "bool")
        if satisfied < required:
            return False
    return True


def _as_list(raw: Any, context: str) -> Sequence[Dict[str, Any]]:
    if raw is None:
        return ()
    if isinstance(raw, dict):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return raw
    raise UnsupportedQueryError(f"{context} of type {type(raw).__name__}", context)


def _single_field(body: Any, clause: str):
    if not isinstance(body, dict) or len(body) != 1:
        raise UnsupportedQueryError(
            f"{clause} with {0 if not isinstance(body, dict) else len(body)} fields",
            clause,
        )
    return next(iter(body.items()))
