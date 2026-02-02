"""
Resilience patterns for the Runsheet backend.

This package provides resilience patterns including circuit breakers
and retry logic to handle external service failures gracefully.
"""

from resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenException,
    CircuitState,
)
from resilience.retry import (
    RetryConfig,
    RetryExhaustedException,
    calculate_delay,
    retry,
    retry_async,
)

__all__ = [
    # Circuit breaker
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitOpenException",
    "CircuitState",
    # Retry
    "RetryConfig",
    "RetryExhaustedException",
    "calculate_delay",
    "retry",
    "retry_async",
]
