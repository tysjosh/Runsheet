"""The in-Python aggregation engine must reproduce Elasticsearch's output shapes.

Callers read these responses positionally — ``agg["buckets"][0]["doc_count"]``,
``agg["value"]``, ``bucket["key_as_string"]`` — so the shape is the contract, not
just the numbers. What is pinned here is the set of behaviours where a plausible
implementation is quietly wrong:

* ``min``/``max`` on a date field return epoch millis, because callers subtract
  them;
* an empty ``min`` is ``null``, not ``0``, because ``0`` reads as a measurement;
* ``date_histogram`` fills empty intervals, because the output is plotted;
* a ``terms`` aggregation counts each element of an array field;
* ``sum_other_doc_count`` reports what ``size`` truncated;
* and every pipeline aggregation raises rather than approximating.
"""

from __future__ import annotations

import pytest

from persistence.document_aggregations import (
    AggregationInputTooLarge,
    UnsupportedAggregationError,
    run_aggregations,
)


def docs(*bodies):
    return [(f"d{i}", body) for i, body in enumerate(bodies)]


class TestMetrics:
    def test_sum_and_avg_ignore_missing_fields(self):
        result = run_aggregations(
            docs({"n": 1}, {"n": 3}, {"other": 9}), {"t": {"sum": {"field": "n"}}}
        )
        assert result["t"]["value"] == 4.0

    def test_avg_of_nothing_is_null_not_zero(self):
        result = run_aggregations(docs({"other": 1}), {"a": {"avg": {"field": "n"}}})
        assert result["a"]["value"] is None

    def test_sum_of_nothing_is_zero(self):
        """ES reports 0 for an empty sum and null for an empty avg. Both are
        deliberate: a sum of no values is zero, a mean of no values is undefined."""
        result = run_aggregations(docs({"other": 1}), {"s": {"sum": {"field": "n"}}})
        assert result["s"]["value"] == 0.0

    def test_numeric_strings_are_counted(self):
        result = run_aggregations(docs({"n": "5"}, {"n": 5}), {"s": {"sum": {"field": "n"}}})
        assert result["s"]["value"] == 10.0

    def test_non_numeric_strings_are_skipped_not_fatal(self):
        result = run_aggregations(
            docs({"n": "abc"}, {"n": 2}), {"s": {"sum": {"field": "n"}}}
        )
        assert result["s"]["value"] == 2.0

    def test_booleans_are_not_summed_as_ones(self):
        """``True`` is not a quantity, and Python would otherwise add it as 1."""
        result = run_aggregations(
            docs({"n": True}, {"n": 2}), {"s": {"sum": {"field": "n"}}}
        )
        assert result["s"]["value"] == 2.0

    def test_value_count_counts_values_not_documents(self):
        result = run_aggregations(
            docs({"g": ["a", "b"]}, {"g": "c"}), {"c": {"value_count": {"field": "g"}}}
        )
        assert result["c"]["value"] == 3

    def test_cardinality_counts_distinct_values(self):
        result = run_aggregations(
            docs({"g": "a"}, {"g": "a"}, {"g": "b"}),
            {"c": {"cardinality": {"field": "g"}}},
        )
        assert result["c"]["value"] == 2

    def test_stats_reports_every_field(self):
        result = run_aggregations(
            docs({"n": 1}, {"n": 5}), {"s": {"stats": {"field": "n"}}}
        )
        assert result["s"] == {
            "count": 2, "min": 1.0, "max": 5.0, "avg": 3.0, "sum": 6.0,
        }

    def test_empty_stats_reports_zero_count_and_null_bounds(self):
        result = run_aggregations(docs({}), {"s": {"stats": {"field": "n"}}})
        assert result["s"]["count"] == 0
        assert result["s"]["min"] is None


class TestMinMaxOnDates:
    def test_a_timestamp_field_reports_epoch_millis_and_a_string(self):
        """Callers subtract two ``min`` values to get a duration.

        Returning the ISO string would make that arithmetic raise instead.
        """
        result = run_aggregations(
            docs({"ts": "2026-08-06T00:00:00+00:00"}, {"ts": "2026-08-07T00:00:00+00:00"}),
            {"first": {"min": {"field": "ts"}}, "last": {"max": {"field": "ts"}}},
        )
        assert isinstance(result["first"]["value"], float)
        one_day_ms = 24 * 60 * 60 * 1000
        assert result["last"]["value"] - result["first"]["value"] == one_day_ms
        assert result["first"]["value_as_string"].startswith("2026-08-06")

    def test_a_z_suffixed_timestamp_parses(self):
        result = run_aggregations(
            docs({"ts": "2026-08-06T00:00:00Z"}), {"m": {"min": {"field": "ts"}}}
        )
        assert isinstance(result["m"]["value"], float)

    def test_a_non_date_string_field_compares_lexically_with_no_value_as_string(self):
        result = run_aggregations(
            docs({"s": "b"}, {"s": "a"}), {"m": {"min": {"field": "s"}}}
        )
        assert result["m"]["value"] == "a"
        assert "value_as_string" not in result["m"]

    def test_empty_min_is_null(self):
        result = run_aggregations(docs({}), {"m": {"min": {"field": "n"}}})
        assert result["m"]["value"] is None


class TestTermsBuckets:
    def test_buckets_are_ordered_by_count_descending_by_default(self):
        result = run_aggregations(
            docs({"s": "a"}, {"s": "b"}, {"s": "b"}), {"t": {"terms": {"field": "s"}}}
        )
        assert [b["key"] for b in result["t"]["buckets"]] == ["b", "a"]

    def test_each_element_of_an_array_gets_its_own_bucket(self):
        result = run_aggregations(
            docs({"g": ["a", "b"]}), {"t": {"terms": {"field": "g"}}}
        )
        assert {b["key"] for b in result["t"]["buckets"]} == {"a", "b"}

    def test_size_truncates_and_sum_other_doc_count_reports_the_remainder(self):
        """The field that lets a caller tell a truncated aggregation from a whole one."""
        result = run_aggregations(
            docs({"s": "a"}, {"s": "b"}, {"s": "c"}),
            {"t": {"terms": {"field": "s", "size": 1}}},
        )
        assert len(result["t"]["buckets"]) == 1
        assert result["t"]["sum_other_doc_count"] == 2

    def test_order_by_key_ascending(self):
        result = run_aggregations(
            docs({"s": "b"}, {"s": "a"}),
            {"t": {"terms": {"field": "s", "order": {"_key": "asc"}}}},
        )
        assert [b["key"] for b in result["t"]["buckets"]] == ["a", "b"]

    def test_order_by_a_sub_aggregation(self):
        result = run_aggregations(
            docs({"s": "a", "n": 1}, {"s": "b", "n": 9}),
            {
                "t": {
                    "terms": {"field": "s", "order": {"total": "desc"}},
                    "aggs": {"total": {"sum": {"field": "n"}}},
                }
            },
        )
        assert [b["key"] for b in result["t"]["buckets"]] == ["b", "a"]

    def test_missing_places_documents_without_the_field_in_a_named_bucket(self):
        result = run_aggregations(
            docs({"s": "a"}, {}),
            {"t": {"terms": {"field": "s", "missing": "none"}}},
        )
        assert {b["key"] for b in result["t"]["buckets"]} == {"a", "none"}

    def test_min_doc_count_drops_thin_buckets(self):
        result = run_aggregations(
            docs({"s": "a"}, {"s": "b"}, {"s": "b"}),
            {"t": {"terms": {"field": "s", "min_doc_count": 2}}},
        )
        assert [b["key"] for b in result["t"]["buckets"]] == ["b"]

    def test_nested_sub_aggregations_see_only_their_bucket(self):
        result = run_aggregations(
            docs({"s": "a", "n": 1}, {"s": "b", "n": 100}),
            {
                "t": {
                    "terms": {"field": "s"},
                    "aggs": {"total": {"sum": {"field": "n"}}},
                }
            },
        )
        totals = {b["key"]: b["total"]["value"] for b in result["t"]["buckets"]}
        assert totals == {"a": 1.0, "b": 100.0}


class TestFilterAndFilters:
    def test_filter_narrows_and_reports_a_doc_count(self):
        result = run_aggregations(
            docs({"k": "x", "n": 1}, {"k": "y", "n": 2}),
            {"f": {"filter": {"term": {"k": "x"}}, "aggs": {"t": {"sum": {"field": "n"}}}}},
        )
        assert result["f"]["doc_count"] == 1
        assert result["f"]["t"]["value"] == 1.0

    def test_filters_returns_a_named_bucket_map(self):
        result = run_aggregations(
            docs({"k": "x"}, {"k": "y"}),
            {
                "f": {
                    "filters": {
                        "filters": {"ex": {"term": {"k": "x"}}, "why": {"term": {"k": "y"}}}
                    }
                }
            },
        )
        assert result["f"]["buckets"]["ex"]["doc_count"] == 1
        assert result["f"]["buckets"]["why"]["doc_count"] == 1

    def test_missing_bucket_selects_documents_without_the_field(self):
        result = run_aggregations(
            docs({"a": 1}, {}), {"m": {"missing": {"field": "a"}}}
        )
        assert result["m"]["doc_count"] == 1


class TestDateHistogram:
    def test_daily_buckets_are_floored_to_the_interval(self):
        result = run_aggregations(
            docs(
                {"ts": "2026-08-01T01:00:00+00:00"},
                {"ts": "2026-08-01T23:00:00+00:00"},
            ),
            {"h": {"date_histogram": {"field": "ts", "calendar_interval": "day"}}},
        )
        assert len(result["h"]["buckets"]) == 1
        assert result["h"]["buckets"][0]["doc_count"] == 2
        assert result["h"]["buckets"][0]["key_as_string"].startswith("2026-08-01T00:00")

    def test_empty_intervals_are_filled_by_default(self):
        """The output is plotted; a gap would be drawn as a join, not a zero."""
        result = run_aggregations(
            docs({"ts": "2026-08-01T00:00:00+00:00"}, {"ts": "2026-08-03T00:00:00+00:00"}),
            {"h": {"date_histogram": {"field": "ts", "calendar_interval": "day"}}},
        )
        assert [b["doc_count"] for b in result["h"]["buckets"]] == [1, 0, 1]

    def test_min_doc_count_one_suppresses_the_gaps(self):
        result = run_aggregations(
            docs({"ts": "2026-08-01T00:00:00+00:00"}, {"ts": "2026-08-03T00:00:00+00:00"}),
            {
                "h": {
                    "date_histogram": {
                        "field": "ts", "calendar_interval": "day", "min_doc_count": 1,
                    }
                }
            },
        )
        assert [b["doc_count"] for b in result["h"]["buckets"]] == [1, 1]

    def test_fixed_intervals_are_accepted(self):
        result = run_aggregations(
            docs({"ts": "2026-08-01T00:00:00+00:00"}, {"ts": "2026-08-01T00:20:00+00:00"}),
            {"h": {"date_histogram": {"field": "ts", "fixed_interval": "15m"}}},
        )
        assert [b["doc_count"] for b in result["h"]["buckets"]] == [1, 1]

    def test_an_empty_input_yields_no_buckets_rather_than_one_of_zero(self):
        result = run_aggregations(
            docs({}), {"h": {"date_histogram": {"field": "ts", "calendar_interval": "day"}}}
        )
        assert result["h"]["buckets"] == []

    def test_a_calendar_month_is_refused(self):
        """A 30-day approximation shifts every boundary after the first."""
        with pytest.raises(UnsupportedAggregationError):
            run_aggregations(
                docs({"ts": "2026-08-01T00:00:00+00:00"}),
                {"h": {"date_histogram": {"field": "ts", "calendar_interval": "month"}}},
            )

    def test_nested_aggregations_run_per_bucket(self):
        result = run_aggregations(
            docs(
                {"ts": "2026-08-01T00:00:00+00:00", "n": 1},
                {"ts": "2026-08-02T00:00:00+00:00", "n": 10},
            ),
            {
                "h": {
                    "date_histogram": {"field": "ts", "calendar_interval": "day"},
                    "aggs": {"t": {"sum": {"field": "n"}}},
                }
            },
        )
        assert [b["t"]["value"] for b in result["h"]["buckets"]] == [1.0, 10.0]


class TestRangeBuckets:
    def test_ranges_are_inclusive_lower_exclusive_upper(self):
        result = run_aggregations(
            docs({"n": 0}, {"n": 10}, {"n": 20}),
            {"r": {"range": {"field": "n", "ranges": [{"from": 0, "to": 10}]}}},
        )
        assert result["r"]["buckets"][0]["doc_count"] == 1

    def test_open_ended_ranges(self):
        result = run_aggregations(
            docs({"n": 5}, {"n": 500}),
            {"r": {"range": {"field": "n", "ranges": [{"from": 100}, {"to": 100}]}}},
        )
        counts = [b["doc_count"] for b in result["r"]["buckets"]]
        assert counts == [1, 1]

    def test_keyed_ranges_return_a_map(self):
        result = run_aggregations(
            docs({"n": 5}),
            {
                "r": {
                    "range": {
                        "field": "n",
                        "keyed": True,
                        "ranges": [{"key": "low", "to": 10}],
                    }
                }
            },
        )
        assert result["r"]["buckets"]["low"]["doc_count"] == 1


class TestTopHits:
    def test_top_hits_returns_sorted_bodies_with_ids(self):
        result = run_aggregations(
            docs({"n": 1}, {"n": 3}, {"n": 2}),
            {"t": {"top_hits": {"size": 2, "sort": [{"n": {"order": "desc"}}]}}},
        )
        hits = result["t"]["hits"]["hits"]
        assert [h["_source"]["n"] for h in hits] == [3, 2]
        assert hits[0]["_id"] == "d1"
        assert result["t"]["hits"]["total"]["value"] == 3

    def test_top_hits_honours_source_filtering(self):
        result = run_aggregations(
            docs({"keep": 1, "drop": 2}),
            {"t": {"top_hits": {"size": 1, "_source": ["keep"]}}},
        )
        assert result["t"]["hits"]["hits"][0]["_source"] == {"keep": 1}


class TestRefusals:
    @pytest.mark.parametrize(
        "agg_type",
        ["bucket_script", "avg_bucket", "stats_bucket", "derivative", "cumulative_sum"],
    )
    def test_pipeline_aggregations_raise(self, agg_type):
        """They need a painless interpreter. Refusing beats guessing."""
        with pytest.raises(UnsupportedAggregationError) as exc:
            run_aggregations(docs({"n": 1}), {"x": {agg_type: {"buckets_path": "a"}}})
        assert agg_type in str(exc.value)

    def test_a_script_valued_metric_raises(self):
        with pytest.raises(UnsupportedAggregationError):
            run_aggregations(
                docs({"n": 1}), {"x": {"sum": {"script": {"source": "doc['n'].value"}}}}
            )

    def test_an_unknown_aggregation_raises(self):
        with pytest.raises(UnsupportedAggregationError):
            run_aggregations(docs({"n": 1}), {"x": {"percentiles": {"field": "n"}}})

    def test_two_types_in_one_aggregation_raise(self):
        with pytest.raises(UnsupportedAggregationError):
            run_aggregations(
                docs({"n": 1}), {"x": {"sum": {"field": "n"}, "avg": {"field": "n"}}}
            )

    def test_an_unknown_terms_option_raises(self):
        """Silently ignoring ``collect_mode`` is harmless; ignoring an unknown
        option in general is not, and the engine cannot tell which is which."""
        with pytest.raises(UnsupportedAggregationError):
            run_aggregations(
                docs({"s": "a"}), {"t": {"terms": {"field": "s", "collect_mode": "x"}}}
            )

    def test_over_the_input_cap_it_refuses(self, monkeypatch):
        import persistence.document_aggregations as mod

        monkeypatch.setattr(mod, "MAX_AGGREGATION_ROWS", 1)
        with pytest.raises(AggregationInputTooLarge):
            run_aggregations(docs({"n": 1}, {"n": 2}), {"s": {"sum": {"field": "n"}}})


class TestEmptyInput:
    def test_no_aggregations_requested(self):
        assert run_aggregations(docs({"n": 1}), {}) == {}

    def test_aggregating_zero_documents(self):
        result = run_aggregations(
            [], {"t": {"terms": {"field": "s"}}, "s": {"sum": {"field": "n"}}}
        )
        assert result["t"]["buckets"] == []
        assert result["s"]["value"] == 0.0
