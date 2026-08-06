"""Readiness probes the store that actually serves documents.

``elasticsearch`` was a **critical** health dependency, so with the cluster gone
``ping()`` answers False, the aggregate status becomes ``unhealthy``, and an
orchestrator pulls a working service out of rotation over a dependency nothing
reads. Worse than a cosmetic wrong label: it is an outage caused by the migration
succeeding.

So the check follows the backend. The reported dependency name says which store was
probed, and both names are critical — in either configuration, a failure means
documents are unreachable.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from health.service import HealthCheckService


class _FakeSettings:
    def __init__(self, postgres: bool) -> None:
        self.document_store_is_postgres = postgres


def _pin_backend(monkeypatch, *, postgres: bool) -> None:
    monkeypatch.setattr(
        "config.settings.get_settings", lambda: _FakeSettings(postgres)
    )


class TestTheProbedStoreFollowsTheBackend:
    def test_it_names_postgres_on_the_postgres_backend(self, monkeypatch):
        _pin_backend(monkeypatch, postgres=True)
        assert HealthCheckService._document_store_name() == "postgres"

    def test_it_names_elasticsearch_on_the_legacy_backend(self, monkeypatch):
        _pin_backend(monkeypatch, postgres=False)
        assert HealthCheckService._document_store_name() == "elasticsearch"

    def test_a_settings_failure_falls_back_to_elasticsearch(self, monkeypatch):
        """The conservative direction: report the legacy dependency rather than
        claim a Postgres store that may not be configured."""
        def _boom():
            raise RuntimeError("settings unavailable")

        monkeypatch.setattr("config.settings.get_settings", _boom)
        assert HealthCheckService._document_store_name() == "elasticsearch"


class TestAMissingClusterDoesNotMarkTheServiceUnhealthy:
    async def test_a_stopped_cluster_is_irrelevant_on_postgres(self, monkeypatch):
        """The regression this exists to prevent.

        The stand-in's ``ping()`` returns False, which is honest — and must not be
        consulted, because Elasticsearch is not the document store any more.
        """
        from services.no_cluster import NoClusterClient

        _pin_backend(monkeypatch, postgres=True)
        es_service = MagicMock()
        es_service.client = NoClusterClient()
        service = HealthCheckService(es_service)

        async def _postgres_is_up():
            return True

        monkeypatch.setattr(service, "_ping_document_store", _postgres_is_up)
        result = await service._check_document_store()

        assert result.name == "postgres"
        assert result.healthy is True

    async def test_an_unreachable_database_is_unhealthy_on_postgres(self, monkeypatch):
        """The probe has to be able to fail, or it is decoration."""
        _pin_backend(monkeypatch, postgres=True)
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
        """Documents unreachable is not a degradation, whichever store it is."""
        from health.service import DependencyHealth

        service = HealthCheckService(MagicMock())
        status = service._determine_overall_status(
            [DependencyHealth(name="postgres", healthy=False, response_time_ms=1.0)]
        )

        assert status == "unhealthy"

    def test_a_failed_elasticsearch_check_is_still_unhealthy(self):
        """The legacy name stays critical so a rollback keeps its semantics."""
        from health.service import DependencyHealth

        service = HealthCheckService(MagicMock())
        status = service._determine_overall_status(
            [DependencyHealth(name="elasticsearch", healthy=False, response_time_ms=1.0)]
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
