"""
Property-based tests for Job ID Uniqueness.

# Feature: logistics-scheduling, Property 6: Job ID Uniqueness

**Validates: Requirement 2.2**

For any batch of concurrent job ID generation calls, all returned IDs must
be unique. This holds for:
- The Redis sequential path (JOB_{counter})
- The UUID fallback path (JOB_{uuid[:8]})
- Mixed mode where some IDs come from Redis and some from UUID fallback

Additionally, every generated ID must match the JOB_ prefix format.
"""

import asyncio
import re
from itertools import count
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis.strategies import integers

from scheduling.services.job_id_generator import JobIdGenerator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JOB_ID_PATTERN = re.compile(r"^JOB_.+$")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Batch sizes between 2 and 50 to test concurrent generation
_batch_sizes = integers(min_value=2, max_value=50)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_redis_mock():
    """Return a mock Redis client whose incr returns incrementing values."""
    counter = count(1)
    mock_client = AsyncMock()
    mock_client.incr = AsyncMock(side_effect=lambda _key: next(counter))
    return mock_client


# ---------------------------------------------------------------------------
# Property 6a – Redis path uniqueness
# ---------------------------------------------------------------------------
class TestRedisPathUniqueness:
    """**Validates: Requirement 2.2**"""

    @given(batch_size=_batch_sizes)
    @settings(max_examples=200)
    async def test_concurrent_redis_ids_are_unique(self, batch_size: int):
        """
        When Redis is available, concurrent calls to next_id() must all
        produce unique IDs via the atomic INCR counter.

        **Validates: Requirement 2.2**
        """
        gen = JobIdGenerator(redis_url="redis://localhost:6379")
        gen._client = _make_redis_mock()

        ids = await asyncio.gather(*(gen.next_id() for _ in range(batch_size)))

        assert len(ids) == len(set(ids)), (
            f"Expected {batch_size} unique IDs from Redis path, "
            f"but got {len(set(ids))} unique out of {len(ids)}: "
            f"duplicates={[x for x in ids if ids.count(x) > 1]}"
        )


# ---------------------------------------------------------------------------
# Property 6b – UUID fallback uniqueness
# ---------------------------------------------------------------------------
class TestUUIDFallbackUniqueness:
    """**Validates: Requirement 2.2**"""

    @given(batch_size=_batch_sizes)
    @settings(max_examples=200)
    async def test_concurrent_uuid_ids_are_unique(self, batch_size: int):
        """
        When Redis is unavailable (redis_url=None), concurrent calls to
        next_id() must all produce unique IDs via UUID fallback.

        **Validates: Requirement 2.2**
        """
        gen = JobIdGenerator(redis_url=None)

        ids = await asyncio.gather(*(gen.next_id() for _ in range(batch_size)))

        assert len(ids) == len(set(ids)), (
            f"Expected {batch_size} unique IDs from UUID fallback, "
            f"but got {len(set(ids))} unique out of {len(ids)}: "
            f"duplicates={[x for x in ids if ids.count(x) > 1]}"
        )


# ---------------------------------------------------------------------------
# Property 6c – Mixed mode uniqueness
# ---------------------------------------------------------------------------
class TestMixedModeUniqueness:
    """**Validates: Requirement 2.2**"""

    @given(
        redis_count=integers(min_value=1, max_value=25),
        uuid_count=integers(min_value=1, max_value=25),
    )
    @settings(max_examples=200)
    async def test_mixed_redis_and_uuid_ids_are_unique(
        self, redis_count: int, uuid_count: int
    ):
        """
        When some IDs come from the Redis path and some from the UUID
        fallback (simulating intermittent Redis failures), all IDs must
        still be unique.

        **Validates: Requirement 2.2**
        """
        # Generate IDs from Redis path
        redis_gen = JobIdGenerator(redis_url="redis://localhost:6379")
        redis_gen._client = _make_redis_mock()
        redis_ids = await asyncio.gather(
            *(redis_gen.next_id() for _ in range(redis_count))
        )

        # Generate IDs from UUID fallback path
        uuid_gen = JobIdGenerator(redis_url=None)
        uuid_ids = await asyncio.gather(
            *(uuid_gen.next_id() for _ in range(uuid_count))
        )

        all_ids = list(redis_ids) + list(uuid_ids)

        assert len(all_ids) == len(set(all_ids)), (
            f"Expected {len(all_ids)} unique IDs in mixed mode "
            f"({redis_count} Redis + {uuid_count} UUID), "
            f"but got {len(set(all_ids))} unique: "
            f"duplicates={[x for x in all_ids if all_ids.count(x) > 1]}"
        )


# ---------------------------------------------------------------------------
# Property 6d – Format validity
# ---------------------------------------------------------------------------
class TestJobIdFormatValidity:
    """**Validates: Requirement 2.2**"""

    @given(batch_size=_batch_sizes)
    @settings(max_examples=200)
    async def test_all_ids_match_job_prefix_format(self, batch_size: int):
        """
        All generated IDs must match the JOB_ prefix pattern, regardless
        of whether they come from the Redis or UUID path.

        **Validates: Requirement 2.2**
        """
        # Test Redis path
        redis_gen = JobIdGenerator(redis_url="redis://localhost:6379")
        redis_gen._client = _make_redis_mock()
        redis_ids = await asyncio.gather(
            *(redis_gen.next_id() for _ in range(batch_size))
        )

        # Test UUID fallback path
        uuid_gen = JobIdGenerator(redis_url=None)
        uuid_ids = await asyncio.gather(
            *(uuid_gen.next_id() for _ in range(batch_size))
        )

        for job_id in list(redis_ids) + list(uuid_ids):
            assert JOB_ID_PATTERN.match(job_id), (
                f"Job ID '{job_id}' does not match expected JOB_* format"
            )
