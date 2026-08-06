"""Readiness probes the store that actually serves documents.

``elasticsearch`` was a **critical** health dependency, so with the cluster gone
``ping()`` answers False, the aggregate status becomes ``unhealthy``, and an
orchestrator pulls a working service out of rotation over a dependency nothing
reads. Worse than a cosmetic wrong label: it is an outage caused by the migration
succeeding.

So the check probes Postgres and reports it by name. Three tests here followed the
``DOCUMENT_STORE_BACKEND`` switch — that the name tracked the flag, that a settings
failure fell back to the legacy name, and that a failed ``elasticsearch`` check
stayed critical so a rollback kept its semantics. The switch is gone: there is one
store, ``_document_store_name`` returns a constant, and a rollback is a cluster
restore rather than a flag. The ``elasticsearch`` name is still treated as critical
by ``_determine_overall_status`` and that is deliberately left alone — it costs
nothing and it is the safe direction for any caller that still constructs one.

What is asserted here is what still has teeth: the probe does not consult the
cluster, and it can fail.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from health.service import HealthCheckService


class TestTheProbedStoreIsPostgres:
    def test_it_names_postgres(self):
        assert HealthCheckService._document_store_name() == "postgres"


class TestAMissingClusterDoesNotMarkTheServiceUnhealthy:
    async def test_a_stopped_cluster_is_irrelevant(self, monkeypatch):
        """The regression this exists to prevent.

        The stand-in's ``ping()`` returns False, which is honest — and must not be
        consulted, because Elasticsearch is not the document store any more.
        """
        from services.no_cluster import NoClusterClient

        es_service = MagicMock()
        es_service.client = NoClusterClient()
        service = HealthCheckService(es_service)

        async def _postgres_is_up():
            return True

        monkeypatch.setattr(service, "_ping_document_store", _postgres_is_up)
        result = await service._check_document_store()

        assert result.name == "postgres"
        assert result.healthy is True

    async def test_the_probe_does_not_touch_the_client_at_all(self):
        """Stronger than the test above, which stubs the probe out.

        ``NoClusterClient``'s data plane raises and its control plane no-ops, so a
        probe that still reached for the cluster would either blow up or quietly
        report a made-up answer. Asserting on a client whose every attribute access
        is recorded catches both.
        """
        touched: list = []

        class _Tripwire:
            def __getattr__(self, name):
                touched.append(name)
                raise AssertionError(
                    f"the readiness probe reached the Elasticsearch client "
                    f"(.{name}); it must probe Postgres"
                )

        es_service = MagicMock()
        es_service.client = _Tripwire()
        service = HealthCheckService(es_service)

        # The real probe, against the real database if one is configured. Either
        # outcome is fine — what matters is that the client was never consulted.
        try:
            await service._check_document_store()
        except Exception:  # noqa: BLE001 — no database configured is an acceptable outcome
            pass

        assert touched == []

    async def test_an_unreachable_database_is_unhealthy(self, monkeypatch):
        """The probe has to be able to fail, or it is decoration."""
        service = HealthCheckService(MagicMock())

        async def _postgres_is_down():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(service, "_ping_document_store", _postgres_is_down)
        result = await service._check_document_store()

        assert result.name == "postgres"
        assert result.healthy is False
        assert "connection refused" in result.error


class TestTheAggregateStatus:
    def test_a_failed_postgres_check_is_unhealthy_not_degraded(self):
        """Documents unreachable is not a degradation."""
        from health.service import DependencyHealth

        service = HealthCheckService(MagicMock())
        status = service._determine_overall_status(
            [DependencyHealth(name="postgres", healthy=False, response_time_ms=1.0)]
        )

        assert status == "unhealthy"

    def test_a_failed_session_store_is_only_degraded(self):
        """Unchanged: sessions are recoverable, documents are not."""
        from health.service import DependencyHealth

        service = HealthCheckService(MagicMock())
        status = service._determine_overall_status(
            [
                DependencyHealth(name="postgres", healthy=True, response_time_ms=1.0),
                DependencyHealth(name="session_store", healthy=False, response_time_ms=1.0),
            ]
        )

        assert status == "degraded"
