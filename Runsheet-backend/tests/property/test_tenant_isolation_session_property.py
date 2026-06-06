"""
Property-based test for SuperTokens-migration tenant isolation.

# Feature: supertokens-auth-migration, Property 5: Tenant isolation

**Validates: Requirements 5.4, 11.4**

Property 5: Tenant isolation — a verified session scoped to tenant A never
resolves or returns tenant B's records.

This is exercised two complementary ways, both against the production seams:

1. ``inject_tenant_filter`` (``ops.middleware.tenant_guard``) — given a data
   store partitioned by ``tenant_id`` and an Elasticsearch query, the query
   produced for a session scoped to tenant A, when applied to the store,
   resolves only tenant A's records and never any of tenant B's (Req 5.4). The
   filtered match is simulated faithfully: a tenant-``term`` filter selects
   exactly the documents whose ``tenant_id`` equals the scope.

2. A FastAPI ``TestClient`` app whose tenant-scoped endpoint depends on the real
   ``get_tenant_context`` seam, with the verified session installed via the
   Test_Auth_Path ``override_auth`` (test/dev only). For all generated A != B
   and record sets, a request issued under a session scoped to tenant A — even
   when it supplies ``?tenant_id=B`` — returns only tenant A's records and never
   tenant B's (Req 11.4, and the no-client-tenant guarantee).

The Test_Auth_Path is environment-gated, so the verifier/override is installed
through ``override_auth`` and fully released in teardown; the environment is
pinned to ``test`` only for the duration of each example.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

import auth.test_auth as test_auth
from auth.test_auth import override_auth
from config.settings import Environment
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Realistic tenant identifiers: non-empty, distinct alphanumeric slugs.
_tenant_ids = st.from_regex(r"[a-zA-Z0-9_\-]{1,32}", fullmatch=True)

# A record is an opaque payload tagged with the tenant it belongs to. We model a
# tenant-partitioned store as a list of such records.
_record_payloads = st.text(min_size=0, max_size=24)


def _partitioned_store(
    tenant_a: str, tenant_b: str, a_payloads: list[str], b_payloads: list[str]
) -> list[dict]:
    """Build a tenant-partitioned record store with rows for A and B."""
    store: list[dict] = []
    for i, payload in enumerate(a_payloads):
        store.append({"id": f"a-{i}", "tenant_id": tenant_a, "payload": payload})
    for i, payload in enumerate(b_payloads):
        store.append({"id": f"b-{i}", "tenant_id": tenant_b, "payload": payload})
    return store


def _apply_tenant_filter(store: list[dict], filtered_query: dict) -> list[dict]:
    """Simulate Elasticsearch applying the tenant ``term`` filter to the store.

    Faithfully models ES semantics: only the ``tenant_id`` ``term`` filter
    clauses in the ``bool.filter`` array restrict results; documents survive iff
    their ``tenant_id`` matches every such term filter.
    """
    filters = (
        filtered_query.get("query", {}).get("bool", {}).get("filter", [])
    )
    tenant_terms = [
        clause["term"]["tenant_id"]
        for clause in filters
        if isinstance(clause, dict)
        and "term" in clause
        and "tenant_id" in clause["term"]
    ]
    return [
        doc
        for doc in store
        if all(doc.get("tenant_id") == term for term in tenant_terms)
    ]


@contextmanager
def _force_test_environment() -> Iterator[None]:
    """Pin ``settings.environment`` to ``test`` for the Test_Auth_Path guard.

    The Test_Auth_Path refuses to operate outside test/development, so the
    environment is pinned only for the body of the block and released on exit.
    """
    fake_settings = SimpleNamespace(environment=Environment.TEST)
    with patch.object(test_auth, "get_settings", return_value=fake_settings):
        yield


# ---------------------------------------------------------------------------
# Property 5a — query-filter isolation (Req 5.4)
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 5: Tenant isolation
class TestTenantIsolationQueryFilter:
    """**Validates: Requirements 5.4**"""

    @given(
        tenant_a=_tenant_ids,
        tenant_b=_tenant_ids,
        a_payloads=st.lists(_record_payloads, max_size=8),
        b_payloads=st.lists(_record_payloads, max_size=8),
    )
    @settings(max_examples=100)
    def test_session_scope_resolves_only_its_tenant(
        self,
        tenant_a: str,
        tenant_b: str,
        a_payloads: list[str],
        b_payloads: list[str],
    ):
        """A query scoped to tenant A resolves only A's records, never B's."""
        # Distinct tenants are the meaningful case for isolation.
        if tenant_a == tenant_b:
            return

        store = _partitioned_store(tenant_a, tenant_b, a_payloads, b_payloads)

        # The verified session is scoped to tenant A; the scope is the sole
        # source of the data-access filter (Req 5.4).
        scoped_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_a)
        resolved = _apply_tenant_filter(store, scoped_query)

        resolved_tenants = {doc["tenant_id"] for doc in resolved}
        # Never resolves tenant B's records.
        assert tenant_b not in resolved_tenants, (
            f"Tenant B={tenant_b} records leaked into a tenant A={tenant_a} scope"
        )
        # Resolves exactly tenant A's records.
        assert resolved_tenants <= {tenant_a}
        assert len(resolved) == len(a_payloads)


# ---------------------------------------------------------------------------
# Property 5b — end-to-end isolation through the get_tenant_context seam
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 5: Tenant isolation
class TestTenantIsolationEndToEnd:
    """**Validates: Requirements 11.4**"""

    @given(
        tenant_a=_tenant_ids,
        tenant_b=_tenant_ids,
        a_payloads=st.lists(_record_payloads, max_size=8),
        b_payloads=st.lists(_record_payloads, max_size=8),
    )
    @settings(
        max_examples=100,
        # The FastAPI app/TestClient is rebuilt per example by design; suppress
        # the function-scoped-fixture style health check (no fixtures are used).
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_request_under_tenant_a_never_returns_tenant_b(
        self,
        tenant_a: str,
        tenant_b: str,
        a_payloads: list[str],
        b_payloads: list[str],
    ):
        """A request under a tenant-A session returns only A's records.

        Even when the request supplies ``?tenant_id=B``, the endpoint derives
        its scope from the verified ``get_tenant_context`` and never leaks
        tenant B's records (Req 11.4).
        """
        if tenant_a == tenant_b:
            return

        store = _partitioned_store(tenant_a, tenant_b, a_payloads, b_payloads)

        app = FastAPI()

        @app.get("/records")
        def list_records(
            tenant_id: str | None = None,
            tenant: TenantContext = Depends(get_tenant_context),
        ):
            # Scope comes ONLY from the verified context, never the query param.
            scoped_query = inject_tenant_filter(
                {"query": {"match_all": {}}}, tenant.tenant_id
            )
            resolved = _apply_tenant_filter(store, scoped_query)
            return {
                "tenant_id": tenant.tenant_id,
                "records": [doc["payload"] for doc in resolved],
                "tenants_seen": sorted({doc["tenant_id"] for doc in resolved}),
            }

        client = TestClient(app)

        with _force_test_environment():
            # Verified session scoped to tenant A installed via Test_Auth_Path;
            # released on block exit (teardown of the override + bypass).
            with override_auth(app, tenant_id=tenant_a):
                # Attacker supplies tenant B via query param — must be ignored.
                resp = client.get("/records", params={"tenant_id": tenant_b})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tenant_a
        # Tenant B's records never appear in a tenant A request.
        assert tenant_b not in body["tenants_seen"]
        assert set(body["tenants_seen"]) <= {tenant_a}
        assert body["records"] == list(a_payloads)
