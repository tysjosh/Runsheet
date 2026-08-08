"""Keyset pagination, and the body keys the store used to drop on the floor.

Two defects motivate this file, and they share a cause. The store's rule is that
an unsupported clause raises rather than being ignored — that rule is what caught
every silent-empty defect in this migration. It was enforced for clauses inside
``query`` and ``aggs`` and not at all for the top level of the search body, where
an unrecognised key was simply never read:

* ``search_after`` paginates seventeen commerce, compliance and integration reads.
  Dropped, every page-2 request returns page 1.
* ``runtime_mappings`` computes a latency in painless for the notification
  send-latency metric. Dropped, the ``stats`` aggregation runs over a field that
  does not exist and the endpoint reports zero seconds as though measured.

Neither logged anything, which is why they were found by grepping for body keys
rather than by a test.

The pagination semantics pinned here are the ones that are easy to get subtly
wrong: the comparison is lexicographic across all sort keys rather than on the
first, ``total`` stays the total for the query rather than the remaining tail, and
a length mismatch raises instead of paginating by a prefix.
"""

from __future__ import annotations

import pytest

from persistence.document_query import UnsupportedQueryError

TENANT = "demo-tenant"


async def _seed(store, index_name, rows):
    for doc_id, document in rows:
        await store.index_document(index_name, doc_id, dict(document))


def _ids(response):
    return [hit["_id"] for hit in response["hits"]["hits"]]


# ---------------------------------------------------------------------------
# The body-key guard
# ---------------------------------------------------------------------------


class TestUnknownBodyKeysRaise:
    async def test_runtime_mappings_raises_instead_of_being_ignored(self, store, index_name):
        """The exact shape that made a latency metric report zero.

        Ignoring it is worse than refusing it: the aggregation still runs, over a
        field that does not exist, and returns a number that looks measured.
        """
        with pytest.raises(UnsupportedQueryError) as excinfo:
            await store.search_documents(
                index_name,
                {
                    "query": {"match_all": {}},
                    "runtime_mappings": {"latency_ms": {"type": "long"}},
                    "aggs": {"stats": {"stats": {"field": "latency_ms"}}},
                },
            )
        assert "runtime_mappings" in str(excinfo.value)

    @pytest.mark.parametrize(
        "key, value",
        [
            ("post_filter", {"term": {"a": 1}}),
            ("collapse", {"field": "a"}),
            ("script_fields", {"a": {"script": "1"}}),
            ("min_score", 0.5),
            ("highlight", {"fields": {"a": {}}}),
        ],
    )
    async def test_any_unread_key_raises(self, store, index_name, key, value):
        """Not an allowlist of known-bad keys — anything unread fails.

        A denylist would have to be extended every time someone reaches for a
        feature, which is the same as not having one.
        """
        with pytest.raises(UnsupportedQueryError):
            await store.search_documents(
                index_name, {"query": {"match_all": {}}, key: value}
            )

    async def test_track_total_hits_is_accepted(self, store, index_name):
        """Nine call sites pass it, and Postgres already satisfies what it asks for.

        Listed with a reason rather than silently tolerated, which is the whole
        distinction this guard is drawing.
        """
        await _seed(store, index_name, [("a", {"tenant_id": TENANT})])

        response = await store.search_documents(
            index_name, {"query": {"match_all": {}}, "track_total_hits": True}
        )

        assert response["hits"]["total"] == {"value": 1, "relation": "eq"}

    async def test_the_keys_the_store_reads_are_all_accepted(self, store, index_name):
        """A guard that rejected a working query would be worse than none."""
        await _seed(store, index_name, [("a", {"tenant_id": TENANT, "n": 1})])

        response = await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "size": 1,
                "from": 0,
                "sort": [{"n": {"order": "asc"}}],
                "_source": ["n"],
                "aggs": {"total": {"sum": {"field": "n"}}},
            },
        )

        assert _ids(response) == ["a"]


# ---------------------------------------------------------------------------
# sort values on hits
# ---------------------------------------------------------------------------


class TestHitsCarryTheirSortValues:
    async def test_sort_values_are_returned_when_sorting(self, store, index_name):
        """The keyset callers read ``hits[-1]["sort"]`` to build the next cursor.

        Omitting it does not error — it reads as "there is no next page", so
        pagination stops after the first one.
        """
        await _seed(
            store,
            index_name,
            [("a", {"tenant_id": TENANT, "created_at": "2026-01-01", "id": "a"})],
        )

        response = await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "sort": [{"created_at": {"order": "desc"}}, {"id": {"order": "asc"}}],
            },
        )

        assert response["hits"]["hits"][0]["sort"] == ["2026-01-01", "a"]

    async def test_no_sort_key_when_not_sorting(self, store, index_name):
        """ES omits it entirely on an unsorted search."""
        await _seed(store, index_name, [("a", {"tenant_id": TENANT})])

        response = await store.search_documents(index_name, {"query": {"match_all": {}}})

        assert "sort" not in response["hits"]["hits"][0]

    async def test_a_missing_sort_field_is_null_not_an_error(self, store, index_name):
        await _seed(store, index_name, [("a", {"tenant_id": TENANT})])

        response = await store.search_documents(
            index_name, {"query": {"match_all": {}}, "sort": [{"nope": {"order": "asc"}}]}
        )

        assert response["hits"]["hits"][0]["sort"] == [None]


# ---------------------------------------------------------------------------
# search_after
# ---------------------------------------------------------------------------


SORT = [{"created_at": {"order": "desc"}}, {"account_id": {"order": "asc"}}]

ROWS = [
    ("ACC-1", {"tenant_id": TENANT, "created_at": "2026-01-05", "account_id": "ACC-1"}),
    ("ACC-2", {"tenant_id": TENANT, "created_at": "2026-01-04", "account_id": "ACC-2"}),
    ("ACC-3", {"tenant_id": TENANT, "created_at": "2026-01-03", "account_id": "ACC-3"}),
    ("ACC-4", {"tenant_id": TENANT, "created_at": "2026-01-02", "account_id": "ACC-4"}),
]


class TestSearchAfterPaginates:
    async def test_it_walks_the_whole_result_set_exactly_once(self, store, index_name):
        """The property that matters: every row once, in order, no repeats.

        Asserted by walking to exhaustion rather than by checking one page, because
        a keyset predicate that compares only the first sort key passes a
        single-page check and repeats rows at the boundaries.
        """
        await _seed(store, index_name, ROWS)

        seen = []
        cursor = None
        for _ in range(10):
            body = {"query": {"match_all": {}}, "size": 2, "sort": SORT}
            if cursor is not None:
                body["search_after"] = cursor
            response = await store.search_documents(index_name, body)
            hits = response["hits"]["hits"]
            if not hits:
                break
            seen.extend(hit["_id"] for hit in hits)
            cursor = hits[-1]["sort"]

        assert seen == ["ACC-1", "ACC-2", "ACC-3", "ACC-4"]

    async def test_the_second_page_is_not_the_first_page(self, store, index_name):
        """The specific symptom of the key being dropped."""
        await _seed(store, index_name, ROWS)

        first = await store.search_documents(
            index_name, {"query": {"match_all": {}}, "size": 2, "sort": SORT}
        )
        second = await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "size": 2,
                "sort": SORT,
                "search_after": first["hits"]["hits"][-1]["sort"],
            },
        )

        assert _ids(first) == ["ACC-1", "ACC-2"]
        assert _ids(second) == ["ACC-3", "ACC-4"]

    async def test_it_breaks_ties_on_the_later_sort_key(self, store, index_name):
        """Two rows sharing ``created_at`` is the case a first-key-only comparison
        gets wrong: it would either skip the tied sibling or return it twice."""
        await _seed(
            store,
            index_name,
            [
                ("B", {"tenant_id": TENANT, "created_at": "2026-01-01", "account_id": "B"}),
                ("A", {"tenant_id": TENANT, "created_at": "2026-01-01", "account_id": "A"}),
                ("C", {"tenant_id": TENANT, "created_at": "2026-01-01", "account_id": "C"}),
            ],
        )

        first = await store.search_documents(
            index_name, {"query": {"match_all": {}}, "size": 2, "sort": SORT}
        )
        second = await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "size": 2,
                "sort": SORT,
                "search_after": first["hits"]["hits"][-1]["sort"],
            },
        )

        assert _ids(first) == ["A", "B"]
        assert _ids(second) == ["C"]

    async def test_numbers_compare_numerically_not_as_text(self, store, index_name):
        """As text ``"10"`` precedes ``"9"``, which would drop rows from the tail."""
        await _seed(
            store,
            index_name,
            [(str(n), {"tenant_id": TENANT, "n": n}) for n in (9, 10, 11)],
        )
        sort = [{"n": {"order": "asc"}}]

        response = await store.search_documents(
            index_name,
            {"query": {"match_all": {}}, "size": 5, "sort": sort, "search_after": [9]},
        )

        assert _ids(response) == ["10", "11"]

    async def test_the_total_is_the_query_total_not_the_remaining_tail(
        self, store, index_name
    ):
        """A caller rendering "N results" must not watch N shrink as it pages."""
        await _seed(store, index_name, ROWS)

        response = await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "size": 2,
                "sort": SORT,
                "search_after": ["2026-01-04", "ACC-2"],
            },
        )

        assert response["hits"]["total"]["value"] == 4
        assert _ids(response) == ["ACC-3", "ACC-4"]

    async def test_it_still_respects_the_query_filter(self, store, index_name):
        """Pagination narrowing must compose with filtering, not replace it."""
        await _seed(
            store,
            index_name,
            ROWS + [("OTHER", {"tenant_id": "other", "created_at": "2026-01-01", "account_id": "OTHER"})],
        )

        response = await store.search_documents(
            index_name,
            {
                "query": {"term": {"tenant_id": TENANT}},
                "size": 10,
                "sort": SORT,
                "search_after": ["2026-01-05", "ACC-1"],
            },
        )

        assert _ids(response) == ["ACC-2", "ACC-3", "ACC-4"]


class TestSearchAfterRefusesWhatItCannotDo:
    async def test_a_length_mismatch_raises(self, store, index_name):
        """The shipped cursor bug in miniature.

        Seventeen call sites passed ``[cursor, cursor]`` for a two-key sort where
        the cursor was an id — the right length, wrong values, which Elasticsearch
        rejects with a 400 when the first key is a date. A wrong LENGTH is the
        variant that would otherwise paginate by a prefix of the sort and repeat
        rows silently.
        """
        with pytest.raises(UnsupportedQueryError) as excinfo:
            await store.search_documents(
                index_name,
                {"query": {"match_all": {}}, "sort": SORT, "search_after": ["only-one"]},
            )
        assert "search_after" in str(excinfo.value)

    async def test_without_a_sort_it_raises(self, store, index_name):
        """ES requires a sort for ``search_after``; without one there is no cursor
        to be after, and the store must not invent an ordering."""
        with pytest.raises(UnsupportedQueryError):
            await store.search_documents(
                index_name, {"query": {"match_all": {}}, "search_after": ["x"]}
            )

    async def test_a_non_list_cursor_raises(self, store, index_name):
        with pytest.raises(UnsupportedQueryError):
            await store.search_documents(
                index_name,
                {"query": {"match_all": {}}, "sort": SORT, "search_after": "ACC-1"},
            )
