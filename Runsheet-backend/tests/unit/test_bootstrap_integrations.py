"""Unit tests for ``bootstrap.integrations.initialize``.

This module had **zero** test coverage, which the changed-file coverage gate
caught the first time it ever ran (it sits behind ``needs: backend-tests``, and
that job had been failing earlier on an undeclared ``pypdf`` import).

Zero coverage is worth fixing here rather than excluding, because the module's
whole job is the boot-order repair described in its own comment: the
``OrderIntakePipeline`` is constructed in ``bootstrap/fuel.py`` (boot order #5),
*before* this module (#11) builds the ``IntakeChannelRepository`` and before
agents (#10) register the ``credentials_vault``. So the pipeline is created with
both set to ``None``, and without the late-injection performed here "every
dispatcher order create / webhook ingest 500s in
``_resolve_dispatcher_channel``".

That is a wiring guarantee, and wiring is exactly what has broken silently in
this codebase before — a stale ``_STAGE_BUFFER_MAP`` entry once meant three of
four pipeline stages were never evaluated on any production path while the run
still reported success. A guarantee that only holds at startup, and whose
failure mode is a 500 on the primary intake route, should not rest on nobody
having reordered the bootstrap.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from bootstrap.container import ServiceContainer
import bootstrap.integrations as bootstrap_integrations


class _RecordingPipeline:
    """Captures what the bootstrap injects, in the order it arrives."""

    def __init__(self) -> None:
        self.intake_channel_repo: Any = None
        self.credentials_vault: Any = None
        self.customer_tank_repo: Any = None
        self.calls: List[str] = []

    def set_intake_channel_repo(self, repo: Any) -> None:
        self.calls.append("set_intake_channel_repo")
        self.intake_channel_repo = repo

    def set_credentials_vault(self, vault: Any) -> None:
        self.calls.append("set_credentials_vault")
        self.credentials_vault = vault

    def set_customer_tank_repo(self, repo: Any) -> None:
        self.calls.append("set_customer_tank_repo")
        self.customer_tank_repo = repo


def _es() -> MagicMock:
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    es.get_document = AsyncMock(return_value=None)
    return es


def _container(
    *,
    with_vault: bool = True,
    pipeline: Optional[_RecordingPipeline] = None,
) -> ServiceContainer:
    container = ServiceContainer()
    container.es_service = _es()
    if with_vault:
        container.credentials_vault = MagicMock()
    if pipeline is not None:
        container.order_intake_pipeline = pipeline
    return container


class TestMissingCredentialsVault:
    """Without the vault the module must bail out, not half-wire itself."""

    @pytest.mark.asyncio
    async def test_returns_without_registering_the_repository(self):
        container = _container(with_vault=False)

        await bootstrap_integrations.initialize(MagicMock(), container)

        assert not container.has("intake_channel_repository"), (
            "the repository was registered without a credentials_vault, so it "
            "cannot decrypt channel secrets"
        )

    @pytest.mark.asyncio
    async def test_says_which_boot_order_assumption_was_violated(self, caplog):
        """The warning has to be actionable: it names the ordering dependency."""
        container = _container(with_vault=False)

        with caplog.at_level(logging.WARNING):
            await bootstrap_integrations.initialize(MagicMock(), container)

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "credentials_vault" in messages
        assert "bootstrap/agents.py" in messages, (
            "the warning should name the module that must run first"
        )

    @pytest.mark.asyncio
    async def test_does_not_touch_the_pipeline(self):
        """Bailing out must not leave a partially-injected pipeline behind."""
        pipeline = _RecordingPipeline()
        container = _container(with_vault=False, pipeline=pipeline)

        await bootstrap_integrations.initialize(MagicMock(), container)

        assert pipeline.calls == []


class TestLateInjectionIntoThePipeline:
    """The boot-order repair this module exists to perform."""

    @pytest.mark.asyncio
    async def test_registers_the_intake_channel_repository(self):
        container = _container()

        await bootstrap_integrations.initialize(MagicMock(), container)

        assert container.has("intake_channel_repository")

    @pytest.mark.asyncio
    async def test_injects_both_dependencies_the_pipeline_was_built_without(self):
        """The guarantee: without this, dispatcher order create 500s."""
        pipeline = _RecordingPipeline()
        container = _container(pipeline=pipeline)

        await bootstrap_integrations.initialize(MagicMock(), container)

        assert pipeline.intake_channel_repo is not None, (
            "intake_channel_repo was never injected — _resolve_dispatcher_channel "
            "will raise AttributeError on None"
        )
        assert pipeline.credentials_vault is not None
        assert pipeline.intake_channel_repo is container.intake_channel_repository, (
            "the pipeline got a different repository instance than the one "
            "registered on the container"
        )

    @pytest.mark.asyncio
    async def test_warns_when_there_is_no_pipeline_to_inject_into(self, caplog):
        """Silence here would hide a broken primary intake path."""
        container = _container(pipeline=None)

        with caplog.at_level(logging.WARNING):
            await bootstrap_integrations.initialize(MagicMock(), container)

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "order_intake_pipeline" in messages

    @pytest.mark.asyncio
    async def test_a_pipeline_setter_that_raises_does_not_abort_the_boot(self):
        """One failed injection must not take the whole application down.

        The counterweight to the warnings above: this module is startup wiring,
        so a failure has to be reported rather than propagated, or a single bad
        dependency stops the process from booting at all.
        """
        pipeline = _RecordingPipeline()
        pipeline.set_intake_channel_repo = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("setter exploded")
        )
        container = _container(pipeline=pipeline)

        await bootstrap_integrations.initialize(MagicMock(), container)

        # Reached the later wiring despite the failure above.
        assert container.has("intake_channel_repository")


class TestCustomerTankWiring:
    """The import workflow is wired last because it needs the injected pipeline."""

    @pytest.mark.asyncio
    async def test_registers_the_tank_repository_and_import_service(self):
        container = _container(pipeline=_RecordingPipeline())

        await bootstrap_integrations.initialize(MagicMock(), container)

        assert container.has("customer_tank_repository")
        assert container.has("tank_import_service")

    @pytest.mark.asyncio
    async def test_gives_the_pipeline_its_customer_tank_repository(self):
        pipeline = _RecordingPipeline()
        container = _container(pipeline=pipeline)

        await bootstrap_integrations.initialize(MagicMock(), container)

        assert pipeline.customer_tank_repo is container.customer_tank_repository
