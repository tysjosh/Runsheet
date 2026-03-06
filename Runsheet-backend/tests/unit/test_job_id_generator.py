"""
Unit tests for JobIdGenerator.

Tests sequential ID generation via Redis INCR and UUID fallback
when Redis is unavailable.

Requirement 2.2: Job IDs in JOB_{sequential_number} format.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scheduling.services.job_id_generator import JobIdGenerator


@pytest.mark.asyncio
async def test_next_id_returns_sequential_format_with_redis():
    """Redis INCR returns a counter; next_id should return JOB_{counter}."""
    gen = JobIdGenerator(redis_url="redis://localhost:6379")

    mock_client = AsyncMock()
    mock_client.incr = AsyncMock(return_value=42)
    gen._client = mock_client

    result = await gen.next_id()
    assert result == "JOB_42"
    mock_client.incr.assert_awaited_once_with(JobIdGenerator.KEY)


@pytest.mark.asyncio
async def test_next_id_increments_sequentially():
    """Successive calls should produce incrementing IDs."""
    gen = JobIdGenerator(redis_url="redis://localhost:6379")

    mock_client = AsyncMock()
    mock_client.incr = AsyncMock(side_effect=[1, 2, 3])
    gen._client = mock_client

    ids = [await gen.next_id() for _ in range(3)]
    assert ids == ["JOB_1", "JOB_2", "JOB_3"]


@pytest.mark.asyncio
async def test_next_id_falls_back_to_uuid_when_no_redis_url():
    """Without a redis_url, should produce UUID-based IDs."""
    gen = JobIdGenerator(redis_url=None)

    result = await gen.next_id()
    assert result.startswith("JOB_")
    # UUID hex prefix is 8 chars
    suffix = result[len("JOB_"):]
    assert len(suffix) == 8


@pytest.mark.asyncio
async def test_next_id_falls_back_to_uuid_on_redis_error():
    """If Redis INCR raises, should fall back to UUID-based ID."""
    gen = JobIdGenerator(redis_url="redis://localhost:6379")

    mock_client = AsyncMock()
    mock_client.incr = AsyncMock(side_effect=Exception("connection lost"))
    gen._client = mock_client

    result = await gen.next_id()
    assert result.startswith("JOB_")
    suffix = result[len("JOB_"):]
    assert len(suffix) == 8


@pytest.mark.asyncio
async def test_next_id_falls_back_to_uuid_on_connect_failure():
    """If connecting to Redis fails, should fall back to UUID-based ID."""
    gen = JobIdGenerator(redis_url="redis://bad-host:6379")

    # Simulate _get_client failing by making the import raise
    async def _failing_get_client():
        raise Exception("cannot connect")

    # Force _get_client to return None (simulating connection failure)
    gen._redis_url = "redis://bad-host:6379"
    with patch.object(gen, "_get_client", return_value=None):
        result = await gen.next_id()
        assert result.startswith("JOB_")
        suffix = result[len("JOB_"):]
        assert len(suffix) == 8


@pytest.mark.asyncio
async def test_uuid_fallback_ids_are_unique():
    """UUID-based fallback IDs should be unique across calls."""
    gen = JobIdGenerator(redis_url=None)

    ids = {await gen.next_id() for _ in range(50)}
    assert len(ids) == 50


@pytest.mark.asyncio
async def test_close_closes_redis_client():
    """close() should close the Redis client and reset it to None."""
    gen = JobIdGenerator(redis_url="redis://localhost:6379")

    mock_client = AsyncMock()
    gen._client = mock_client

    await gen.close()
    mock_client.close.assert_awaited_once()
    assert gen._client is None


@pytest.mark.asyncio
async def test_close_is_safe_when_no_client():
    """close() should be a no-op when no client is connected."""
    gen = JobIdGenerator(redis_url=None)
    await gen.close()  # Should not raise


@pytest.mark.asyncio
async def test_key_constant():
    """The Redis key should match the expected value."""
    assert JobIdGenerator.KEY == "scheduling:job_id_counter"
