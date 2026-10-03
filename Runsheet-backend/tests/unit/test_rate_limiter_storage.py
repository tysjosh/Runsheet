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


def test_redis_storage_client_has_socket_timeouts(monkeypatch):
    # F5 review: slowapi hits Redis synchronously on the event loop, and
    # redis-py's default is no timeout, so the client must carry its own.
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("REDIS_URL", _DEAD_REDIS)
    limiter = rl.create_rate_limiter()
    kwargs = limiter._storage.storage.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 0.5
    assert kwargs["socket_connect_timeout"] == 0.5


def test_memory_limiter_gets_no_redis_options(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    limiter = rl.create_rate_limiter()
    assert limiter._storage_options == {}


def test_hung_redis_falls_back_quickly(monkeypatch):
    """A Redis that accepts connections but never answers (black-holed
    ElastiCache) must not stall the request: the read times out and slowapi
    falls back to memory. Without timeouts this request blocks indefinitely.

    The "server" is a listening socket that never accepts or replies, on an
    ephemeral 127.0.0.1 port, so the local Redis on 6379 is never touched.
    """
    import socket
    import threading
    import time

    from slowapi.errors import RateLimitExceeded

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    port = server.getsockname()[1]
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
    limiter = rl.create_rate_limiter()
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rl._custom_rate_limit_handler)

    @app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request):
        return {"ok": True}

    client = TestClient(app)
    result: dict = {}

    def call():
        result["codes"] = [client.get("/limited").status_code for _ in range(3)]

    worker = threading.Thread(target=call, daemon=True)
    started = time.monotonic()
    worker.start()
    try:
        worker.join(timeout=10)
        assert not worker.is_alive(), "request stalled on a hung Redis"
        assert time.monotonic() - started < 5
        assert result["codes"] == [200, 200, 429]
    finally:
        # Closing the listener resets the pending connection, which also frees
        # a worker that is still blocked on the old code path.
        server.close()
        worker.join(timeout=5)
