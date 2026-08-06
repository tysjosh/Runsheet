"""The Python query matcher must agree with Elasticsearch's semantics.

This module is the oracle the SQL translator is property-tested against
(``tests/postgres/test_document_query_translation.py``), and the ``filter`` /
``filters`` aggregation primitive. If it is wrong in the same direction as the
translator, the property test passes and both are wrong — so the semantics it
claims are pinned here directly, against the behaviour Elasticsearch actually has.

Each test names the trap rather than the clause, because the clauses are easy and
the traps are what ship.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from persistence.document_matcher import extract_values, matches
from persistence.document_query import UnsupportedQueryError

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


class TestTermIsExactAndTypeSensitive:
    def test_a_string_does_not_match_a_number(self):
        assert not matches({"n": 1}, {"term": {"n": "1"}})
        assert not matches({"n": "1"}, {"term": {"n": 1}})

    def test_a_boolean_does_not_match_one(self):
        """Python says ``True == 1``; Elasticsearch and jsonb do not.

        Without the explicit bool check the two backends disagree on
        ``{"term": {"enabled": 1}}``, and the disagreement only shows up on
        documents that happen to store a boolean.
        """
        assert not matches({"enabled": True}, {"term": {"enabled": 1}})
        assert matches({"enabled": True}, {"term": {"enabled": True}})

    def test_int_and_float_of_the_same_value_match(self):
        """JSON has one number type; ``7`` and ``7.0`` are the same value."""
        assert matches({"n": 7}, {"term": {"n": 7.0}})
        assert matches({"n": 7.0}, {"term": {"n": 7}})

    def test_an_array_matches_on_any_element(self):
        """A field holding an array is a field with several values."""
        doc = {"grades": ["DIESEL_2", "GASOLINE_REG"]}
        assert matches(doc, {"term": {"grades": "DIESEL_2"}})
        assert matches(doc, {"term": {"grades": "GASOLINE_REG"}})
        assert not matches(doc, {"term": {"grades": "PROPANE"}})

    def test_a_missing_field_does_not_match_and_does_not_raise(self):
        assert not matches({}, {"term": {"status": "active"}})

    def test_a_null_term_is_refused(self):
        """ES rejects it. Treating it as "field is null" would match documents
        the ES query would have errored on."""
        with pytest.raises(UnsupportedQueryError):
            matches({"a": 1}, {"term": {"a": None}})


class TestTermsIsAnyOf:
    def test_empty_list_matches_nothing(self):
        """The dangerous default is the other way: matching everything."""
        assert not matches({"s": "a"}, {"terms": {"s": []}})

    def test_nulls_in_the_list_are_ignored(self):
        assert matches({"s": "a"}, {"terms": {"s": [None, "a"]}})


class TestExistsFollowsElasticsearch:
    def test_an_explicit_json_null_counts_as_absent(self):
        """ES treats ``null`` as no value. A key-presence check alone would not."""
        assert not matches({"a": None}, {"exists": {"field": "a"}})

    def test_an_empty_array_counts_as_absent(self):
        assert not matches({"a": []}, {"exists": {"field": "a"}})

    def test_a_falsey_value_still_exists(self):
        """0, False and "" are values. Truthiness is not existence."""
        for value in (0, False, "", 0.0):
            assert matches({"a": value}, {"exists": {"field": "a"}}), value


class TestRangeTypesByTheBound:
    def test_a_numeric_bound_compares_numerically(self):
        assert matches({"n": 10}, {"range": {"n": {"gte": 9}}})
        assert not matches({"n": 9}, {"range": {"n": {"gte": 10}}})

    def test_a_numeric_string_is_coerced_for_a_numeric_bound(self):
        """Real indices store numbers as strings; ES coerces on a numeric field."""
        assert matches({"n": "10"}, {"range": {"n": {"gte": 9}}})

    def test_a_string_bound_compares_lexically(self):
        """ISO-8601 sorts lexically the same way it sorts chronologically, which
        is what every timestamp range in the codebase relies on."""
        doc = {"ts": "2026-08-04T09:30:00+00:00"}
        assert matches(doc, {"range": {"ts": {"gte": "2026-08-01T00:00:00+00:00"}}})
        assert not matches(doc, {"range": {"ts": {"gte": "2026-08-05T00:00:00+00:00"}}})

    def test_a_non_numeric_value_against_a_numeric_bound_does_not_match(self):
        """It also must not raise: ES skips a document of the wrong type."""
        assert not matches({"n": "abc"}, {"range": {"n": {"gte": 1}}})

    def test_both_bounds_must_hold(self):
        assert matches({"n": 5}, {"range": {"n": {"gte": 1, "lte": 10}}})
        assert not matches({"n": 50}, {"range": {"n": {"gte": 1, "lte": 10}}})

    def test_date_math_resolves_against_the_injected_now(self):
        recent = {"ts": "2026-08-05T12:00:00+00:00"}
        old = {"ts": "2026-07-01T12:00:00+00:00"}
        query = {"range": {"ts": {"gte": "now-3d"}}}
        assert matches(recent, query, now=NOW)
        assert not matches(old, query, now=NOW)

    def test_a_multi_valued_field_matches_when_any_value_is_in_range(self):
        assert matches({"n": [1, 100]}, {"range": {"n": {"gte": 50}}})


class TestBoolCombination:
    def test_should_is_required_only_when_nothing_else_is(self):
        """The subtlest ES rule in the DSL.

        With no must/filter, at least one should must match. With a must present,
        should only affects scoring — so a document satisfying the must but no
        should still matches.
        """
        doc = {"a": 1}
        assert not matches(doc, {"bool": {"should": [{"term": {"a": 2}}]}})
        assert matches(
            doc,
            {"bool": {"must": [{"term": {"a": 1}}], "should": [{"term": {"a": 2}}]}},
        )

    def test_minimum_should_match_zero_makes_should_optional(self):
        assert matches(
            {"a": 1},
            {"bool": {"should": [{"term": {"a": 2}}], "minimum_should_match": 0}},
        )

    def test_minimum_should_match_two_needs_two(self):
        doc = {"a": 1, "b": 2}
        two = [{"term": {"a": 1}}, {"term": {"b": 2}}]
        one = [{"term": {"a": 1}}, {"term": {"b": 99}}]
        assert matches(doc, {"bool": {"should": two, "minimum_should_match": 2}})
        assert not matches(doc, {"bool": {"should": one, "minimum_should_match": 2}})

    def test_must_not_excludes(self):
        assert not matches({"a": 1}, {"bool": {"must_not": [{"term": {"a": 1}}]}})
        assert matches({"a": 2}, {"bool": {"must_not": [{"term": {"a": 1}}]}})

    def test_must_not_on_a_missing_field_includes_the_document(self):
        """The three-valued-logic case that broke the SQL side.

        A document with no ``a`` does not match ``term a=1``, so it MUST match the
        negation. Losing it is a silent under-count.
        """
        assert matches({}, {"bool": {"must_not": [{"term": {"a": 1}}]}})
        assert matches({}, {"bool": {"must_not": [{"range": {"a": {"gte": 1}}}]}})

    def test_an_empty_bool_matches_everything(self):
        assert matches({"a": 1}, {"bool": {}})

    def test_a_single_clause_object_is_accepted_where_a_list_is_expected(self):
        """ES accepts either; several call sites pass the bare object."""
        assert matches({"a": 1}, {"bool": {"must": {"term": {"a": 1}}}})


class TestTextClauses:
    def test_match_is_case_insensitive_substring(self):
        assert matches({"note": "Hoboken Depot"}, {"match": {"note": "depot"}})
        assert not matches({"note": "Hoboken Depot"}, {"match": {"note": "yard"}})

    def test_an_empty_match_query_matches_everything(self):
        assert matches({"note": "x"}, {"match": {"note": ""}})

    def test_multi_match_ors_across_fields(self):
        doc = {"a": "north", "b": "south"}
        assert matches(doc, {"multi_match": {"query": "south", "fields": ["a", "b"]}})
        assert not matches(doc, {"multi_match": {"query": "east", "fields": ["a", "b"]}})

    def test_multi_match_ignores_field_boosts(self):
        """``^3`` only affects ranking, and nothing here ranks."""
        assert matches(
            {"a": "north"}, {"multi_match": {"query": "north", "fields": ["a^3"]}}
        )

    def test_wildcard_star_and_question_mark(self):
        assert matches({"s": "abc"}, {"wildcard": {"s": "a*"}})
        assert matches({"s": "abc"}, {"wildcard": {"s": "a?c"}})
        assert not matches({"s": "abc"}, {"wildcard": {"s": "a?"}})

    def test_wildcard_is_case_sensitive_unless_asked(self):
        assert not matches({"s": "ABC"}, {"wildcard": {"s": "abc"}})
        assert matches(
            {"s": "ABC"}, {"wildcard": {"s": {"value": "abc", "case_insensitive": True}}}
        )

    def test_a_literal_percent_in_a_pattern_stays_literal(self):
        """LIKE metacharacters must be escaped or ``50%`` matches everything."""
        assert matches({"s": "50% full"}, {"match": {"s": "50%"}})
        assert not matches({"s": "half full"}, {"match": {"s": "50%"}})


class TestFieldPaths:
    def test_a_dotted_path_addresses_a_nested_object(self):
        doc = {"cargo": {"description": "diesel"}}
        assert matches(doc, {"term": {"cargo.description": "diesel"}})

    def test_a_keyword_subfield_resolves_to_the_underlying_value(self):
        """``.keyword`` is a multi-field of an analyzed field; the stored jsonb
        value is the unanalyzed one, so the suffix is dropped on both backends."""
        assert matches({"s": "a b"}, {"term": {"s.keyword": "a b"}})

    def test_extract_values_flattens_one_level(self):
        assert extract_values({"a": [1, 2]}, "a") == [1, 2]
        assert extract_values({"a": 1}, "a") == [1]
        assert extract_values({}, "a") == []
        assert extract_values({"a": None}, "a") == []


class TestUnsupportedClausesRaise:
    @pytest.mark.parametrize(
        "query",
        [
            {"geo_distance": {"distance": "1km"}},
            {"nested": {"path": "a", "query": {}}},
            {"query_string": {"query": "a AND b"}},
            {"script": {"script": "true"}},
            {"function_score": {}},
        ],
    )
    def test_unknown_clause(self, query):
        with pytest.raises(UnsupportedQueryError):
            matches({"a": 1}, query)

    def test_two_clauses_in_one_object(self):
        with pytest.raises(UnsupportedQueryError):
            matches({"a": 1}, {"term": {"a": 1}, "exists": {"field": "a"}})

    def test_an_unknown_bool_option(self):
        with pytest.raises(UnsupportedQueryError):
            matches({"a": 1}, {"bool": {"must": [], "adjust_pure_negative": True}})


class TestIds:
    def test_ids_matches_the_document_id(self):
        assert matches({}, {"ids": {"values": ["a"]}}, doc_id="a")
        assert not matches({}, {"ids": {"values": ["b"]}}, doc_id="a")

    def test_without_a_doc_id_ids_matches_nothing_rather_than_raising(self):
        assert not matches({}, {"ids": {"values": ["a"]}})


class TestMatchAllAndNone:
    def test_match_all(self):
        assert matches({}, {"match_all": {}})

    def test_match_none(self):
        assert not matches({"a": 1}, {"match_none": {}})

    def test_no_query_matches_everything(self):
        assert matches({"a": 1}, None)
        assert matches({"a": 1}, {})
