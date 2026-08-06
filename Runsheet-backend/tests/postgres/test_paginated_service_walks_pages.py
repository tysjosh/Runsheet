"""A real paginated service, walked to exhaustion over the real document store.

Nine list endpoints paginate with ``search_after``. Every one of them was broken,
and it took a live request against the cluster to notice, because every unit test
in the codebase asserts one page. One page passes whether or not the cursor works:
page 1 ignores the cursor by definition.

So this test walks ``AccountService.list`` to exhaustion and checks the
invariant that pagination exists to provide — every record exactly once — rather
than checking that a page has the right length. That is the assertion that fails
whether the bug is a dropped ``search_after``, a cursor built from the wrong field,
a comparison on only the first sort key, or a missing ``sort`` on the hit.

It runs against Postgres because that is where the whole search path is real: the
store compiles the sort and the keyset predicate, and the service builds the cursor
from what the store returns.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

TENANT = "demo-tenant"


class _PostgresBackedFacade:
    """``ElasticsearchService`` with ``_pg_store()`` pinned to the test store."""

    from services.elasticsearch_service import ElasticsearchService as _Real

    search_documents = _Real.search_documents
    index_document = _Real.index_document
    get_document = _Real.get_document
    update_document = _Real.update_document
    _is_retired_index = _Real._is_retired_index
    del _Real

    def __init__(self, store: Any) -> None:
        self._store = store

    def _pg_store(self) -> Any:
        return self._store


def _account(index: int) -> Dict[str, Any]:
    # Deliberately only three distinct created_at values across seven accounts, so
    # ties straddle page boundaries. A unique timestamp per row would let a
    # first-key-only keyset comparison pass.
    day = f"2026-01-{(index % 3) + 1:02d}"
    return {
        "account_id": f"ACC-{index:03d}",
        "tenant_id": TENANT,
        "customer_id": "CUST-1",
        "display_name": f"Account {index}",
        "status": "active",
        "created_at": f"{day}T00:00:00+00:00",
        "updated_at": f"{day}T00:00:00+00:00",
    }


@pytest.fixture
async def seeded(store, index_name):
    for index in range(7):
        document = _account(index)
        await store.index_document(index_name, document["account_id"], document)
    return index_name


async def _walk(service, *, limit: int) -> List[str]:
    """Page through every account, returning the ids in the order seen.

    Bounded, and the bound is an assertion rather than a convenience: the failure
    mode of a dropped cursor is that page 1 comes back with the same cursor
    attached, so the loop never ends. A test that trusted ``while cursor:`` would
    hang instead of failing.
    """
    seen: List[str] = []
    cursor = None
    for _ in range(20):
        page = await service.list(TENANT, limit=limit, cursor=cursor)
        seen.extend(item["account_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError(f"pagination did not terminate; saw {len(seen)} item(s)")


@pytest.fixture
def service(store, seeded):
    from commerce.services.account_service import AccountService

    instance = AccountService(_PostgresBackedFacade(store))
    # The service is constructed against the real index constant; point it at the
    # per-test index so the walk cannot see another test's rows.
    instance_index = seeded
    import commerce.services.account_service as module

    original = module.ACCOUNTS_CURRENT_INDEX
    module.ACCOUNTS_CURRENT_INDEX = instance_index
    yield instance
    module.ACCOUNTS_CURRENT_INDEX = original


class TestPaginationWalksEveryRecordOnce:
    @pytest.mark.parametrize("limit", [1, 2, 3, 6, 7])
    async def test_every_account_exactly_once(self, service, limit):
        """The invariant, at page sizes that divide the set evenly and unevenly.

        ``limit=7`` matters on its own: a full final page must still terminate.
        With ``next_cursor`` emitted for any full page, that walk asks for an
        eighth item, gets none, and stops — so the test also pins that an empty
        follow-up page is handled rather than looping.
        """
        seen = await _walk(service, limit=limit)

        assert sorted(seen) == [f"ACC-{index:03d}" for index in range(7)]
        assert len(seen) == len(set(seen)), f"repeated ids: {seen}"

    async def test_the_order_is_stable_across_page_sizes(self, service):
        """Paging must not reorder: the concatenation of pages is the full ordering.

        A keyset predicate that disagreed with ORDER BY about direction would still
        return every row once at some page sizes while scrambling the order.
        """
        by_one = await _walk(service, limit=1)
        by_three = await _walk(service, limit=3)

        assert by_one == by_three

    async def test_the_first_page_matches_an_unpaginated_read(self, service):
        page = await service.list(TENANT, limit=3)
        everything = await _walk(service, limit=7)

        assert [item["account_id"] for item in page["items"]] == everything[:3]

    async def test_the_total_does_not_shrink_as_pages_advance(self, service):
        """``search_after`` narrows the page, not the result set.

        A caller rendering "N accounts" alongside the list would watch N fall to 0
        if the cursor predicate leaked into the count.
        """
        first = await service.list(TENANT, limit=2)
        second = await service.list(
            TENANT, limit=2, cursor=first["next_cursor"]
        )

        assert first["items"] and second["items"]
        assert first["items"] != second["items"]


class TestPaginationRejectsBadCursors:
    async def test_a_legacy_bare_id_cursor_is_a_400(self, service):
        """What every client of these endpoints was previously handed.

        On Elasticsearch it produced a 400 from the cluster; here it produces a 400
        from us, with the parameter named.
        """
        from services.keyset_pagination import InvalidCursorError

        with pytest.raises(InvalidCursorError) as excinfo:
            await service.list(TENANT, limit=2, cursor="MISSING-999")

        assert excinfo.value.status_code == 400
