"""An overlay agent must not be gated on a flag nothing sets.

``OverlayAgentBase._get_mode`` reads ``overlay.{agent_id}``, and
``monitor_cycle`` does ``if mode == "disabled": continue`` — it skips the tenant
entirely. Bootstrap seeded 14 *capability* flags
(``overlay.bol_generation``, ``overlay.traffic_aware_routing``, …) and the
agent-level gates were **a disjoint set**, set nowhere and invisible to the
feature-flag admin API. So all twelve overlay and MVP agents skipped every
tenant, and there was nothing to point at while diagnosing it.

Two things made it undiagnosable rather than merely wrong:

* ``get_overlay_state`` returns the *string* ``"disabled"`` for a missing key,
  so ``state or "shadow"`` in ``_get_mode`` never fell through and the
  documented ``shadow`` default was unreachable code.
* Nothing logged the resolved default at boot.

These tests pin the derivation and the absent-vs-disabled distinction.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# The flag key an agent reads must be derivable, and the seeder must use it
# ---------------------------------------------------------------------------


class TestFlagKeyDerivation:
    def test_the_gate_key_is_derived_from_the_agent_id(self):
        from Agents.overlay.base_overlay_agent import OverlayAgentBase

        assert hasattr(OverlayAgentBase, "overlay_flag_key")

    def test_the_seeder_derives_keys_from_the_agents_not_a_literal_list(self):
        """A restated list is what drifted; derivation cannot."""
        from bootstrap.agents import _overlay_agent_flag_keys

        class _Agent:
            def __init__(self, agent_id):
                self.agent_id = agent_id

            def overlay_flag_key(self):
                return f"overlay.{self.agent_id}"

        keys = _overlay_agent_flag_keys(
            [_Agent("tank_forecasting"), _Agent("route_planning")]
        )
        assert keys == ["overlay.tank_forecasting", "overlay.route_planning"]

    def test_agents_without_the_gate_are_skipped_not_fatal(self):
        """Layer-0 autonomous agents have no overlay gate."""
        from bootstrap.agents import _overlay_agent_flag_keys

        assert _overlay_agent_flag_keys([object()]) == []

    def test_the_capability_flags_and_the_agent_gates_stay_distinct(self):
        """Documents the trap: these are two different namespaces.

        If a future change makes a capability flag double as an agent gate,
        this test should be revisited deliberately rather than silently.
        """
        from bootstrap.agents import _FUEL_OPS_FEATURE_FLAG_DEFAULTS

        agent_gates = {
            "overlay.tank_forecasting",
            "overlay.route_planning",
            "overlay.delivery_prioritization",
            "overlay.compartment_loading",
            "overlay.exception_replanning",
        }
        assert not agent_gates & set(_FUEL_OPS_FEATURE_FLAG_DEFAULTS), (
            "an agent gate is now also seeded as a capability flag — confirm "
            "that is intended"
        )

    def test_bootstrap_seeds_the_agent_gates(self):
        """Structural check on the wiring line.

        Importing bootstrap/agents.py needs ES, Redis and a scheduler, so this
        asserts on the source. It fails if the second seeding pass is dropped,
        which is exactly how the gates came to be unseeded.
        """
        import pathlib

        source = pathlib.Path("bootstrap/agents.py").read_text()
        assert "_overlay_agent_flag_keys(" in source
        assert 'label="overlay agent gate"' in source


# ---------------------------------------------------------------------------
# Absent must be distinguishable from explicitly disabled
# ---------------------------------------------------------------------------


class TestAbsentIsNotDisabled:
    @pytest.mark.asyncio
    async def test_a_missing_key_returns_none_from_the_or_none_variant(self):
        from ops.services.feature_flags import FeatureFlagService

        svc = FeatureFlagService(redis_url="redis://localhost:6379")
        svc.client = MagicMock()
        svc.client.get = AsyncMock(return_value=None)

        assert await svc.get_overlay_state_or_none("overlay.x", "t1") is None

    @pytest.mark.asyncio
    async def test_the_legacy_variant_still_reports_disabled(self):
        """Existing fail-closed callers must not change behaviour."""
        from ops.services.feature_flags import FeatureFlagService

        svc = FeatureFlagService(redis_url="redis://localhost:6379")
        svc.client = MagicMock()
        svc.client.get = AsyncMock(return_value=None)

        assert await svc.get_overlay_state("overlay.x", "t1") == "disabled"

    @pytest.mark.asyncio
    async def test_a_redis_failure_fails_closed_rather_than_returning_none(self):
        """An unreachable Redis is not an unconfigured flag.

        Returning None there would let the deployment default activate agents
        during an outage.
        """
        from ops.services.feature_flags import FeatureFlagService

        svc = FeatureFlagService(redis_url="redis://localhost:6379")
        svc.client = MagicMock()
        svc.client.get = AsyncMock(side_effect=RuntimeError("redis down"))

        assert await svc.get_overlay_state_or_none("overlay.x", "t1") == "disabled"

    @pytest.mark.asyncio
    async def test_a_configured_value_is_returned_verbatim(self):
        from ops.services.feature_flags import FeatureFlagService

        svc = FeatureFlagService(redis_url="redis://localhost:6379")
        svc.client = MagicMock()
        svc.client.get = AsyncMock(return_value="active_gated")

        assert (
            await svc.get_overlay_state_or_none("overlay.x", "t1")
            == "active_gated"
        )


# ---------------------------------------------------------------------------
# The deployment default must be a setting, and must be validated
# ---------------------------------------------------------------------------


class TestDeploymentDefaultMode:
    def test_the_default_preserves_todays_behaviour(self):
        """Introducing the setting must not activate twelve agents by itself."""
        from config.settings import Settings

        assert Settings.model_fields["overlay_default_mode"].default == "disabled"

    def test_an_unrecognised_mode_is_rejected(self):
        """A typo would fail the ``== "disabled"`` check and be treated as a
        commit path — activating every overlay agent."""
        import os
        from unittest.mock import patch

        from config.settings import Settings

        env = {
            "ELASTIC_ENDPOINT": "https://es.example.com",
            "ELASTIC_API_KEY": "k",
            "ENVIRONMENT": "development",
            "OVERLAY_DEFAULT_MODE": "enabled",  # not a valid mode
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings()
        assert "overlay_default_mode" in str(exc.value)

    @pytest.mark.parametrize(
        "mode", ["disabled", "shadow", "active_gated", "active_auto"]
    )
    def test_every_mode_the_agents_understand_is_accepted(self, mode):
        import os
        from unittest.mock import patch

        from config.settings import Settings

        env = {
            "ELASTIC_ENDPOINT": "https://es.example.com",
            "ELASTIC_API_KEY": "k",
            "ENVIRONMENT": "development",
            "OVERLAY_DEFAULT_MODE": mode,
        }
        with patch.dict(os.environ, env, clear=True):
            assert Settings().overlay_default_mode == mode

    def test_get_mode_consults_the_setting_rather_than_a_literal(self):
        """Guards against the fallback being hardcoded again."""
        from Agents.overlay.base_overlay_agent import OverlayAgentBase

        source = inspect.getsource(OverlayAgentBase._default_overlay_mode)
        assert "overlay_default_mode" in source


# ---------------------------------------------------------------------------
# End-to-end: what an unset tenant actually resolves to
# ---------------------------------------------------------------------------


class _Flags:
    """Feature-flag stand-in whose stored value is scriptable."""

    def __init__(self, value=None, raises=False):
        self._value = value
        self._raises = raises
        self.asked = []

    async def get_overlay_state_or_none(self, flag_key, tenant_id):
        if self._raises:
            raise AttributeError("older service")
        self.asked.append((flag_key, tenant_id))
        return self._value

    async def get_overlay_state(self, flag_key, tenant_id):
        self.asked.append((flag_key, tenant_id))
        return self._value if self._value is not None else "disabled"

    async def is_enabled(self, tenant_id):
        return True


def _agent(flags):
    """Build a minimal concrete overlay agent around ``flags``."""
    from Agents.overlay.base_overlay_agent import OverlayAgentBase

    class _Probe(OverlayAgentBase):
        async def evaluate(self, signals):  # pragma: no cover - unused here
            return []

    return _Probe(
        agent_id="probe",
        subscriptions=[],
        signal_bus=MagicMock(),
        es_service=MagicMock(),
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=flags,
    )


class TestModeResolution:
    @pytest.mark.asyncio
    async def test_it_asks_for_the_agent_scoped_key(self):
        flags = _Flags(value=None)
        agent = _agent(flags)

        await agent._get_mode("tenant-a")

        assert flags.asked == [("overlay.probe", "tenant-a")], (
            "the gate must be the agent-scoped key; a capability flag name "
            "here is the mismatch that disabled every overlay agent"
        )

    @pytest.mark.asyncio
    async def test_an_unset_tenant_takes_the_deployment_default(self, monkeypatch):
        """The previously unreachable path.

        A missing key used to yield the string "disabled" regardless of intent.
        """
        agent = _agent(_Flags(value=None))
        monkeypatch.setattr(
            type(agent), "_default_overlay_mode", staticmethod(lambda: "shadow")
        )

        assert await agent._get_mode("tenant-a") == "shadow"

    @pytest.mark.asyncio
    async def test_an_explicit_disabled_still_wins_over_the_default(self):
        """Opting a tenant out must not be overridden by the deployment default."""
        agent = _agent(_Flags(value="disabled"))

        assert await agent._get_mode("tenant-a") == "disabled"

    @pytest.mark.asyncio
    async def test_an_explicit_mode_is_honoured(self):
        agent = _agent(_Flags(value="active_auto"))

        assert await agent._get_mode("tenant-a") == "active_auto"

    @pytest.mark.asyncio
    async def test_shadow_is_not_a_commit_path(self):
        """The default being safe is what makes flipping it defensible."""
        agent = _agent(_Flags(value="shadow"))

        assert await agent._is_active_commit_mode("tenant-a") is False

    @pytest.mark.asyncio
    async def test_an_older_flag_service_still_resolves(self):
        """``get_overlay_state_or_none`` may be absent on an older service."""
        agent = _agent(_Flags(value="active_gated", raises=True))

        assert await agent._get_mode("tenant-a") == "active_gated"

    @pytest.mark.asyncio
    async def test_a_disabled_tenant_is_skipped_by_the_cycle(self):
        """Ties the flag to the observable consequence.

        ``monitor_cycle`` skipping is why an unset gate meant the agent did
        nothing at all rather than merely not committing.
        """
        agent = _agent(_Flags(value="disabled"))
        agent.evaluate = AsyncMock(return_value=[])
        agent._pending_work_tenants = lambda: ["tenant-a"]

        await agent.monitor_cycle()

        assert agent.evaluate.await_count == 0

    @pytest.mark.asyncio
    async def test_a_shadow_tenant_is_evaluated(self):
        agent = _agent(_Flags(value="shadow"))
        agent.evaluate = AsyncMock(return_value=[])
        agent._pending_work_tenants = lambda: ["tenant-a"]

        await agent.monitor_cycle()

        assert agent.evaluate.await_count == 1
