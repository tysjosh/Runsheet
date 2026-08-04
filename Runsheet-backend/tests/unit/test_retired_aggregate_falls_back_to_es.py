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


class TestWriteSideDoesNotMirrorARetiredAggregate:
    """The read helpers were only half of it — two write paths mirrored too.

    ``OpsElasticsearchService.upsert_shipment_current`` dual-wrote every applied
    upsert into the ``shipment`` current-state table, and ``seed_all_data``'s
    shipment seeder wrote there exclusively. Rev 0007 dropped that table, so the
    seeder failed its whole entity outright and the ops path logged a
    "Postgres dual-write failed" for a table that no longer exists — a real
    error line for a write that should never have been attempted.

    Read-side fallback cannot help here: there is nowhere to fall back *to* for
    a write, so the mirror has to go rather than degrade.
    """

    def test_the_current_state_repository_does_not_register_shipment(self):
        from persistence.repositories import CurrentStateRepository

        assert RETIRED not in CurrentStateRepository._SPECS
        with pytest.raises(ValueError, match=RETIRED):
            CurrentStateRepository(RETIRED)

    @pytest.mark.asyncio
    async def test_an_applied_shipment_upsert_mirrors_nothing(self, monkeypatch):
        from ops.services.ops_es_service import OpsElasticsearchService

        calls: list = []

        async def _record(aggregate_type, doc, **kwargs):
            calls.append(aggregate_type)

        monkeypatch.setattr(
            "commerce.services.commerce_persistence_bridge."
            "mirror_current_state_upsert",
            _record,
        )

        service = OpsElasticsearchService.__new__(OpsElasticsearchService)

        async def _applied(**kwargs):
            return True

        monkeypatch.setattr(service, "_scripted_upsert", _applied, raising=False)

        applied = await service.upsert_shipment_current(
            {"shipment_id": "SHP-1", "tenant_id": "t", "status": "delivered"}
        )

        assert applied is True, "the ES write must still be reported"
        assert calls == [], f"mirrored into a retired aggregate: {calls}"

    def test_the_seeder_never_opens_a_repository_over_the_retired_aggregate(self):
        """``seed_all_data`` must not reach for the dropped table.

        Asserted on the source rather than by calling the seeder because
        importing ``seed_all_data`` builds a live Elasticsearch client at module
        scope. The two constructor calls below are verbatim what raised, and
        they are scoped to the whole module so moving the code into a helper
        does not slip past the check.
        """
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[2] / "seed_all_data.py"
        ).read_text()

        for constructor in (
            f'CurrentStateRepository("{RETIRED}")',
            f'HybridReadRepository("{RETIRED}")',
        ):
            assert constructor not in source, (
                f"seed_all_data.py still constructs {constructor} — rev 0007 "
                "dropped that table"
            )
        assert "SHIPMENTS_CURRENT" in source, (
            "the shipment seeder should target the surviving ES index"
        )
