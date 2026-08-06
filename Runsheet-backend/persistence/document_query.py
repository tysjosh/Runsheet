"""Translate the Elasticsearch query DSL this codebase actually uses into SQL.

Phase 2 of the Elasticsearch → Postgres migration. The translator is deliberately
scoped to the measured DSL surface rather than to the DSL:

===================  =====  ==================================================
Clause               Uses   Handling
===================  =====  ==================================================
``term``              813   ``document @> {"field": value}`` (GIN-indexed)
``bool``              388   ``must``/``filter`` AND, ``should`` OR, ``must_not`` NOT
``terms``             168   OR of containment
``range``             115   type-aware comparison, with ``now-7d`` date math
``match_all``          75   no predicate
``exists``             38   ``document ? 'field'`` and not JSON null
``ids``                 –   primary-key IN
``match``              10   substring, case-insensitive (see below)
``multi_match``         6   substring across fields, ORed
``wildcard``            4   ``*``/``?`` translated to ``LIKE`` metacharacters
``prefix``              –   left-anchored substring
===================  =====  ==================================================

Everything else — ``script``, ``nested``, ``query_string``, ``geo_*``,
``function_score``, ``more_like_this`` — raises
:class:`UnsupportedQueryError`. That is the single most important decision in this
module. A translator that silently ignores a clause it does not understand
returns *wrong rows*, and a caller cannot tell the difference between "no
documents matched" and "your filter was dropped". Every silent-empty bug this
codebase has produced came from that shape, so an unsupported clause fails loudly
with the clause named.

Two semantics worth stating because they are easy to get subtly wrong:

**``term`` is exact, not analyzed.** In Elasticsearch, ``term`` against a ``text``
field matches analyzed tokens, and against a ``keyword`` field matches the whole
value. Every strict mapping in this codebase types its filterable fields as
``keyword``, so exact match is the faithful translation. The two exceptions are
the legacy dynamically-mapped ``trucks`` and ``locations`` indices, where
``tenant_id`` came out as ``text`` and a ``term`` query therefore matches
*nothing* today — the application post-filters those in Python precisely because
of it. Exact match is strictly more correct there.

**``term`` against an array matches any element.** ``{"term": {"allowed_grades":
"DIESEL_2"}}`` matches a document whose ``allowed_grades`` is
``["DIESEL_2", "GASOLINE_REG"]``. jsonb containment does this natively, which is
why containment is used rather than ``->>`` equality.

**``should`` is required only when nothing else is.** With no ``must``/``filter``
present, Elasticsearch requires at least one ``should`` clause to match; with a
``must`` present, ``should`` only affects scoring and matches nothing extra. We
do not score, so ``should`` is translated as a required OR in the first case and
dropped in the second. ``minimum_should_match`` is honoured when given.

Text matching (``match`` / ``multi_match`` / ``wildcard`` / ``prefix``) is
case-insensitive substring, i.e. ``ILIKE '%term%'``. It is NOT tokenised, so it
does not reproduce stemming, ``fuzziness``, or per-term scoring. The four call
sites that pass ``fuzziness: AUTO`` get substring behaviour instead; that is a
behaviour change and is called out in
``docs/elasticsearch-to-postgres-migration.md`` rather than hidden here.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import (
    Boolean,
    Float,
    and_,
    cast,
    false,
    func,
    not_,
    or_,
    true,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import ColumnElement

__all__ = [
    "UnsupportedQueryError",
    "build_predicate",
    "build_order_by",
    "collect_query_fields",
    "collect_sort_fields",
    "parse_date_math",
    "resolve_source_filter",
]


class UnsupportedQueryError(NotImplementedError):
    """A query clause the translator does not implement.

    Raised rather than ignored. The caller sees which clause and where, so an
    unsupported query surfaces as a loud failure during the soak instead of as a
    silently wrong result set in production.
    """

    def __init__(self, clause: str, context: str = "query") -> None:
        super().__init__(
            f"Elasticsearch clause {clause!r} in {context} is not supported by "
            "the Postgres document store. Add a translation in "
            "persistence.document_query or keep this read on Elasticsearch."
        )
        self.clause = clause
        self.context = context


# ---------------------------------------------------------------------------
# Date math
# ---------------------------------------------------------------------------

#: ``now``, ``now-7d``, ``now+30m``. Rounding (``now/d``) is not used anywhere in
#: the codebase and is rejected rather than approximated.
_DATE_MATH = re.compile(r"^now(?:(?P<sign>[+-])(?P<amount>\d+)(?P<unit>[smhdwMy]))?$")

_UNIT_DELTA = {
    "s": lambda n: timedelta(seconds=n),
    "m": lambda n: timedelta(minutes=n),
    "h": lambda n: timedelta(hours=n),
    "d": lambda n: timedelta(days=n),
    "w": lambda n: timedelta(weeks=n),
    # Calendar months/years are approximated by their common lengths. Only ``d``,
    # ``h`` and ``m`` appear in the codebase; these two exist so a future caller
    # gets a defined answer instead of a crash.
    "M": lambda n: timedelta(days=30 * n),
    "y": lambda n: timedelta(days=365 * n),
}


def parse_date_math(value: Any, *, now: Optional[datetime] = None) -> Any:
    """Resolve an Elasticsearch date-math expression to an ISO-8601 string.

    Non-``now`` values pass through untouched, so numbers and literal timestamps
    are unaffected. ``now/d``-style rounding raises rather than silently being
    treated as ``now``: a rounded bound and an unrounded one select different
    rows, and guessing which the caller meant is how an off-by-one-day window
    ships unnoticed.
    """
    if not isinstance(value, str) or not value.startswith("now"):
        return value

    match = _DATE_MATH.match(value)
    if match is None:
        raise UnsupportedQueryError(f"date math {value!r}", "range")

    reference = now or datetime.now(timezone.utc)
    sign, amount, unit = match.group("sign"), match.group("amount"), match.group("unit")
    if sign is None:
        return reference.isoformat()
    delta = _UNIT_DELTA[unit](int(amount))
    resolved = reference - delta if sign == "-" else reference + delta
    return resolved.isoformat()


# ---------------------------------------------------------------------------
# Field access helpers
# ---------------------------------------------------------------------------


def _path(field: str) -> List[str]:
    """Split a dotted Elasticsearch field path into jsonb key segments.

    ``cargo_manifest.description`` addresses a nested object in jsonb exactly as
    it addresses a nested object in Elasticsearch. A trailing ``.keyword`` is
    stripped: it is a multi-field subfield that exists only because the field was
    analyzed as ``text``, and the underlying jsonb value is the unanalyzed one.
    """
    if field.endswith(".keyword"):
        field = field[: -len(".keyword")]
    return field.split(".")


def as_jsonb(column):
    """Re-type a column expression as ``JSONB`` without emitting a CAST.

    ``EsDocumentORM.document`` is declared as the portable
    ``JSON().with_variant(JSONB(), "postgresql")`` so ``Base.metadata.create_all``
    still works against the SQLite database the rest of the test suite uses. The
    cost is that SQLAlchemy resolves the *generic JSON* comparator at expression
    build time, and that comparator means different things:

      * ``contains`` compiles to string ``LIKE '%' || x || '%'`` rather than the
        jsonb containment operator ``@>``. PostgreSQL then rejects the statement
        with ``invalid input syntax for type json``, because ``%`` is not JSON.
      * ``has_key`` does not exist at all.
      * ``->`` on ``json`` yields ``json``, which has **no comparison operators**
        in PostgreSQL, so an ORDER BY on it fails.

    ``type_coerce`` fixes all three by telling SQLAlchemy the expression is JSONB
    — which it is, on PostgreSQL — without adding a runtime cast that would defeat
    the GIN index. A property test comparing SQL to the Python matcher is what
    surfaced this; the first version of the translator used ``contains`` directly
    and every ``term`` query raised.
    """
    return type_coerce(column, JSONB)


def _json_value(column, field: str):
    """A jsonb handle to ``field``, preserving jsonb type and ordering."""
    element = as_jsonb(column)
    for segment in _path(field):
        element = element[segment]
    return element


def _json_text(column, field: str):
    """``field`` as text, for pattern matching and text comparison."""
    return _json_value(column, field).as_string()


def _containment(column, field: str, value: Any):
    """``document @> {"field": value}``, nested paths included.

    Containment is the workhorse: exact, type-aware (``1`` does not match
    ``"1"``), and answered by the GIN index.

    It is issued **twice** — once against the scalar and once against a
    single-element array — because jsonb containment does not do what
    Elasticsearch's ``term`` does on a multi-valued field. In Elasticsearch,
    ``{"term": {"grade": "DIESEL_2"}}`` matches a document whose ``grade`` is
    ``["DIESEL_2", "GASOLINE_REG"]``: a field holding an array is a field with
    several values. In PostgreSQL::

        '{"grade": ["DIESEL_2"]}'::jsonb @> '{"grade": "DIESEL_2"}'::jsonb  -- false
        '{"grade": ["DIESEL_2"]}'::jsonb @> '{"grade": ["DIESEL_2"]}'::jsonb -- true
        '{"grade": "DIESEL_2"}'::jsonb   @> '{"grade": ["DIESEL_2"]}'::jsonb -- false

    (The scalar-matches-array shortcut PostgreSQL documents applies only at the
    top level, not under a key.) So neither form alone covers both storage shapes,
    and the OR does. The property test found this against
    ``truck_compartments.allowed_grades``, which is exactly such a field and is
    filtered by ``term`` in production.

    Both branches are GIN-indexable, so the OR costs an index probe, not a scan.
    """
    segments = _path(field)

    def _wrap(leaf: Any) -> Any:
        payload: Any = leaf
        for segment in reversed(segments):
            payload = {segment: payload}
        return payload

    jsonb_column = as_jsonb(column)
    scalar_form = jsonb_column.contains(_wrap(value))
    if isinstance(value, (list, tuple, dict)):
        # A non-scalar term value has no "wrapped in an array" variant to try;
        # ES would reject it anyway.
        return scalar_form
    return or_(scalar_form, jsonb_column.contains(_wrap([value])))


# ---------------------------------------------------------------------------
# Leaf clauses
# ---------------------------------------------------------------------------


def _term(column, body: Dict[str, Any]) -> ColumnElement:
    field, raw = _single_field(body, "term")
    value = raw.get("value") if isinstance(raw, dict) else raw
    if value is None:
        # ES rejects a null term outright. Translating it as "field is null"
        # would quietly match documents the ES query would have errored on.
        raise UnsupportedQueryError("term with a null value", "term")
    return _containment(column, field, value)


def _terms(column, body: Dict[str, Any]) -> ColumnElement:
    field, values = _single_field(body, "terms")
    if not isinstance(values, (list, tuple, set)):
        raise UnsupportedQueryError(f"terms with non-list value ({type(values).__name__})", "terms")
    values = [v for v in values if v is not None]
    if not values:
        # ES: an empty terms list matches nothing. Returning ``true`` here would
        # widen the query to every document, which is the dangerous direction.
        return _never()
    return or_(*[_containment(column, field, v) for v in values])


def _ids(column, body: Dict[str, Any], id_column) -> ColumnElement:
    values = body.get("values")
    if not isinstance(values, (list, tuple, set)):
        raise UnsupportedQueryError("ids without a values list", "ids")
    values = list(values)
    if not values:
        return _never()
    return id_column.in_(values)


def _exists(column, body: Dict[str, Any]) -> ColumnElement:
    field = body.get("field")
    if not field:
        raise UnsupportedQueryError("exists without a field", "exists")
    segments = _path(field)
    if len(segments) == 1:
        present = as_jsonb(column).has_key(segments[0])  # noqa: W601 — jsonb API
    else:
        # ``has_key`` only applies to the top level, so a nested path is checked
        # by extracting it. Less index-friendly, and only two call sites need it.
        present = _json_value(column, field).is_not(None)

    value = _json_value(column, field)
    kind = func.jsonb_typeof(value)
    # Elasticsearch's ``exists`` is about having a VALUE, not about having a key.
    # Two shapes have a key and no value, and both occur in this cluster:
    #
    #   * an explicit JSON ``null``;
    #   * an EMPTY ARRAY. ``mvp_tank_forecasts`` has two documents with
    #     ``anomaly_flags: []``, and ES excludes them from ``exists`` — an empty
    #     array is zero values. An earlier version tested ``->>'field' IS NOT
    #     NULL``, which returns the *text* ``'[]'`` for an array and so reported
    #     both documents as present. The whole-cluster parity run caught it:
    #     ES=1981, PG=1983.
    #
    # ``jsonb_typeof`` states the distinction directly instead of relying on what
    # ``->>`` happens to render.
    return and_(
        present,
        value.is_not(None),
        kind != "null",
        or_(kind != "array", func.jsonb_array_length(value) > 0),
    )


def _range(column, body: Dict[str, Any], *, now: Optional[datetime]) -> ColumnElement:
    field, bounds = _single_field(body, "range")
    if not isinstance(bounds, dict):
        raise UnsupportedQueryError("range without a bounds object", "range")

    clauses: List[ColumnElement] = []
    for op, bound in bounds.items():
        if op in ("format", "time_zone", "boost", "relation"):
            # Presentation-only in ES for the comparisons we make; ignoring them
            # cannot change which rows match.
            continue
        if op not in ("gte", "gt", "lte", "lt"):
            raise UnsupportedQueryError(f"range operator {op!r}", "range")
        bound = parse_date_math(bound, now=now)
        clauses.append(_compare(column, field, op, bound))
    if not clauses:
        return true()
    return and_(*clauses)


def _compare(column, field: str, op: str, bound: Any) -> ColumnElement:
    """One range comparison, typed by the bound rather than by a schema.

    A numeric bound compares numerically and a string bound compares as text.
    That is not a shortcut: the codebase stores timestamps as ISO-8601 strings
    and relies on their lexical order matching chronological order (the hybrid
    read repository says so explicitly), while quantities are stored as JSON
    numbers. Typing by the bound gives the right comparison for both without
    needing to know the field's mapping.
    """
    if isinstance(bound, bool):
        raise UnsupportedQueryError("range on a boolean bound", "range")
    if isinstance(bound, (int, float)):
        left = cast(_json_text(column, field), Float)
        right = float(bound)
    else:
        left = _json_text(column, field)
        right = bound
    return {
        "gte": left >= right,
        "gt": left > right,
        "lte": left <= right,
        "lt": left < right,
    }[op]


def _like_pattern(text: str) -> str:
    """Escape LIKE metacharacters in a user-supplied substring."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _match(column, body: Dict[str, Any]) -> ColumnElement:
    field, raw = _single_field(body, "match")
    if isinstance(raw, dict):
        query = raw.get("query")
        # ``fuzziness`` is accepted and NOT honoured — substring matching is
        # applied instead. Rejecting it would break four working call sites for
        # a difference none of them depends on; silently promising fuzzy matching
        # would be worse. The behaviour change is documented in the migration doc.
        unsupported = set(raw) - {"query", "fuzziness", "operator", "boost", "type"}
        if unsupported:
            raise UnsupportedQueryError(
                f"match options {sorted(unsupported)}", "match"
            )
    else:
        query = raw
    if query is None or query == "":
        return true()
    return _json_text(column, field).ilike(f"%{_like_pattern(str(query))}%")


def _multi_match(column, body: Dict[str, Any]) -> ColumnElement:
    query = body.get("query")
    fields = body.get("fields") or []
    if not fields:
        raise UnsupportedQueryError("multi_match without fields", "multi_match")
    if query is None or query == "":
        return true()
    pattern = f"%{_like_pattern(str(query))}%"
    # ``^boost`` suffixes are stripped: they only affect ranking, and nothing
    # here ranks.
    names = [f.split("^", 1)[0] for f in fields]
    return or_(*[_json_text(column, f).ilike(pattern) for f in names])


def _wildcard(column, body: Dict[str, Any]) -> ColumnElement:
    field, raw = _single_field(body, "wildcard")
    if isinstance(raw, dict):
        pattern = raw.get("value")
        case_insensitive = bool(raw.get("case_insensitive", False))
    else:
        pattern, case_insensitive = raw, False
    if pattern is None:
        raise UnsupportedQueryError("wildcard without a value", "wildcard")
    # ES wildcards: ``*`` any sequence, ``?`` any single character. Translate to
    # LIKE's ``%`` / ``_`` after escaping the LIKE metacharacters, so a literal
    # ``%`` in the pattern stays literal.
    like = _like_pattern(str(pattern)).replace("*", "%").replace("?", "_")
    column_text = _json_text(column, field)
    return column_text.ilike(like) if case_insensitive else column_text.like(like)


def _prefix(column, body: Dict[str, Any]) -> ColumnElement:
    field, raw = _single_field(body, "prefix")
    value = raw.get("value") if isinstance(raw, dict) else raw
    if value is None:
        raise UnsupportedQueryError("prefix without a value", "prefix")
    return _json_text(column, field).like(f"{_like_pattern(str(value))}%")


# ---------------------------------------------------------------------------
# Compound clauses
# ---------------------------------------------------------------------------


def _bool(column, body: Dict[str, Any], id_column, *, now) -> ColumnElement:
    unsupported = set(body) - {
        "must", "filter", "should", "must_not", "minimum_should_match", "boost",
    }
    if unsupported:
        raise UnsupportedQueryError(f"bool options {sorted(unsupported)}", "bool")

    must = _clause_list(column, body.get("must"), id_column, now=now, context="bool.must")
    filt = _clause_list(column, body.get("filter"), id_column, now=now, context="bool.filter")
    should = _clause_list(column, body.get("should"), id_column, now=now, context="bool.should")
    must_not = _clause_list(column, body.get("must_not"), id_column, now=now, context="bool.must_not")

    required: List[ColumnElement] = [*must, *filt]
    required.extend(not_(clause) for clause in must_not)

    if should:
        minimum = body.get("minimum_should_match")
        if minimum is None:
            # ES: should is required only when there is no must/filter to satisfy.
            # With a must present it contributes to score alone, and we do not
            # score, so it selects nothing extra.
            if not must and not filt:
                required.append(or_(*should))
        elif isinstance(minimum, int) and minimum <= 0:
            # ``minimum_should_match: 0`` makes the should clauses entirely
            # optional — they contribute to score and nothing else. Treating it
            # as 1 (which an earlier version did) narrowed the result set, and
            # the property test caught the disagreement on a document that
            # matched no should clause.
            pass
        elif isinstance(minimum, int) and minimum == 1:
            required.append(or_(*should))
        elif isinstance(minimum, int):
            # "at least N of these" needs a count, not a boolean OR. Expressing
            # it as an OR would match documents satisfying only one clause.
            required.append(
                func.coalesce(
                    sum(_as_int(clause) for clause in should), 0
                ) >= minimum
            )
        else:
            raise UnsupportedQueryError(
                f"minimum_should_match={minimum!r}", "bool"
            )

    if not required:
        return true()
    return and_(*required)


def _as_int(clause: ColumnElement):
    """A boolean predicate as 0/1, so ``should`` clauses can be counted."""
    from sqlalchemy import Integer, case

    return case((clause, 1), else_=0).cast(Integer)


def _clause_list(
    column, raw: Any, id_column, *, now, context: str
) -> List[ColumnElement]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise UnsupportedQueryError(f"{context} of type {type(raw).__name__}", context)
    return [
        build_predicate(column, clause, id_column=id_column, now=now, context=context)
        for clause in raw
    ]


_LEAF_HANDLERS = {
    "term": lambda column, body, id_column, now: _term(column, body),
    "terms": lambda column, body, id_column, now: _terms(column, body),
    "ids": lambda column, body, id_column, now: _ids(column, body, id_column),
    "exists": lambda column, body, id_column, now: _exists(column, body),
    "range": lambda column, body, id_column, now: _range(column, body, now=now),
    "match": lambda column, body, id_column, now: _match(column, body),
    "match_phrase": lambda column, body, id_column, now: _match(column, body),
    "multi_match": lambda column, body, id_column, now: _multi_match(column, body),
    "wildcard": lambda column, body, id_column, now: _wildcard(column, body),
    "prefix": lambda column, body, id_column, now: _prefix(column, body),
    "match_all": lambda column, body, id_column, now: true(),
    "match_none": lambda column, body, id_column, now: _never(),
}


def build_predicate(
    column,
    query: Optional[Dict[str, Any]],
    *,
    id_column,
    now: Optional[datetime] = None,
    context: str = "query",
) -> ColumnElement:
    """Compile one Elasticsearch query clause into a SQL predicate.

    Args:
        column: The jsonb ``document`` column.
        query: A single clause, e.g. ``{"bool": {...}}`` or ``{"term": {...}}``.
            ``None`` or ``{}`` matches everything, as an absent ES ``query`` does.
        id_column: The document-id column, for the ``ids`` clause.
        now: Reference time for date math. Injected so tests are deterministic.
        context: Where in the query this clause sits, for error messages.

    Raises:
        UnsupportedQueryError: for any clause not in the table at the top of this
            module, and for a clause object carrying more than one clause name.
    """
    if not query:
        return true()
    if not isinstance(query, dict):
        raise UnsupportedQueryError(f"{context} of type {type(query).__name__}", context)

    names = [k for k in query if not k.startswith("_")]
    if len(names) != 1:
        # ES itself rejects two clause names in one object. Picking one would be
        # a guess, and the guess silently changes the result set.
        raise UnsupportedQueryError(
            f"{len(names)} clauses in one object ({sorted(names)})", context
        )
    name = names[0]
    body = query[name]

    if name == "bool":
        return _bool(column, body or {}, id_column, now=now)
    if name == "constant_score":  # noqa: SIM114 — distinct clause, distinct handling
        # Pure score wrapper: the filter inside is what selects rows.
        inner = (body or {}).get("filter")
        return build_predicate(
            column, inner, id_column=id_column, now=now, context="constant_score.filter"
        )
    handler = _LEAF_HANDLERS.get(name)
    if handler is None:
        raise UnsupportedQueryError(name, context)
    return _definite(handler(column, body or {}, id_column, now))


def _definite(predicate: ColumnElement) -> ColumnElement:
    """Force a leaf predicate to two-valued logic: NULL becomes FALSE.

    This is not defensive tidying, it is a correctness requirement, and a property
    test found it. ``{"range": {"count": {"gte": 7}}}`` compiles to
    ``cast(document->>'count' AS FLOAT) >= 7``. For a document with no ``count``
    the extraction is NULL, so the comparison is NULL — and SQL's ``NOT NULL`` is
    also NULL. The document therefore satisfied neither the query nor
    ``must_not: [query]``, so the two did not partition the index: a caller asking
    "everything without count >= 7" silently lost rows that have no ``count`` at
    all.

    Elasticsearch has no such state — a document either matches or does not — so
    coalescing to FALSE is what makes the translation faithful. Applied to every
    leaf rather than to the clauses that obviously need it, because ``ilike`` on a
    missing field is NULL too, and so is every comparison.
    """
    return func.coalesce(predicate, false(), type_=Boolean)


def _never() -> ColumnElement:
    """A predicate that matches nothing."""
    from sqlalchemy import false

    return false()


def _single_field(body: Any, clause: str) -> Tuple[str, Any]:
    """Unwrap the ``{field: value}`` shape shared by the single-field clauses."""
    if not isinstance(body, dict) or len(body) != 1:
        raise UnsupportedQueryError(
            f"{clause} with {0 if not isinstance(body, dict) else len(body)} fields",
            clause,
        )
    field, value = next(iter(body.items()))
    return field, value


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def build_order_by(
    column, sort: Any, *, id_column, tiebreak: bool = True
) -> List[ColumnElement]:
    """Compile an Elasticsearch ``sort`` into ORDER BY expressions.

    Accepts every shape the codebase uses: ``[{"field": {"order": "desc"}}]``,
    ``[{"field": "desc"}]``, ``["field"]``, and a bare ``"field"``.

    Ordering goes through the **jsonb value**, not its text form. jsonb has a
    total order that compares numbers numerically and strings lexicographically,
    so one expression sorts an ISO-8601 timestamp field and a numeric quantity
    field correctly. ``->>`` would compare everything as text and put ``"10"``
    before ``"9"``.

    ``_score`` is accepted and ignored: nothing here scores, so every document
    has the same relevance and the sort is a no-op. Ignoring it is safe in a way
    that ignoring a *filter* never is — it changes presentation order, not which
    rows come back.

    A stable tiebreak on the document id is appended so pagination cannot repeat
    or skip a row when the sort field ties, which is a real risk on
    ``created_at`` fields stamped at second resolution.
    """
    entries: List[Tuple[str, str]] = []
    if sort is None:
        pass
    elif isinstance(sort, str):
        entries.append((sort, "asc"))
    elif isinstance(sort, dict):
        for field, spec in sort.items():
            entries.append((field, _sort_order(spec)))
    elif isinstance(sort, (list, tuple)):
        for item in sort:
            if isinstance(item, str):
                entries.append((item, "asc"))
            elif isinstance(item, dict):
                for field, spec in item.items():
                    entries.append((field, _sort_order(spec)))
            else:
                raise UnsupportedQueryError(
                    f"sort entry of type {type(item).__name__}", "sort"
                )
    else:
        raise UnsupportedQueryError(f"sort of type {type(sort).__name__}", "sort")

    order_by: List[ColumnElement] = []
    for field, direction in entries:
        if field in ("_score", "_doc"):
            continue
        element = _json_value(column, field)
        # NULLS LAST on descending matches ES, which omits documents missing the
        # sort field from the head of the results rather than leading with them.
        order_by.append(
            element.desc().nullslast() if direction == "desc" else element.asc().nullslast()
        )
    if tiebreak:
        order_by.append(id_column.asc())
    return order_by


def _sort_order(spec: Any) -> str:
    if isinstance(spec, str):
        order = spec
    elif isinstance(spec, dict):
        unsupported = set(spec) - {"order", "missing", "unmapped_type", "mode", "nested"}
        if unsupported:
            raise UnsupportedQueryError(f"sort options {sorted(unsupported)}", "sort")
        order = spec.get("order", "asc")
    else:
        raise UnsupportedQueryError(f"sort spec of type {type(spec).__name__}", "sort")
    order = str(order).lower()
    if order not in ("asc", "desc"):
        raise UnsupportedQueryError(f"sort order {order!r}", "sort")
    return order


# ---------------------------------------------------------------------------
# _source filtering
# ---------------------------------------------------------------------------


def resolve_source_filter(source: Any) -> Optional[Tuple[Sequence[str], Sequence[str]]]:
    """Normalise a ``_source`` spec into ``(includes, excludes)``, or ``None``.

    ``None`` means "return the whole document". ``_source: false`` is reported as
    an empty include list, which yields ``{}`` per hit — matching ES.
    """
    if source is None or source is True:
        return None
    if source is False:
        return ((), ())
    if isinstance(source, str):
        return ((source,), ())
    if isinstance(source, (list, tuple)):
        return (tuple(source), ())
    if isinstance(source, dict):
        unsupported = set(source) - {"includes", "excludes", "include", "exclude"}
        if unsupported:
            raise UnsupportedQueryError(f"_source options {sorted(unsupported)}", "_source")
        includes = source.get("includes") or source.get("include") or ()
        excludes = source.get("excludes") or source.get("exclude") or ()
        if isinstance(includes, str):
            includes = (includes,)
        if isinstance(excludes, str):
            excludes = (excludes,)
        return (tuple(includes), tuple(excludes))
    raise UnsupportedQueryError(f"_source of type {type(source).__name__}", "_source")


def apply_source_filter(
    document: Dict[str, Any],
    spec: Optional[Tuple[Sequence[str], Sequence[str]]],
) -> Dict[str, Any]:
    """Project a document through an ``(includes, excludes)`` spec.

    Supports the dotted and trailing-``*`` forms the codebase uses. Includes are
    applied first, then excludes, which is ES's order.
    """
    if spec is None:
        return document
    includes, excludes = spec
    if includes:
        result: Dict[str, Any] = {}
        for pattern in includes:
            _copy_matching(document, pattern, result)
    elif excludes:
        # No includes but some excludes: start from the whole document. This is
        # the common ``_source: {"excludes": [...]}`` shape.
        result = _deep_copy(document)
    else:
        # Neither: this is ``_source: false``, which yields an empty hit body.
        return {}
    for pattern in excludes:
        _delete_matching(result, pattern)
    return result


def _deep_copy(value: Any) -> Any:
    """Copy nested dicts so an exclude cannot mutate the stored document.

    ``dict(document)`` is a shallow copy, so deleting ``a.b`` would delete it from
    the row's own ``document`` dict and, on a session-attached ORM row, persist
    that deletion. Cheap to get wrong, expensive to notice.
    """
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _copy_matching(source: Dict[str, Any], pattern: str, into: Dict[str, Any]) -> None:
    head, _, rest = pattern.partition(".")
    if head.endswith("*"):
        prefix = head[:-1]
        for key, value in source.items():
            if key.startswith(prefix):
                into[key] = value
        return
    if head not in source:
        return
    if not rest:
        into[head] = source[head]
        return
    child = source[head]
    if isinstance(child, dict):
        nested = into.setdefault(head, {})
        if isinstance(nested, dict):
            _copy_matching(child, rest, nested)


def _delete_matching(target: Dict[str, Any], pattern: str) -> None:
    head, _, rest = pattern.partition(".")
    if head.endswith("*"):
        prefix = head[:-1]
        for key in [k for k in target if k.startswith(prefix)]:
            del target[key]
        return
    if head not in target:
        return
    if not rest:
        del target[head]
        return
    child = target[head]
    if isinstance(child, dict):
        _delete_matching(child, rest)


# ---------------------------------------------------------------------------
# Field discovery
# ---------------------------------------------------------------------------
#
# Used by :mod:`persistence.document_field_policy` to refuse a query that filters
# on a field the Elasticsearch mapping declares unsearchable (``binary``,
# ``index: false``, ``enabled: false``). Collected by walking the query rather
# than by inspecting the compiled SQL, because the field names are what the policy
# is expressed in and the SQL has already lost them.


def collect_query_fields(query: Optional[Dict[str, Any]]) -> List[str]:
    """Every document field a query clause filters on, at any nesting depth."""
    found: List[str] = []
    _collect(query, found)
    return found


def _collect(query: Any, found: List[str]) -> None:
    if not isinstance(query, dict):
        return
    for name, body in query.items():
        if name == "bool":
            for key in ("must", "filter", "should", "must_not"):
                clauses = (body or {}).get(key)
                if isinstance(clauses, dict):
                    clauses = [clauses]
                for clause in clauses or ():
                    _collect(clause, found)
        elif name == "constant_score":
            _collect((body or {}).get("filter"), found)
        elif name in ("exists",):
            field = (body or {}).get("field")
            if field:
                found.append(field)
        elif name == "multi_match":
            for raw in (body or {}).get("fields") or ():
                found.append(str(raw).split("^", 1)[0])
        elif name in ("term", "terms", "range", "match", "match_phrase", "wildcard", "prefix"):
            if isinstance(body, dict):
                found.extend(body.keys())
        elif name in ("match_all", "match_none", "ids"):
            continue


def collect_sort_fields(sort: Any) -> List[str]:
    """Every document field a ``sort`` orders by, excluding ``_score`` / ``_doc``."""
    found: List[str] = []
    entries = sort if isinstance(sort, (list, tuple)) else [sort] if sort else []
    for item in entries:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            found.extend(item.keys())
    return [f for f in found if f not in ("_score", "_doc")]
