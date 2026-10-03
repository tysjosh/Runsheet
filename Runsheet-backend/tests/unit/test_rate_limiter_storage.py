"""
Regression tests for staging finding F5: slowapi counters must be shared across
replicas, so they live in Redis when ``REDIS_URL`` is set.

The module limiter used to be ``Limiter(key_func=get_client_ip)``: in-process
memory, one set of counters per task. It is now Redis-backed outside
``ENVIRONMENT=test`` with an in-memory fallback, so an unreachable Redis keeps
decorated routes limited instead of erroring.

No test here touches the local Redis on 6379: the unreachable-Redis case dials
127.0.0.1:1, which refuses immediately.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from middleware import rate_limiter as rl

_DEAD_REDIS = "redis://127.0.0.1:1/0"


def test_storage_uri_uses_redis_url_outside_test():
    url = "rediss://:tok@h:6379/0"
    assert rl._rate_limit_storage_uri(environment="staging", redis_url=url) == url
    assert (
        rl._rate_limit_storage_uri(
            environment="development", redis_url="redis://localhost:6379"
        )
        == "redis://localhost:6379"
    )


def test_storage_uri_memory_in_test_env():
    assert (
        rl._rate_limit_storage_uri(
            environment="test", redis_url="redis://localhost:6379"
        )
        == "memory://"
    )


def test_storage_uri_memory_without_redis_url():
    assert rl._rate_limit_storage_uri(environment="staging", redis_url="") == "memory://"
    assert (
        rl._rate_limit_storage_uri(environment="production", redis_url="  ")
        == "memory://"
    )


def test_storage_uri_reads_environment_when_args_omitted(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("REDIS_URL", _DEAD_REDIS)
    assert rl._rate_limit_storage_uri() == _DEAD_REDIS
    monkeypatch.delenv("REDIS_URL")
    assert rl._rate_limit_storage_uri() == "memory://"


def test_module_limiter_has_in_memory_fallback():
    assert rl.limiter._in_memory_fallback_enabled is True


def test_redis_backed_limiter_uses_redis_storage(monkeypatch):
    from limits.storage import RedisStorage

    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("REDIS_URL", _DEAD_REDIS)
    limiter = rl.create_rate_limiter()
    assert isinstance(limiter._storage, RedisStorage)
    assert limiter._in_memory_fallback_enabled is True


def test_unreachable_redis_falls_back_to_memory_and_still_limits(monkeypatch):
    from slowapi.errors import RateLimitExceeded

    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("REDIS_URL", _DEAD_REDIS)
    limiter = rl.create_rate_limiter()
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rl._custom_rate_limit_handler)

    @app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request):
        return {"ok": True}

    client = TestClient(app)
    codes = [client.get("/limited").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
