"""A feature flag must not expire underneath a tenant that is being served.

Both write paths set a 90-day TTL and nothing renewed it. ``enable`` even
documented the intent — "active tenants refresh this on every enable call" — but
the only caller is the admin endpoint, so a tenant enabled once and left alone
reverted to the fail-closed default on day 90. There was no expiry event, no log
at the moment it happened, and no way to tell "never enabled" from "expired".

For the ops master flag that is an outage rather than a degrade: a disabled
tenant gets 404 on the ops API and its webhooks are skipped (Req 27.2, 27.3). For
the overlay flags it silently reverts a tenant to ``overlay_default_mode``.

Reads now slide the window, which keeps the reason the TTL was there — an
offboarded tenant's keys clean themselves up, because a tenant nothing reads for
90 days is a tenant nothing is serving.
"""
from __future__ import annotations

import pytest

from ops.services.feature_flags import (
    FLAG_TTL_SECONDS,
    OVERLAY_PREFIX,
    FeatureFlagService,
)


class _FakeRedis:
    """Records ``expire`` calls so a test can assert the window slid."""

    def __init__(self, store=None, *, expire_raises=False):
        self.store = dict(store or {})
        self.expire_calls: list[tuple[str, int]] = []
        self.set_calls: list[tuple[str, str, int | None]] = []
        self._expire_raises = expire_raises

    async def get(self, key):
        return self.store.get(key)

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.set_calls.append((key, value, ex))

    async def delete(self, key):
        self.store.pop(key, None)

    async def expire(self, key, ttl):
        if self._expire_raises:
            raise RuntimeError("EXPIRE unsupported by this server")
        self.expire_calls.append((key, ttl))
        return key in self.store


def _service(redis):
    svc = FeatureFlagService(redis_url="redis://localhost:6379")
    svc.client = redis
    return svc


class TestReadsSlideTheWindow:
    @pytest.mark.asyncio
    async def test_is_enabled_refreshes_the_master_flag(self):
        redis = _FakeRedis({"ops_ff:tenant-a": "1"})
        svc = _service(redis)

        assert await svc.is_enabled("tenant-a") is True

        assert redis.expire_calls == [("ops_ff:tenant-a", FLAG_TTL_SECONDS)], (
            "an enabled tenant's flag was read without sliding its expiry — it "
            "will silently revert to disabled and take the ops API down with it"
        )

    @pytest.mark.asyncio
    async def test_reading_an_overlay_state_refreshes_it(self):
        key = f"{OVERLAY_PREFIX}overlay.tank_forecasting:tenant-a"
        redis = _FakeRedis({key: "active_gated"})
        svc = _service(redis)

        assert await svc.get_overlay_state_or_none(
            "overlay.tank_forecasting", "tenant-a"
        ) == "active_gated"
        assert redis.expire_calls == [(key, FLAG_TTL_SECONDS)]

    @pytest.mark.asyncio
    async def test_the_string_form_refreshes_too(self):
        """``get_overlay_state`` delegates, so it must inherit the refresh."""
        key = f"{OVERLAY_PREFIX}overlay.route_planning:tenant-a"
        redis = _FakeRedis({key: "shadow"})
        svc = _service(redis)

        assert await svc.get_overlay_state("overlay.route_planning", "tenant-a") == "shadow"
        assert redis.expire_calls == [(key, FLAG_TTL_SECONDS)]


class TestRefreshDoesNotCreateOrResurrectKeys:
    @pytest.mark.asyncio
    async def test_an_absent_master_flag_is_not_refreshed(self):
        """Refreshing a missing key would be pointless; not doing it is the check.

        More importantly, a disabled tenant must stay disabled — a read must
        never be able to bring a flag back.
        """
        redis = _FakeRedis({})
        svc = _service(redis)

        assert await svc.is_enabled("tenant-a") is False
        assert redis.expire_calls == []
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_an_unset_overlay_flag_is_not_refreshed(self):
        redis = _FakeRedis({})
        svc = _service(redis)

        assert await svc.get_overlay_state_or_none("overlay.x", "tenant-a") is None
        assert redis.expire_calls == []
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_disable_still_removes_the_key(self):
        redis = _FakeRedis({"ops_ff:tenant-a": "1"})
        svc = _service(redis)

        await svc.disable("tenant-a", user_id="op-1")

        assert "ops_ff:tenant-a" not in redis.store
        assert await svc.is_enabled("tenant-a") is False


class TestRefreshIsBestEffort:
    @pytest.mark.asyncio
    async def test_a_server_without_expire_still_answers_the_read(self):
        """The value is already in hand; a failed refresh must not lose it."""
        key = f"{OVERLAY_PREFIX}overlay.tank_forecasting:tenant-a"
        redis = _FakeRedis({key: "active_auto"}, expire_raises=True)
        svc = _service(redis)

        assert await svc.get_overlay_state_or_none(
            "overlay.tank_forecasting", "tenant-a"
        ) == "active_auto"

    @pytest.mark.asyncio
    async def test_is_enabled_survives_a_failed_refresh(self):
        redis = _FakeRedis({"ops_ff:tenant-a": "1"}, expire_raises=True)
        svc = _service(redis)

        assert await svc.is_enabled("tenant-a") is True


class TestWritesUseTheSharedConstant:
    """Two hardcoded ``90 * 24 * 60 * 60`` literals were how these drifted apart."""

    @pytest.mark.asyncio
    async def test_enable_writes_the_shared_ttl(self):
        redis = _FakeRedis({})
        svc = _service(redis)

        await svc.enable("tenant-a", user_id="op-1")

        assert redis.set_calls == [("ops_ff:tenant-a", "1", FLAG_TTL_SECONDS)]

    @pytest.mark.asyncio
    async def test_set_overlay_state_writes_the_shared_ttl(self):
        redis = _FakeRedis({})
        svc = _service(redis)

        previous = await svc.set_overlay_state(
            "overlay.tank_forecasting", "tenant-a", "shadow", user_id="op-1"
        )

        assert previous == "disabled"
        key = f"{OVERLAY_PREFIX}overlay.tank_forecasting:tenant-a"
        assert redis.set_calls == [(key, "shadow", FLAG_TTL_SECONDS)]
