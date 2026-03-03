"""
Property-based tests for Tenant Isolation.

# Feature: ops-intelligence-layer, Property 6: Tenant Isolation

**Validates: Requirements 9.1-9.8**

For any tenant_id and any ES query, inject_tenant_filter always produces
a query containing a tenant_id filter term. For any two different tenant_ids,
the produced queries have different tenant_id filter values. The tenant_id
filter is always in the "filter" clause (not "must" or "should").
"""

import sys
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis.strategies import (
    dictionaries,
    fixed_dictionaries,
    from_regex,
    just,
    none,
    one_of,
    recursive,
    text,
)

# ---------------------------------------------------------------------------
# Mock the elasticsearch_service module before importing ops modules
# ---------------------------------------------------------------------------
sys.modules.setdefault("services.elasticsearch_service", MagicMock())

from ops.middleware.tenant_guard import inject_tenant_filter  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Tenant IDs: non-empty alphanumeric strings (realistic tenant identifiers)
_tenant_ids = from_regex(r"[a-zA-Z0-9_\-]{1,64}", fullmatch=True)

# Simple ES leaf queries that inject_tenant_filter should wrap
_leaf_queries = one_of(
    just({"query": {"match_all": {}}}),
    just({"query": {"term": {"status": "delivered"}}}),
    just({"query": {"range": {"created_at": {"gte": "2024-01-01"}}}}),
    just({"query": {"match": {"origin": "warehouse"}}}),
    just({"query": {"bool": {"must": [{"term": {"status": "in_transit"}}]}}}),
    just({}),  # empty query — inject_tenant_filter should handle gracefully
    just({"query": {"terms": {"status": ["delivered", "failed"]}}}),
    just({"query": {"exists": {"field": "rider_id"}}}),
    just({"query": {"wildcard": {"shipment_id": {"value": "SHP-*"}}}}),
    just({"query": {"bool": {"should": [{"term": {"status": "failed"}}], "minimum_should_match": 1}}}),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_bool_filter(result: dict) -> list:
    """Extract the 'filter' clause from the result's bool query."""
    return result.get("query", {}).get("bool", {}).get("filter", [])


def _extract_bool_must(result: dict) -> list:
    """Extract the 'must' clause from the result's bool query."""
    return result.get("query", {}).get("bool", {}).get("must", [])


def _extract_bool_should(result: dict) -> list:
    """Extract the 'should' clause from the result's bool query."""
    return result.get("query", {}).get("bool", {}).get("should", [])


def _find_tenant_term(clauses: list, tenant_id: str) -> bool:
    """Check if any clause is a term filter for the given tenant_id."""
    for clause in clauses:
        if isinstance(clause, dict) and clause.get("term", {}).get("tenant_id") == tenant_id:
            return True
    return False


# ---------------------------------------------------------------------------
# Property 1 – inject_tenant_filter always includes tenant_id in filter
# ---------------------------------------------------------------------------
class TestTenantFilterAlwaysPresent:
    """**Validates: Requirements 9.1-9.8**"""

    @given(query=_leaf_queries, tenant_id=_tenant_ids)
    @settings(max_examples=200)
    def test_tenant_filter_always_injected(self, query: dict, tenant_id: str):
        """
        For any tenant_id and any ES query, inject_tenant_filter always
        produces a query containing a tenant_id term filter.
        """
        result = inject_tenant_filter(query, tenant_id)

        # The result must have a bool query with a filter clause
        assert "query" in result, "Result must contain a 'query' key"
        assert "bool" in result["query"], "Result query must be a 'bool' query"

        filter_clauses = _extract_bool_filter(result)
        assert len(filter_clauses) > 0, "Bool query must have a 'filter' clause"
        assert _find_tenant_term(filter_clauses, tenant_id), (
            f"Filter clause must contain a term filter for tenant_id={tenant_id}"
        )


# ---------------------------------------------------------------------------
# Property 2 – different tenant_ids produce different filter values
# ---------------------------------------------------------------------------
class TestTenantFilterDifferentiation:
    """**Validates: Requirements 9.1-9.8**"""

    @given(query=_leaf_queries, tenant_a=_tenant_ids, tenant_b=_tenant_ids)
    @settings(max_examples=200)
    def test_different_tenants_produce_different_filters(
        self, query: dict, tenant_a: str, tenant_b: str
    ):
        """
        For any two different tenant_ids, inject_tenant_filter produces
        queries with different tenant_id filter values.
        """
        assume(tenant_a != tenant_b)

        result_a = inject_tenant_filter(query, tenant_a)
        result_b = inject_tenant_filter(query, tenant_b)

        filter_a = _extract_bool_filter(result_a)
        filter_b = _extract_bool_filter(result_b)

        # Both must have tenant filters
        assert _find_tenant_term(filter_a, tenant_a)
        assert _find_tenant_term(filter_b, tenant_b)

        # Tenant A's filter must NOT match tenant B and vice versa
        assert not _find_tenant_term(filter_a, tenant_b), (
            f"Tenant A's query must not contain tenant_id={tenant_b}"
        )
        assert not _find_tenant_term(filter_b, tenant_a), (
            f"Tenant B's query must not contain tenant_id={tenant_a}"
        )


# ---------------------------------------------------------------------------
# Property 3 – tenant_id filter is in "filter" clause, not "must" or "should"
# ---------------------------------------------------------------------------
class TestTenantFilterPlacement:
    """**Validates: Requirements 9.1-9.8**"""

    @given(query=_leaf_queries, tenant_id=_tenant_ids)
    @settings(max_examples=200)
    def test_tenant_filter_in_filter_clause_not_must_or_should(
        self, query: dict, tenant_id: str
    ):
        """
        The tenant_id filter is always in the "filter" clause (not "must"
        or "should"), ensuring it is not scored.
        """
        result = inject_tenant_filter(query, tenant_id)

        bool_query = result.get("query", {}).get("bool", {})

        # tenant_id MUST be in the filter clause
        filter_clauses = bool_query.get("filter", [])
        assert _find_tenant_term(filter_clauses, tenant_id), (
            "tenant_id term must be in the 'filter' clause"
        )

        # tenant_id MUST NOT be in the must clause
        must_clauses = bool_query.get("must", [])
        assert not _find_tenant_term(must_clauses, tenant_id), (
            "tenant_id term must NOT be in the 'must' clause (would affect scoring)"
        )

        # tenant_id MUST NOT be in the should clause
        should_clauses = bool_query.get("should", [])
        assert not _find_tenant_term(should_clauses, tenant_id), (
            "tenant_id term must NOT be in the 'should' clause (would affect scoring)"
        )
