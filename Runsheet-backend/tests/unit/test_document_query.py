"""The parts of the query translator that are testable without a database.

The predicate compilation itself is verified against real PostgreSQL, by property
test, in ``tests/postgres/test_document_query_translation.py`` — that is the only
verification worth having for a DSL translation and it cannot run on SQLite,
because the whole thing rests on jsonb operators SQLite does not have.

What this module covers is everything either side of that: date math, ``_source``
projection, ``sort`` shape parsing, and the refusals. The refusals matter most.
The translator's central promise is that a clause it cannot handle raises rather
than being dropped, and a dropped clause returns wrong rows that a caller cannot
distinguish from right ones.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from persistence.document_query import (
    UnsupportedQueryError,
    apply_source_filter,
    build_order_by,
    build_predicate,
    parse_date_math,
    resolve_source_filter,
)

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _column():
    from persistence.models import EsDocumentORM

    return EsDocumentORM.document, EsDocumentORM.doc_id


class TestDateMath:
    def test_bare_now_resolves_to_the_reference_time(self):
        assert parse_date_math("now", now=NOW) == NOW.isoformat()

    @pytest.mark.parametrize(
        "expression,expected_iso",
        [
            ("now-7d", "2026-07-30T12:00:00+00:00"),
            ("now-6h", "2026-08-06T06:00:00+00:00"),
            ("now-5m", "2026-08-06T11:55:00+00:00"),
            ("now+1d", "2026-08-07T12:00:00+00:00"),
            ("now-1w", "2026-07-30T12:00:00+00:00"),
        ],
    )
    def test_offsets(self, expression, expected_iso):
        assert parse_date_math(expression, now=NOW) == expected_iso

    def test_non_now_values_pass_through_untouched(self):
        assert parse_date_math(42, now=NOW) == 42
        assert parse_date_math("2026-01-01", now=NOW) == "2026-01-01"
        assert parse_date_math(None, now=NOW) is None

    def test_a_value_merely_starting_with_now_is_not_mistaken_for_date_math(self):
        """``"nowhere"`` appears in the codebase as a plain string."""
        with pytest.raises(UnsupportedQueryError):
            parse_date_math("nowhere", now=NOW)

    def test_rounding_is_refused_rather_than_approximated(self):
        """``now/d`` and ``now`` select different rows; guessing ships a bad window."""
        with pytest.raises(UnsupportedQueryError) as exc:
            parse_date_math("now/d", now=NOW)
        assert "now/d" in str(exc.value)


class TestSourceResolution:
    def test_absent_or_true_means_the_whole_document(self):
        assert resolve_source_filter(None) is None
        assert resolve_source_filter(True) is None

    def test_false_means_an_empty_body(self):
        assert resolve_source_filter(False) == ((), ())

    def test_a_list_is_an_include_list(self):
        assert resolve_source_filter(["a", "b"]) == (("a", "b"), ())

    def test_a_bare_string_is_a_single_include(self):
        assert resolve_source_filter("a") == (("a",), ())

    def test_includes_and_excludes_object(self):
        assert resolve_source_filter({"includes": ["a"], "excludes": ["b"]}) == (
            ("a",), ("b",),
        )

    def test_the_singular_aliases_are_accepted(self):
        assert resolve_source_filter({"include": "a", "exclude": "b"}) == (("a",), ("b",))

    def test_an_unknown_option_raises(self):
        with pytest.raises(UnsupportedQueryError):
            resolve_source_filter({"includes": ["a"], "unknown": 1})


class TestSourceProjection:
    DOC = {
        "a": 1,
        "b": 2,
        "nested": {"keep": 1, "drop": 2},
        "prefix_one": 1,
        "prefix_two": 2,
    }

    def test_no_spec_returns_the_document_unchanged(self):
        assert apply_source_filter(self.DOC, None) is self.DOC

    def test_includes_keeps_only_named_fields(self):
        assert apply_source_filter(self.DOC, (("a",), ())) == {"a": 1}

    def test_excludes_removes_named_fields(self):
        result = apply_source_filter(self.DOC, ((), ("a", "b")))
        assert "a" not in result and "b" not in result
        assert result["nested"] == {"keep": 1, "drop": 2}

    def test_false_yields_an_empty_body(self):
        assert apply_source_filter(self.DOC, ((), ())) == {}

    def test_a_dotted_include_reaches_into_a_nested_object(self):
        assert apply_source_filter(self.DOC, (("nested.keep",), ())) == {
            "nested": {"keep": 1}
        }

    def test_a_dotted_exclude_removes_only_the_subfield(self):
        result = apply_source_filter(self.DOC, ((), ("nested.drop",)))
        assert result["nested"] == {"keep": 1}

    def test_a_trailing_star_matches_by_prefix(self):
        assert apply_source_filter(self.DOC, (("prefix_*",), ())) == {
            "prefix_one": 1, "prefix_two": 2,
        }

    def test_excluding_does_not_mutate_the_input(self):
        """A shallow copy here would delete the field from the ORM row's own dict
        and persist the deletion on the next flush."""
        original = {"outer": {"keep": 1, "drop": 2}}
        apply_source_filter(original, ((), ("outer.drop",)))
        assert original["outer"] == {"keep": 1, "drop": 2}

    def test_an_absent_include_is_skipped_not_an_error(self):
        assert apply_source_filter(self.DOC, (("missing",), ())) == {}


class TestSortParsing:
    def test_every_shape_the_codebase_uses_is_accepted(self):
        column, id_column = _column()
        for sort in (
            "created_at",
            ["created_at"],
            [{"created_at": "desc"}],
            [{"created_at": {"order": "desc"}}],
            {"created_at": "asc"},
        ):
            order_by = build_order_by(column, sort, id_column=id_column)
            # One expression for the field plus the id tiebreak.
            assert len(order_by) == 2, sort

    def test_a_stable_id_tiebreak_is_always_appended(self):
        """``created_at`` is often stamped at second resolution, so ties are
        common and an unstable sort repeats or skips rows across pages."""
        column, id_column = _column()
        order_by = build_order_by(column, None, id_column=id_column)
        assert len(order_by) == 1

    def test_the_tiebreak_can_be_suppressed(self):
        column, id_column = _column()
        assert build_order_by(column, None, id_column=id_column, tiebreak=False) == []

    def test_score_and_doc_are_ignored_rather_than_refused(self):
        """Nothing here ranks, so ``_score`` is a no-op — and ignoring a SORT key
        changes presentation order, not which rows come back. Ignoring a FILTER
        never gets the same latitude."""
        column, id_column = _column()
        order_by = build_order_by(column, [{"_score": "desc"}], id_column=id_column)
        assert len(order_by) == 1  # tiebreak only

    def test_multiple_keys_are_preserved_in_order(self):
        column, id_column = _column()
        order_by = build_order_by(
            column, [{"a": "asc"}, {"b": "desc"}], id_column=id_column
        )
        assert len(order_by) == 3

    def test_an_invalid_order_direction_raises(self):
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError):
            build_order_by(column, [{"a": "sideways"}], id_column=id_column)

    def test_an_unknown_sort_option_raises(self):
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError):
            build_order_by(
                column, [{"a": {"order": "asc", "script": {}}}], id_column=id_column
            )


class TestRefusals:
    """The translator's central promise: never silently drop a clause."""

    @pytest.mark.parametrize(
        "query,expected",
        [
            ({"geo_distance": {"distance": "1km"}}, "geo_distance"),
            ({"nested": {"path": "a"}}, "nested"),
            ({"query_string": {"query": "a AND b"}}, "query_string"),
            ({"script": {"script": "true"}}, "script"),
            ({"function_score": {}}, "function_score"),
            ({"more_like_this": {}}, "more_like_this"),
            ({"knn": {}}, "knn"),
        ],
    )
    def test_an_unsupported_clause_names_itself(self, query, expected):
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError) as exc:
            build_predicate(column, query, id_column=id_column)
        assert expected in str(exc.value)

    def test_the_error_says_where_to_go_next(self):
        """A refusal is only actionable if it says what the options are."""
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError) as exc:
            build_predicate(column, {"knn": {}}, id_column=id_column)
        message = str(exc.value)
        assert "document_query" in message
        assert "Elasticsearch" in message

    def test_two_clauses_in_one_object_raise_rather_than_picking_one(self):
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError):
            build_predicate(
                column, {"term": {"a": 1}, "exists": {"field": "b"}}, id_column=id_column
            )

    def test_a_refusal_inside_a_bool_propagates(self):
        """A nested unsupported clause must not be swallowed by the compound."""
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError):
            build_predicate(
                column,
                {"bool": {"must": [{"term": {"a": 1}}, {"geo_distance": {}}]}},
                id_column=id_column,
            )

    def test_an_unknown_bool_option_raises(self):
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError):
            build_predicate(
                column,
                {"bool": {"must": [], "adjust_pure_negative": True}},
                id_column=id_column,
            )

    def test_a_null_term_raises(self):
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError):
            build_predicate(column, {"term": {"a": None}}, id_column=id_column)

    def test_a_terms_clause_with_a_non_list_value_raises(self):
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError):
            build_predicate(column, {"terms": {"a": "not-a-list"}}, id_column=id_column)

    def test_an_unknown_range_operator_raises(self):
        column, id_column = _column()
        with pytest.raises(UnsupportedQueryError):
            build_predicate(
                column, {"range": {"a": {"between": 1}}}, id_column=id_column
            )

    def test_an_empty_query_compiles_to_a_true_predicate(self):
        """An absent ``query`` matches everything in ES, and must here too —
        the opposite default would make every unfiltered list come back empty."""
        column, id_column = _column()
        for query in (None, {}):
            predicate = build_predicate(column, query, id_column=id_column)
            assert str(predicate.compile(compile_kwargs={"literal_binds": True})) == "true"
