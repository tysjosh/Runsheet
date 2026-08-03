"""A retired aggregate must fall back to ES, not raise (latent bug from rev 0007).

Dropping ``shipments_current`` removed ``shipment`` from
``HybridReadRepository._SPECS`` and from the projection registries. Seven
endpoints in ``ops/api/endpoints.py`` still ask for it —
``read_hybrid_search("shipment", ...)``, ``read_hybrid_get``,
``read_hybrid_fetch_for_aggregation`` — and the hybrid read helpers gated only on
``COMMERCE_READ_FROM_POSTGRES``. With that flag on, every one of those calls
constructed a ``HybridReadRepository`` for an unregistered aggregate and got
``ValueError: Unknown hybrid aggregate_type: 'shipment'``.

Nobody hit it because those routes sit behind ``require_ops_enabled``, which
defaults off. That is the argument for fixing it centrally rather than at the
call sites: a latent crash guarded only by a feature flag is one flag flip from
being live, and call sites can be added faster than they can be found.

An aggregate with no Postgres table is not read-cut-over whatever the flag says,
so the helpers now report ``_NOT_CUT_OVER`` and the caller serves the request
from Elasticsearch — the behaviour it had before cutover.
"""
from __future__ import annotations

import pytest

from commerce.services.commerce_persistence_bridge import (
    _NOT_CUT_OVER,
    _hybrid_cut_over,
    read_hybrid_fetch_for_aggregation,
    read_hybrid_get,
    read_hybrid_search,
)
from persistence.read_repositories import HybridReadRepository

RETIRED = "shipment"
LIVE = "fuel_order"


class TestIsRegistered:
    def test_a_retired_aggregate_is_not_registered(self):
        assert HybridReadRepository.is_registered(RETIRED) is False

    def test_a_live_aggregate_is_registered(self):
        assert HybridReadRepository.is_registered(LIVE) is True

    def test_constructing_one_for_a_retired_aggregate_still_raises(self):
        """The guard belongs in the callers, not in a silently permissive ctor.

        Asking for a repository over a table that does not exist is a
        programming error and should say so; the helpers avoid asking.
        """
        with pytest.raises(ValueError, match=RETIRED):
            HybridReadRepository(RETIRED)


class TestCutOverPredicate:
    def test_a_retired_aggregate_is_never_cut_over_even_with_the_flag_on(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "commerce.services.commerce_persistence_bridge.read_from_postgres",
            lambda: True,
        )
        assert _hybrid_cut_over(RETIRED) is False

    def test_a_live_aggregate_is_cut_over_with_the_flag_on(self, monkeypatch):
        monkeypatch.setattr(
            "commerce.services.commerce_persistence_bridge.read_from_postgres",
            lambda: True,
        )
        assert _hybrid_cut_over(LIVE) is True

    def test_nothing_is_cut_over_with_the_flag_off(self, monkeypatch):
        """The counterweight: the registration check must not bypass the flag."""
        monkeypatch.setattr(
            "commerce.services.commerce_persistence_bridge.read_from_postgres",
            lambda: False,
        )
        assert _hybrid_cut_over(LIVE) is False
        assert _hybrid_cut_over(RETIRED) is False


class TestHelpersFallBackRatherThanRaise:
    """The three helper shapes the surviving ops endpoints actually call."""

    @pytest.fixture(autouse=True)
    def _flag_on(self, monkeypatch):
        monkeypatch.setattr(
            "commerce.services.commerce_persistence_bridge.read_from_postgres",
            lambda: True,
        )

    @pytest.mark.asyncio
    async def test_search_returns_not_cut_over(self):
        assert await read_hybrid_search(RETIRED, "t") is _NOT_CUT_OVER

    @pytest.mark.asyncio
    async def test_get_returns_not_cut_over(self):
        assert await read_hybrid_get(RETIRED, "t", "id-1") is _NOT_CUT_OVER

    @pytest.mark.asyncio
    async def test_fetch_for_aggregation_returns_not_cut_over(self):
        assert (
            await read_hybrid_fetch_for_aggregation(RETIRED, "t") is _NOT_CUT_OVER
        )
