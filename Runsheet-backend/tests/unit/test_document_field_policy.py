"""Fields Elasticsearch cannot search must not become searchable in Postgres.

An Elasticsearch mapping can store a field without indexing it — ``binary``,
``index: false``, ``enabled: false``. In a jsonb column everything is queryable,
so moving the document plane to Postgres silently widens what a caller can filter
on. Two of the affected fields make that a security property:

* ``fuel_orders_current.pod_otp`` — the proof-of-delivery one-time code. Made
  filterable, an authenticated caller with access to any order search can confirm
  a guessed OTP one query at a time.
* ``driver_devices.push_token`` — an Expo push credential.

The rest are ``enabled: false`` payload blobs, where allowing a filter would also
make the two backends behave differently under load (a full jsonb scan in
Postgres, an error in Elasticsearch).

These tests pin the policy against the real declared mappings rather than against
a fixture, because the value of the guard is that it tracks the mappings. A
fixture would keep passing after someone made ``pod_otp`` searchable.
"""

from __future__ import annotations

import pytest

from persistence.document_field_policy import (
    UnsearchableFieldError,
    assert_searchable,
    reasons_for,
    unsearchable_fields,
)


class TestThePolicyReadsTheRealMappings:
    def test_the_credential_ciphertext_is_unsearchable(self):
        assert unsearchable_fields("tenant_credentials") >= {"ciphertext", "wrapped_dek"}

    def test_the_delivery_otp_is_unsearchable(self):
        """The one that matters most: ES declares ``index: false`` on it."""
        assert "pod_otp" in unsearchable_fields("fuel_orders_current")

    def test_the_push_token_is_unsearchable(self):
        assert "push_token" in unsearchable_fields("driver_devices")

    def test_disabled_object_subtrees_are_covered_by_their_parent(self):
        assert "event_payload" in unsearchable_fields("job_events")

    def test_every_registry_actually_imports(self):
        """A registry that fails to import deletes part of the policy, silently.

        ``_iter_mappings`` skips a registry it cannot import. That is the right
        behaviour for a module that is genuinely optional and the wrong behaviour to
        rely on: when ``ops.services.ops_es_mappings`` was first generated with
        ``json.dumps`` it contained a lowercase ``false``, so it parsed and then
        raised ``NameError`` on import — and the ops indices quietly reported no
        unsearchable fields at all.

        So the registry list is checked directly rather than through the policy,
        which is the only way to tell "declares nothing" from "could not be read".
        """
        from persistence.document_field_policy import _REGISTRIES

        broken = []
        for module_path, attribute in _REGISTRIES:
            try:
                module = __import__(module_path, fromlist=[attribute])
            except Exception as exc:  # noqa: BLE001
                broken.append(f"{module_path}: {type(exc).__name__}: {exc}")
                continue
            registry = getattr(module, attribute, None)
            if not isinstance(registry, dict):
                broken.append(
                    f"{module_path}.{attribute} is {type(registry).__name__}, not a dict"
                )
            elif not registry:
                broken.append(f"{module_path}.{attribute} is empty")

        assert not broken, (
            "these mapping registries cannot be read, so any unsearchable field "
            f"they declare is queryable: {broken}"
        )

    def test_the_ops_poison_queue_payload_is_unsearchable(self):
        """``original_payload`` is declared ``enabled: false``.

        It holds the raw payload of a failed ingestion — whatever an upstream system
        sent, which is exactly the kind of thing that turns out to contain a token.
        Elasticsearch cannot filter on it at all; jsonb can filter on anything, and
        the ops mappings were not in the registry until Phase 6 moved them out of
        ``OpsElasticsearchService``.
        """
        from persistence.document_field_policy import unsearchable_fields

        assert "original_payload" in unsearchable_fields("ops_poison_queue")

    def test_an_index_with_no_declared_mapping_has_no_restrictions(self):
        """A dynamically-mapped index indexed everything, by definition."""
        assert unsearchable_fields("no_such_index_anywhere") == frozenset()

    def test_ordinary_fields_are_not_restricted(self):
        blocked = unsearchable_fields("fuel_orders_current")
        assert "status" not in blocked
        assert "tenant_id" not in blocked

    def test_the_reason_is_reported_for_the_error_message(self):
        reasons = reasons_for("tenant_credentials")
        assert "binary" in reasons["ciphertext"]


class TestAssertSearchable:
    def test_an_allowed_field_passes(self):
        assert_searchable("fuel_orders_current", ["status", "tenant_id"])

    def test_a_blocked_field_raises(self):
        with pytest.raises(UnsearchableFieldError) as exc:
            assert_searchable("fuel_orders_current", ["pod_otp"])
        assert "pod_otp" in str(exc.value)
        assert "index: false" in str(exc.value)

    def test_the_error_is_a_permission_error(self):
        """403, not 400: the two fields that matter most are credentials."""
        with pytest.raises(PermissionError):
            assert_searchable("tenant_credentials", ["ciphertext"])

    def test_a_keyword_subfield_cannot_slip_past(self):
        """``pod_otp.keyword`` addresses the same value the policy blocks."""
        with pytest.raises(UnsearchableFieldError):
            assert_searchable("fuel_orders_current", ["pod_otp.keyword"])

    def test_a_child_of_a_disabled_object_is_blocked_by_the_parent(self):
        """ES does not describe the subtree, so the policy cannot enumerate it."""
        with pytest.raises(UnsearchableFieldError):
            assert_searchable("job_events", ["event_payload.driver_id"])

    def test_a_field_merely_prefixed_by_a_blocked_name_is_allowed(self):
        """``pod_otp_verified`` is a different field from ``pod_otp``.

        A naive ``startswith`` would block it, which would break a legitimate
        read — the opposite failure, and just as wrong.
        """
        assert_searchable("fuel_orders_current", ["pod_otp_verified_at"])

    def test_an_unrestricted_index_short_circuits(self):
        assert_searchable("mvp_routes", ["anything", "at", "all"])


class TestTheStoreEnforcesIt:
    """The guard has to sit on the query path, not just exist as a helper."""

    def test_search_documents_collects_fields_from_query_sort_and_aggs(self):
        import inspect

        from persistence.document_store import PostgresDocumentStore

        source = inspect.getsource(PostgresDocumentStore.search_documents)
        assert "assert_searchable(" in source
        for collector in (
            "collect_query_fields",
            "collect_sort_fields",
            "collect_aggregation_fields",
        ):
            assert collector in source, (
                f"{collector} is not consulted, so a blocked field reached "
                "through that part of the body would not be caught"
            )


class TestFieldCollection:
    """The collectors must find fields wherever a query can hide them."""

    @pytest.mark.parametrize(
        "query,expected",
        [
            ({"term": {"pod_otp": "123"}}, {"pod_otp"}),
            ({"terms": {"pod_otp": ["1"]}}, {"pod_otp"}),
            ({"range": {"pod_otp": {"gte": "1"}}}, {"pod_otp"}),
            ({"exists": {"field": "pod_otp"}}, {"pod_otp"}),
            ({"prefix": {"pod_otp": "1"}}, {"pod_otp"}),
            ({"wildcard": {"pod_otp": "1*"}}, {"pod_otp"}),
            ({"match": {"pod_otp": "1"}}, {"pod_otp"}),
            (
                {"multi_match": {"query": "1", "fields": ["pod_otp^2", "status"]}},
                {"pod_otp", "status"},
            ),
            (
                {"bool": {"must_not": [{"term": {"pod_otp": "1"}}]}},
                {"pod_otp"},
            ),
            (
                {"bool": {"should": [{"bool": {"filter": [{"term": {"pod_otp": "1"}}]}}]}},
                {"pod_otp"},
            ),
            (
                {"constant_score": {"filter": {"term": {"pod_otp": "1"}}}},
                {"pod_otp"},
            ),
        ],
    )
    def test_query_fields_are_found_at_any_depth(self, query, expected):
        from persistence.document_query import collect_query_fields

        assert set(collect_query_fields(query)) >= expected

    def test_sort_fields_are_found_and_score_is_ignored(self):
        from persistence.document_query import collect_sort_fields

        assert set(collect_sort_fields([{"pod_otp": "desc"}, "_score"])) == {"pod_otp"}

    def test_aggregation_fields_are_found_including_nested_and_filters(self):
        from persistence.document_aggregations import collect_aggregation_fields

        aggs = {
            "outer": {
                "terms": {"field": "status"},
                "aggs": {
                    "inner": {"sum": {"field": "pod_otp"}},
                    "picked": {"filter": {"term": {"push_token": "x"}}},
                },
            }
        }
        found = set(collect_aggregation_fields(aggs))
        assert {"status", "pod_otp", "push_token"} <= found

    def test_top_hits_sort_fields_are_found(self):
        from persistence.document_aggregations import collect_aggregation_fields

        aggs = {"t": {"top_hits": {"sort": [{"pod_otp": "desc"}]}}}
        assert "pod_otp" in collect_aggregation_fields(aggs)
