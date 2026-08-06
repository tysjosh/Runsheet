"""Keyset cursors for the nine list endpoints that got ``search_after`` wrong.

Every one of them passed an id where ``search_after`` expects the trailing hit's
sort values. The sort is ``[{created_at: desc}, {id: asc}]``, so an id went in as a
date boundary. Against the live cluster, page 2 is an HTTP 400::

    failed to parse date field [ACC-008] with format
    [strict_date_optional_time||epoch_millis]

The cursor stays an id, because these endpoints have a second implementation —
``persistence.read_repositories`` serves them from the relational tables when
``COMMERCE_READ_FROM_POSTGRES`` is on — and that one already does keyset pagination
correctly with an id cursor. Two implementations of one endpoint handing out
different cursor formats would break every in-flight cursor on either flag flip.
So the boundary is resolved server-side from the cursor row, which is what the
relational path does in SQL.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from services.keyset_pagination import (
    InvalidCursorError,
    next_cursor_from_hits,
    search_after_for_cursor,
)

SORT = [{"created_at": {"order": "desc"}}, {"account_id": {"order": "asc"}}]


class _Store:
    """Answers ``get_document`` from a dict, and records what was asked for."""

    def __init__(self, documents: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.documents = documents or {}
        self.asked: list = []

    async def get_document(self, index: str, doc_id: str):
        self.asked.append((index, doc_id))
        return self.documents.get(doc_id)


class TestSortFieldsAreRead:
    async def test_it_resolves_the_cursor_row_to_its_sort_values(self):
        """The whole fix: an id in, the boundary the sort actually needs out.

        The values are the ones the live cluster returned for this document —
        ``sort=[1762448588875, 'ACC-008']`` in epoch millis on Elasticsearch, the
        stored ISO string here — and passing ``ACC-008`` for both is what produced
        the 400.
        """
        store = _Store(
            {"ACC-008": {"created_at": "2026-01-04T00:00:00+00:00", "account_id": "ACC-008"}}
        )

        values = await search_after_for_cursor(
            store, "accounts_current", "ACC-008", SORT
        )

        assert values == ["2026-01-04T00:00:00+00:00", "ACC-008"]

    async def test_the_values_are_in_sort_order_not_document_order(self):
        """A boundary in the wrong order compares a date against an id, which is
        the original bug wearing a different hat."""
        store = _Store({"A": {"account_id": "A", "created_at": "2026-01-01"}})

        values = await search_after_for_cursor(store, "accounts_current", "A", SORT)

        assert values == ["2026-01-01", "A"]

    async def test_it_reads_the_index_it_was_given(self):
        store = _Store({"A": {"created_at": "x", "account_id": "A"}})

        await search_after_for_cursor(store, "accounts_current", "A", SORT)

        assert store.asked == [("accounts_current", "A")]

    @pytest.mark.parametrize(
        "sort, expected",
        [
            (["created_at"], ["2026-01-01"]),
            ([{"created_at": "desc"}], ["2026-01-01"]),
            ([{"created_at": {"order": "desc"}}], ["2026-01-01"]),
            ([{"_score": {"order": "desc"}}, {"account_id": "asc"}], ["A"]),
        ],
    )
    async def test_every_sort_shape_the_call_sites_use(self, sort, expected):
        """``_score`` names no field, so it contributes no boundary value —
        including it would offset every later value by one."""
        store = _Store({"A": {"created_at": "2026-01-01", "account_id": "A"}})

        assert await search_after_for_cursor(store, "i", "A", sort) == expected


class TestBadCursorsAreRefused:
    async def test_an_unknown_cursor_is_a_400(self):
        """Not silently ignored.

        The relational path drops an unresolvable cursor and returns page 1 again,
        which makes ``while next_cursor:`` loop forever — page 1 comes back with
        the same cursor attached. A 400 tells the client to restart.
        """
        store = _Store()

        with pytest.raises(InvalidCursorError) as excinfo:
            await search_after_for_cursor(store, "accounts_current", "ACC-404", SORT)

        assert excinfo.value.status_code == 400
        assert "ACC-404" in str(excinfo.value)

    async def test_a_cursor_row_missing_a_sort_field_is_a_400(self):
        """Guessing a boundary from a partial row silently returns the wrong page."""
        store = _Store({"A": {"account_id": "A"}})  # no created_at

        with pytest.raises(InvalidCursorError) as excinfo:
            await search_after_for_cursor(store, "accounts_current", "A", SORT)

        assert "created_at" in str(excinfo.value)

    async def test_a_query_with_no_sort_is_a_400(self):
        """``search_after`` is a position in an ordering; without one there is
        nothing to be after."""
        store = _Store({"A": {"account_id": "A"}})

        with pytest.raises(InvalidCursorError):
            await search_after_for_cursor(store, "accounts_current", "A", None)

    async def test_it_is_a_value_error_too(self):
        """So a caller that wants to handle it locally can catch the obvious type."""
        store = _Store()

        with pytest.raises(ValueError):
            await search_after_for_cursor(store, "i", "nope", SORT)


class TestNextCursor:
    def test_a_full_page_yields_the_trailing_id(self):
        hits = [
            {"_source": {"account_id": "A"}},
            {"_source": {"account_id": "B"}},
        ]
        assert next_cursor_from_hits(hits, 2, id_field="account_id") == "B"

    def test_a_short_page_ends_pagination(self):
        """Fewer hits than requested is the end of the set; a cursor there promises
        a page that comes back empty."""
        hits = [{"_source": {"account_id": "A"}}]
        assert next_cursor_from_hits(hits, 2, id_field="account_id") is None

    def test_an_empty_page_ends_pagination(self):
        assert next_cursor_from_hits([], 2, id_field="account_id") is None

    def test_a_hit_without_the_id_field_ends_pagination(self):
        """Rather than emitting ``"None"`` as a cursor string, which resolves to
        nothing and 400s on the next request."""
        hits = [{"_source": {}}, {"_source": {}}]
        assert next_cursor_from_hits(hits, 2, id_field="account_id") is None

    def test_the_cursor_is_a_string(self):
        """It travels in a query parameter; an int cursor would come back as text
        and fail to match the stored id."""
        hits = [{"_source": {"id": 1}}, {"_source": {"id": 2}}]
        assert next_cursor_from_hits(hits, 2, id_field="id") == "2"

    def test_it_matches_the_relational_paths_cursor_rule(self):
        """``persistence.read_repositories._page_result`` emits the trailing id on a
        full page and ``None`` otherwise. The two must agree or a flag flip
        invalidates every cursor in flight."""
        from persistence.read_repositories import _page_result

        items = [{"account_id": "A"}, {"account_id": "B"}]
        relational = _page_result(items, 2, "account_id")["next_cursor"]
        document_store = next_cursor_from_hits(
            [{"_source": item} for item in items], 2, id_field="account_id"
        )

        assert relational == document_store == "B"
