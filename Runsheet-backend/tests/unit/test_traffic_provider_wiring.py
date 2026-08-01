"""
Traffic-aware routing must be reachable, cached, and budgeted.

Two hooks on ``RoutePlanningAgent`` were never called by bootstrap:

* ``set_tenant_config`` — ``_resolve_traffic_provider_name`` returns ``None``
  immediately when it is unset, so ``overlay.traffic_provider:{tenant_id}`` was
  unreadable and traffic-aware routing never engaged for any tenant, even with
  ``overlay.traffic_aware_routing`` switched on.

* ``set_traffic_provider_factory`` — the agent *does* fall back to the
  module-level ``build_traffic_provider(name)`` registry, so this hook is not
  what made a provider constructible. What it does is inject the Redis client.
  The registry fallback calls ``build_traffic_provider(provider_name)`` with no
  kwargs, so ``TrafficProvider.__init__`` receives ``redis_client=None``, which
  silently disables the per-pair 900s matrix cache (Req 2.1.4) *and* the
  per-tenant monthly budget counter (Req 2.1.7). Every route build would hit the
  paid Directions API uncached and unbudgeted.

That second point is why these tests assert on the constructed provider's Redis
handle rather than merely on "a provider came back".

Validates: Requirements 2.1.2, 2.1.4, 2.1.5, 2.1.7
"""

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.route_planning_agent import (
    TRAFFIC_PROVIDER_CONFIG_KEY_TEMPLATE,
    RoutePlanningAgent,
)
from fuel.services.traffic_provider import (
    GoogleDirectionsTrafficProvider,
    HERETrafficProvider,
    MapboxTrafficProvider,
    TrafficProvider,
    build_traffic_provider,
)

TENANT = "tenant-traffic"


def _deps() -> dict:
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws = MagicMock()
    ws.broadcast_activity = AsyncMock()
    ws.broadcast_event = AsyncMock(return_value=0)

    confirmation = MagicMock()
    confirmation.process_mutation = AsyncMock()

    flags = MagicMock()
    flags.get_overlay_state = AsyncMock(return_value="active_auto")
    flags.is_enabled = AsyncMock(return_value=True)

    return {
        "signal_bus": signal_bus,
        "es_service": es,
        "activity_log_service": activity_log,
        "ws_manager": ws,
        "confirmation_protocol": confirmation,
        "autonomy_config_service": MagicMock(),
        "feature_flag_service": flags,
    }


def _redis_with_provider(name: Optional[str]) -> MagicMock:
    """A Redis stub returning ``name`` for the traffic-provider key only."""
    redis = MagicMock()
    key = TRAFFIC_PROVIDER_CONFIG_KEY_TEMPLATE.format(tenant_id=TENANT)

    async def _get(requested: str) -> Any:
        if requested == key and name is not None:
            return name.encode("utf-8")  # decode_responses=False in bootstrap
        return None

    redis.get = AsyncMock(side_effect=_get)
    return redis


def _bootstrap_style_factory(redis_client: Any):
    """The factory bootstrap installs, reproduced for test purposes."""

    def _factory(provider_name: str, tenant_id: str) -> Optional[TrafficProvider]:
        try:
            return build_traffic_provider(
                provider_name, redis_client=redis_client
            )
        except ValueError:
            return None

    return _factory


# ---------------------------------------------------------------------------
# The provider name has to be readable at all
# ---------------------------------------------------------------------------


class TestProviderNameResolution:
    @pytest.mark.asyncio
    async def test_without_tenant_config_no_provider_resolves(self):
        """The pre-fix state: unreadable config means Haversine for everyone."""
        agent = RoutePlanningAgent(**_deps())

        assert agent._tenant_config is None
        assert await agent._resolve_traffic_provider_name(TENANT) is None

    @pytest.mark.asyncio
    async def test_with_tenant_config_the_name_resolves(self):
        """Validates: Requirement 2.1.2"""
        agent = RoutePlanningAgent(**_deps())
        agent.set_tenant_config(_redis_with_provider("mapbox"))

        assert await agent._resolve_traffic_provider_name(TENANT) == "mapbox"

    @pytest.mark.asyncio
    async def test_bytes_values_are_decoded(self):
        """bootstrap builds Redis with ``decode_responses=False``."""
        agent = RoutePlanningAgent(**_deps())
        agent.set_tenant_config(_redis_with_provider("here"))

        assert await agent._resolve_traffic_provider_name(TENANT) == "here"

    @pytest.mark.asyncio
    async def test_an_unconfigured_tenant_resolves_to_none(self):
        """No provider configured is a normal state, not an error."""
        agent = RoutePlanningAgent(**_deps())
        agent.set_tenant_config(_redis_with_provider(None))

        assert await agent._resolve_traffic_provider_name(TENANT) is None


# ---------------------------------------------------------------------------
# The cost-control point: the provider must get the Redis client
# ---------------------------------------------------------------------------


class TestProviderCarriesRedis:
    @pytest.mark.asyncio
    async def test_the_registry_fallback_builds_a_cacheless_provider(self):
        """Pins why the factory is needed rather than the registry fallback.

        ``build_traffic_provider(name)`` with no kwargs yields a provider whose
        cache and budget counter are both inert, because both are keyed off the
        Redis handle. This is the behaviour the factory exists to avoid.
        """
        provider = build_traffic_provider("mapbox")

        assert provider._redis is None

    @pytest.mark.asyncio
    async def test_the_bootstrap_factory_attaches_redis(self):
        """Validates: Requirements 2.1.4, 2.1.7"""
        redis = MagicMock()
        agent = RoutePlanningAgent(**_deps())
        agent.set_tenant_config(_redis_with_provider("mapbox"))
        agent.set_traffic_provider_factory(_bootstrap_style_factory(redis))

        provider = agent._get_or_build_traffic_provider(TENANT, "mapbox")

        assert provider is not None
        assert provider._redis is redis, (
            "provider built without the Redis client — the per-pair cache and "
            "the per-tenant monthly budget counter are both inert, so every "
            "route build bills the Directions API"
        )

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("mapbox", MapboxTrafficProvider),
            ("here", HERETrafficProvider),
            ("google", GoogleDirectionsTrafficProvider),
        ],
    )
    @pytest.mark.asyncio
    async def test_each_supported_provider_builds_with_redis(
        self, name, expected
    ):
        redis = MagicMock()
        factory = _bootstrap_style_factory(redis)

        provider = factory(name, TENANT)

        assert isinstance(provider, expected)
        assert provider._redis is redis

    @pytest.mark.asyncio
    async def test_an_unknown_provider_returns_none_rather_than_cacheless(self):
        """Falling through to the registry would build an unbudgeted provider.

        Returning ``None`` sends the agent to Haversine, which is the correct
        outcome for a misconfigured provider name.
        """
        factory = _bootstrap_style_factory(MagicMock())

        assert factory("not-a-provider", TENANT) is None


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


class TestDegradesToHaversine:
    @pytest.mark.asyncio
    async def test_a_raising_factory_does_not_break_routing(self):
        """The agent logs and falls back; it must not propagate."""
        agent = RoutePlanningAgent(**_deps())
        agent.set_tenant_config(_redis_with_provider("mapbox"))

        def _boom(provider_name: str, tenant_id: str):
            raise RuntimeError("credential store unreachable")

        agent.set_traffic_provider_factory(_boom)

        # Falls through to the registry, which still returns a provider — the
        # point is that no exception escapes.
        provider = agent._get_or_build_traffic_provider(TENANT, "mapbox")
        assert provider is None or isinstance(provider, TrafficProvider)

    @pytest.mark.asyncio
    async def test_a_failing_tenant_config_resolves_to_none(self):
        agent = RoutePlanningAgent(**_deps())
        redis = MagicMock()
        redis.get = AsyncMock(side_effect=RuntimeError("redis down"))
        agent.set_tenant_config(redis)

        assert await agent._resolve_traffic_provider_name(TENANT) is None
