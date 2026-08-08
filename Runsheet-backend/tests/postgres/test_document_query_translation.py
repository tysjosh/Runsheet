"""The SQL translation and the Python matcher must select the same documents.

A DSL translator cannot be trusted on the strength of one example per clause.
Clauses are individually easy; the bugs live in the interactions — ``must_not``
wrapped around a ``should`` wrapped around a ``range`` on a field half the
documents do not have, a ``terms`` list that happens to be empty, a numeric bound
compared against a value stored as a string. Those combinations are what a
property test reaches and a hand-written case does not.

So: generate documents, generate queries, and assert that
``persistence.document_query`` (compiled to SQL and executed by PostgreSQL) and
``persistence.document_matcher`` (walking Python values) agree on the exact set of
matching ids. The two are written independently — one emits SQL, the other walks
dicts — so agreement is evidence, not a tautology.

When they disagree the failure prints the query and the two id sets, because a
translator bug is only actionable if you can see which document moved.

These tests need real PostgreSQL and skip without it; see ``conftest.py`` for why
a SQLite shim would be worse than nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st

from persistence.document_matcher import matches
from persistence.document_query import build_predicate

pytestmark = pytest.mark.property

# A fixed reference time so date math is deterministic on both sides.
NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Document and query strategies
# ---------------------------------------------------------------------------

#: A small, fixed field vocabulary. Random field NAMES would make almost every
#: generated query miss for lack of overlap, so the interesting cases — present,
#: absent, wrong type, array-valued — would almost never be generated. Fixing the
#: vocabulary and varying presence per document is what makes the search space
#: dense in the region where translations actually break.
FIELDS = ("tenant_id", "status", "count", "grade", "enabled", "created_at", "note")

_STATUSES = ("active", "pending", "cancelled", "COMPLETED")
_GRADES = ("DIESEL_2", "GASOLINE_REG", "PROPANE")
_TENANTS = ("demo-tenant", "other-tenant")
_TIMESTAMPS = (
    "2026-08-01T00:00:00+00:00",
    "2026-08-04T09:30:00+00:00",
    "2026-08-06T11:59:59+00:00",
    "2026-08-09T23:00:00+00:00",
)

_scalar_for_field = {
    "tenant_id": st.sampled_from(_TENANTS),
    "status": st.sampled_from(_STATUSES),
    "count": st.one_of(
        st.integers(min_value=-5, max_value=50),
        st.floats(min_value=-5, max_value=50, allow_nan=False, allow_infinity=False),
        # A numeric field that sometimes holds a numeric string: real indices do
        # this, and it is where a "cast to float" translation and a text
        # comparison diverge.
        st.sampled_from(["0", "7", "42"]),
    ),
    "grade": st.one_of(
        st.sampled_from(_GRADES),
        # Array-valued, so ``term`` must match any element.
        st.lists(st.sampled_from(_GRADES), min_size=1, max_size=3, unique=True),
    ),
    "enabled": st.booleans(),
    "created_at": st.sampled_from(_TIMESTAMPS),
    "note": st.sampled_from(["Hoboken depot", "north YARD", "", "spare tank"]),
}


@st.composite
def documents(draw):
    """A document with a random subset of the vocabulary present."""
    present = draw(
        st.lists(st.sampled_from(FIELDS), min_size=0, max_size=len(FIELDS), unique=True)
    )
    doc = {}
    for field in present:
        doc[field] = draw(_scalar_for_field[field])
    # Two shapes that have a key but no VALUE, which Elasticsearch treats as
    # absent. Both are cases where a naive ``exists`` translation (key present, or
    # ``->>`` not null) disagrees with ES, and the empty array is the one the
    # first version of the translator got wrong — found by the whole-cluster
    # parity run against ``mvp_tank_forecasts.anomaly_flags``, not by this test,
    # which is why the shape is generated here now.
    if draw(st.booleans()):
        doc["nulled"] = None
    if draw(st.booleans()):
        doc["empty_list"] = []
    return doc


def _leaf_queries():
    field_value = st.sampled_from(
        [
            ("tenant_id", t) for t in _TENANTS
        ] + [
            ("status", s) for s in _STATUSES
        ] + [
            ("grade", g) for g in _GRADES
        ] + [
            ("count", n) for n in (0, 7, 42, 7.0)
        ] + [
            ("enabled", b) for b in (True, False)
        ]
    )
    return st.one_of(
        field_value.map(lambda fv: {"term": {fv[0]: fv[1]}}),
        st.lists(field_value, min_size=0, max_size=3).map(
            lambda pairs: {
                "terms": {
                    pairs[0][0] if pairs else "status": [p[1] for p in pairs]
                }
            }
        ),
        st.sampled_from(
            FIELDS + ("nulled", "empty_list", "never_present")
        ).map(lambda f: {"exists": {"field": f}}),
        st.sampled_from(
            [
                {"range": {"count": {"gte": 7}}},
                {"range": {"count": {"lt": 42}}},
                {"range": {"count": {"gte": 0, "lte": 42}}},
                {"range": {"created_at": {"gte": "2026-08-04T00:00:00+00:00"}}},
                {"range": {"created_at": {"lt": "now"}}},
                {"range": {"created_at": {"gte": "now-3d"}}},
            ]
        ),
        st.sampled_from(
            [
                {"match": {"note": "depot"}},
                {"match": {"note": "YARD"}},
                {"match": {"status": "act"}},
                {"multi_match": {"query": "north", "fields": ["note", "status"]}},
                {"wildcard": {"note": {"value": "*yard*", "case_insensitive": True}}},
                {"wildcard": {"status": "act*"}},
                {"prefix": {"status": "c"}},
                {"match_all": {}},
                {"match_none": {}},
            ]
        ),
    )


@st.composite
def queries(draw, depth=2):
    """A leaf clause, or a ``bool`` combining them."""
    if depth <= 0 or draw(st.booleans()):
        return draw(_leaf_queries())
    body = {}
    for key in ("must", "filter", "should", "must_not"):
        if draw(st.booleans()):
            body[key] = draw(
                st.lists(queries(depth=depth - 1), min_size=1, max_size=2)
            )
    if not body:
        body["must"] = [draw(_leaf_queries())]
    if "should" in body and draw(st.booleans()):
        body["minimum_should_match"] = draw(st.integers(min_value=0, max_value=2))
    return {"bool": body}


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


async def _load(store, index, corpus):
    """Replace the index contents with ``corpus`` in one transaction.

    One transaction rather than a call to ``index_document`` per document: this
    module tests the query translation, and 60 hypothesis examples × 8 documents
    × a transaction each turned a 20-second run into three minutes. The write path
    has its own tests.

    The delete matters as much as the insert. Hypothesis reuses the fixture across
    examples, so without it a 2-document example would still see the 8 documents
    the previous example wrote and the comparison would be against a corpus the
    test does not know about.
    """
    from sqlalchemy import delete

    from persistence.models import EsDocumentORM as M

    async with store._session_scope() as session:
        await session.execute(delete(M).where(M.index_name == index))
        for doc_id, doc in corpus.items():
            session.add(
                M(
                    index_name=index,
                    doc_id=doc_id,
                    tenant_id=doc.get("tenant_id"),
                    document=doc,
                )
            )


async def _ids_from_postgres(store, index, query):
    from sqlalchemy import select

    from persistence.models import EsDocumentORM as M

    predicate = build_predicate(
        M.document, query, id_column=M.doc_id, now=NOW
    )
    async with store._session_scope() as session:
        rows = (
            await session.execute(
                select(M.doc_id).where(M.index_name == index, predicate)
            )
        ).scalars().all()
    return set(rows)


def _ids_from_python(docs, query):
    return {
        doc_id for doc_id, doc in docs.items()
        if matches(doc, query, doc_id=doc_id, now=NOW)
    }


@hyp_settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(docs=st.lists(documents(), min_size=1, max_size=8), query=queries())
async def test_sql_and_python_select_the_same_documents(store, index_name, docs, query):
    corpus = {f"doc-{i}": doc for i, doc in enumerate(docs)}
    await _load(store, index_name, corpus)

    from_sql = await _ids_from_postgres(store, index_name, query)
    from_python = _ids_from_python(corpus, query)

    assert from_sql == from_python, (
        f"\nquery: {query}"
        f"\nonly SQL matched:    {sorted(from_sql - from_python)}"
        f"\nonly Python matched: {sorted(from_python - from_sql)}"
        f"\ndocuments: {corpus}"
    )


@hyp_settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(docs=st.lists(documents(), min_size=1, max_size=6), query=queries())
async def test_negating_a_query_partitions_the_index(store, index_name, docs, query):
    """``q`` and ``must_not: [q]`` must together cover every document, exactly once.

    An independent check on the same translation, and it catches a different class
    of bug: a predicate that is accidentally NULL for some documents (very easy in
    SQL when a jsonb path is missing) passes neither the query nor its negation,
    so the two sets no longer partition the index. Comparing against the Python
    matcher would not necessarily reveal that if both sides share the mistake.
    """
    corpus = {f"doc-{i}": doc for i, doc in enumerate(docs)}
    await _load(store, index_name, corpus)

    matched = await _ids_from_postgres(store, index_name, query)
    negated = await _ids_from_postgres(
        store, index_name, {"bool": {"must_not": [query]}}
    )

    assert not (matched & negated), (
        f"documents matched both the query and its negation: "
        f"{sorted(matched & negated)}\nquery: {query}"
    )
    assert matched | negated == set(corpus), (
        f"documents matched neither the query nor its negation: "
        f"{sorted(set(corpus) - matched - negated)}\nquery: {query}"
    )
