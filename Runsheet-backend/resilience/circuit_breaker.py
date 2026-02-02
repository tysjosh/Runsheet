"""
Circuit breaker implementation for the Runsheet backend.

This module implements the circuit breaker pattern to prevent cascading
failures when external services are unavailable. The circuit breaker
has three states:
- CLOSED: Normal operation, requests pass through
- OPEN: Service is failing, requests are rejected immediately
- HALF_OPEN: Testing if service has recovered

Validates:
- Requirement 3.1: Open after 3 consecutive failures and prevent further
  requests for 30 seconds
- Requirement 3.2: Return service unavailable response immediately when
  circuit is open
- Requirement 3.3: Allow a single test request when half-open to determine
  if service has recovered
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")


class CircuitState(Enum):
    """
    Enumeration of circuit breaker states.
    
    The circuit breaker follows this state machine:
    - CLOSED -> OPEN: After failure_threshold consecutive failures
    - OPEN -> HALF_OPEN: After recovery_timeout has elapsed
    - HALF_OPEN -> CLOSED: On successful test request
    - HALF_OPEN -> OPEN: On failed test request
    """
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """
    Configuration for a circuit breaker.
    
    Attributes:
        failure_threshold: Number of consecutive failures before opening
            the circuit. Default is 3 per Requirement 3.1.
        recovery_timeout: Time to wait before attempting recovery.
            Default is 30 seconds per Requirement 3.1.
        half_open_max_calls: Maximum number of test calls allowed in
            half-open state. Default is 1 per Requirement 3.3.
    """
    failure_threshold: int = 3
    recovery_timeout: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    half_open_max_calls: int = 1


class CircuitOpenException(Exception):
    """
    Exception raised when a circuit breaker is open.
    
    This exception indicates that the circuit breaker has tripped and
    is preventing requests from reaching the underlying service.
    
    Validates: Requirement 3.2 - Return service unavailable response
    immediately when circuit is open.
    """
    
    def __init__(self, circuit_name: str, time_until_retry: Optional[timedelta] = None):
        """
        Initialize a CircuitOpenException.
        
        Args:
            circuit_name: The name of the circuit breaker that is open
            time_until_retry: Optional time until the circuit will attempt
                to transition to half-open state
        """
        self.circuit_name = circuit_name
        self.time_until_retry = time_until_retry
        
        message = f"Circuit breaker '{circuit_name}' is open"
        if time_until_retry is not None:
            seconds = int(time_until_retry.total_seconds())
            message += f", retry in {seconds} seconds"
        
        super().__init__(message)


class CircuitBreaker:
    """
    Circuit breaker implementation for protecting external service calls.
    
    The circuit breaker monitors calls to external services and prevents
    cascading failures by "opening" the circuit when too many failures
    occur. While open, all calls fail immediately without attempting
    the underlying operation.
    
    State Machine:
    - CLOSED: Normal operation. Failures increment the failure counter.
      After failure_threshold consecutive failures, transitions to OPEN.
    - OPEN: All calls fail immediately with CircuitOpenException.
      After recovery_timeout, transitions to HALF_OPEN.
    - HALF_OPEN: Allows a limited number of test calls. On success,
      transitions to CLOSED. On failure, transitions back to OPEN.
    
    Example:
        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker("elasticsearch", config)
        
        try:
            result = await breaker.execute(es_client.search, index="trucks")
        except CircuitOpenException:
            # Handle circuit open - return cached data or error
            pass
    
    Validates:
    - Requirement 3.1: Open after 3 consecutive failures
    - Requirement 3.2: Return service unavailable immediately when open
    - Requirement 3.3: Allow single test request when half-open
    """
    
    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        """
        Initialize a circuit breaker.
        
        Args:
            name: A descriptive name for this circuit breaker (e.g., "elasticsearch")
            config: Configuration options. Uses defaults if not provided.
        """
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[datetime] = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()
    
    @property
    def state(self) -> CircuitState:
        """Get the current circuit state."""
        return self._state
    
    @property
    def failure_count(self) -> int:
        """Get the current failure count."""
        return self._failure_count
    
    @property
    def last_failure_time(self) -> Optional[datetime]:
        """Get the time of the last failure."""
        return self._last_failure_time
    
    def get_state(self) -> CircuitState:
        """
        Get the current circuit state.
        
        Returns:
            The current CircuitState (CLOSED, OPEN, or HALF_OPEN)
        """
        return self._state
    
    def _should_attempt_reset(self) -> bool:
        """
        Check if the circuit should attempt to reset from OPEN to HALF_OPEN.
        
        Returns:
            True if recovery_timeout has elapsed since the last failure
        """
        if self._last_failure_time is None:
            return True
        
        elapsed = datetime.utcnow() - self._last_failure_time
        return elapsed >= self.config.recovery_timeout
    
    def _get_time_until_retry(self) -> Optional[timedelta]:
        """
        Calculate the time remaining until the circuit will attempt reset.
        
        Returns:
            Time remaining until retry, or None if ready to retry
        """
        if self._last_failure_time is None:
            return None
        
        elapsed = datetime.utcnow() - self._last_failure_time
        remaining = self.config.recovery_timeout - elapsed
        
        if remaining.total_seconds() <= 0:
            return None
        
        return remaining
    
    def _on_success(self) -> None:
        """
        Handle a successful call.
        
        In CLOSED state: Reset failure count
        In HALF_OPEN state: Transition to CLOSED
        """
        if self._state == CircuitState.HALF_OPEN:
            # Successful test call - close the circuit
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0
        elif self._state == CircuitState.CLOSED:
            # Reset failure count on success
            self._failure_count = 0
    
    def _on_failure(self) -> None:
        """
        Handle a failed call.
        
        In CLOSED state: Increment failure count, open if threshold reached
        In HALF_OPEN state: Transition back to OPEN
        """
        self._last_failure_time = datetime.utcnow()
        
        if self._state == CircuitState.HALF_OPEN:
            # Failed test call - reopen the circuit
            self._state = CircuitState.OPEN
            self._half_open_calls = 0
        elif self._state == CircuitState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self.config.failure_threshold:
                # Threshold reached - open the circuit
                self._state = CircuitState.OPEN
    
    async def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any
    ) -> Any:
        """
        Execute a function with circuit breaker protection.
        
        This method wraps the given function call with circuit breaker
        logic. If the circuit is open, it raises CircuitOpenException
        immediately without calling the function. If the circuit is
        closed or half-open, it attempts the call and updates the
        circuit state based on the result.
        
        Args:
            func: The async function to execute
            *args: Positional arguments to pass to the function
            **kwargs: Keyword arguments to pass to the function
            
        Returns:
            The result of the function call
            
        Raises:
            CircuitOpenException: If the circuit is open and not ready
                to attempt recovery
            Exception: Any exception raised by the underlying function
        
        Validates:
        - Requirement 3.2: Return service unavailable immediately when open
        - Requirement 3.3: Allow single test request when half-open
        """
        async with self._lock:
            # Check if we should transition from OPEN to HALF_OPEN
            if self._state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                else:
                    # Circuit is open and not ready to retry
                    raise CircuitOpenException(
                        self.name,
                        self._get_time_until_retry()
                    )
            
            # Check if we've exceeded half-open call limit
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.config.half_open_max_calls:
                    # Already have a test call in progress, reject this one
                    raise CircuitOpenException(
                        self.name,
                        self._get_time_until_retry()
                    )
                self._half_open_calls += 1
        
        # Execute the function outside the lock
        try:
            result = await func(*args, **kwargs)
            async with self._lock:
                self._on_success()
            return result
        except Exception as e:
            async with self._lock:
                self._on_failure()
            raise
    
    def reset(self) -> None:
        """
        Manually reset the circuit breaker to closed state.
        
        This method can be used to manually recover the circuit breaker,
        for example after a known service recovery or during testing.
        """
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None
        self._half_open_calls = 0
    
    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self._state.value}, "
            f"failure_count={self._failure_count})"
        )
