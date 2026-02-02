"""
Unit tests for the circuit breaker implementation.

These tests verify the circuit breaker state machine behavior:
- CLOSED -> OPEN after failure threshold
- OPEN -> HALF_OPEN after recovery timeout
- HALF_OPEN -> CLOSED on success
- HALF_OPEN -> OPEN on failure

Validates:
- Requirement 3.1: Open after 3 consecutive failures and prevent further
  requests for 30 seconds
- Requirement 3.2: Return service unavailable response immediately when
  circuit is open
- Requirement 3.3: Allow a single test request when half-open to determine
  if service has recovered
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenException,
    CircuitState,
)


class TestCircuitBreakerConfig:
    """Tests for CircuitBreakerConfig defaults and customization."""
    
    def test_default_config(self):
        """Test that default config matches requirements."""
        config = CircuitBreakerConfig()
        
        # Requirement 3.1: 3 consecutive failures
        assert config.failure_threshold == 3
        # Requirement 3.1: 30 seconds recovery timeout
        assert config.recovery_timeout == timedelta(seconds=30)
        # Requirement 3.3: Single test request in half-open
        assert config.half_open_max_calls == 1
    
    def test_custom_config(self):
        """Test that config can be customized."""
        config = CircuitBreakerConfig(
            failure_threshold=5,
            recovery_timeout=timedelta(seconds=60),
            half_open_max_calls=2
        )
        
        assert config.failure_threshold == 5
        assert config.recovery_timeout == timedelta(seconds=60)
        assert config.half_open_max_calls == 2


class TestCircuitBreakerInitialization:
    """Tests for CircuitBreaker initialization."""
    
    def test_initial_state_is_closed(self):
        """Test that circuit breaker starts in CLOSED state."""
        breaker = CircuitBreaker("test")
        
        assert breaker.state == CircuitState.CLOSED
        assert breaker.get_state() == CircuitState.CLOSED
        assert breaker.failure_count == 0
        assert breaker.last_failure_time is None
    
    def test_name_is_set(self):
        """Test that circuit breaker name is set correctly."""
        breaker = CircuitBreaker("elasticsearch")
        
        assert breaker.name == "elasticsearch"
    
    def test_default_config_used_when_none_provided(self):
        """Test that default config is used when none provided."""
        breaker = CircuitBreaker("test")
        
        assert breaker.config.failure_threshold == 3
        assert breaker.config.recovery_timeout == timedelta(seconds=30)
    
    def test_custom_config_is_used(self):
        """Test that custom config is used when provided."""
        config = CircuitBreakerConfig(failure_threshold=5)
        breaker = CircuitBreaker("test", config)
        
        assert breaker.config.failure_threshold == 5


class TestCircuitBreakerClosedState:
    """Tests for circuit breaker behavior in CLOSED state."""
    
    @pytest.mark.asyncio
    async def test_successful_call_in_closed_state(self):
        """Test that successful calls pass through in CLOSED state."""
        breaker = CircuitBreaker("test")
        mock_func = AsyncMock(return_value="success")
        
        result = await breaker.execute(mock_func, "arg1", kwarg1="value1")
        
        assert result == "success"
        mock_func.assert_called_once_with("arg1", kwarg1="value1")
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
    
    @pytest.mark.asyncio
    async def test_failure_increments_count(self):
        """Test that failures increment the failure count."""
        breaker = CircuitBreaker("test")
        mock_func = AsyncMock(side_effect=Exception("Service error"))
        
        with pytest.raises(Exception, match="Service error"):
            await breaker.execute(mock_func)
        
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 1
        assert breaker.last_failure_time is not None
    
    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        """Test that success resets the failure count."""
        breaker = CircuitBreaker("test")
        failing_func = AsyncMock(side_effect=Exception("Error"))
        success_func = AsyncMock(return_value="success")
        
        # Cause some failures
        with pytest.raises(Exception):
            await breaker.execute(failing_func)
        with pytest.raises(Exception):
            await breaker.execute(failing_func)
        
        assert breaker.failure_count == 2
        
        # Success should reset count
        await breaker.execute(success_func)
        
        assert breaker.failure_count == 0
        assert breaker.state == CircuitState.CLOSED
    
    @pytest.mark.asyncio
    async def test_opens_after_threshold_failures(self):
        """
        Test that circuit opens after failure threshold is reached.
        
        Validates Requirement 3.1: Open after 3 consecutive failures.
        """
        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker("test", config)
        mock_func = AsyncMock(side_effect=Exception("Service error"))
        
        # Cause 3 consecutive failures
        for i in range(3):
            with pytest.raises(Exception, match="Service error"):
                await breaker.execute(mock_func)
        
        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 3


class TestCircuitBreakerOpenState:
    """Tests for circuit breaker behavior in OPEN state."""
    
    @pytest.mark.asyncio
    async def test_rejects_calls_when_open(self):
        """
        Test that calls are rejected immediately when circuit is open.
        
        Validates Requirement 3.2: Return service unavailable response
        immediately when circuit is open.
        """
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=timedelta(seconds=30)
        )
        breaker = CircuitBreaker("test", config)
        mock_func = AsyncMock(side_effect=Exception("Error"))
        
        # Open the circuit
        with pytest.raises(Exception):
            await breaker.execute(mock_func)
        
        assert breaker.state == CircuitState.OPEN
        
        # Next call should be rejected without calling the function
        mock_func.reset_mock()
        with pytest.raises(CircuitOpenException) as exc_info:
            await breaker.execute(mock_func)
        
        assert exc_info.value.circuit_name == "test"
        mock_func.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_circuit_open_exception_includes_retry_time(self):
        """Test that CircuitOpenException includes time until retry."""
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=timedelta(seconds=30)
        )
        breaker = CircuitBreaker("test", config)
        mock_func = AsyncMock(side_effect=Exception("Error"))
        
        # Open the circuit
        with pytest.raises(Exception):
            await breaker.execute(mock_func)
        
        # Check exception includes retry time
        with pytest.raises(CircuitOpenException) as exc_info:
            await breaker.execute(mock_func)
        
        assert exc_info.value.time_until_retry is not None
        assert exc_info.value.time_until_retry.total_seconds() > 0
        assert exc_info.value.time_until_retry.total_seconds() <= 30
    
    @pytest.mark.asyncio
    async def test_transitions_to_half_open_after_timeout(self):
        """
        Test that circuit transitions to HALF_OPEN after recovery timeout.
        
        Validates Requirement 3.1: Prevent further requests for 30 seconds.
        """
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=timedelta(seconds=1)
        )
        breaker = CircuitBreaker("test", config)
        failing_func = AsyncMock(side_effect=Exception("Error"))
        success_func = AsyncMock(return_value="success")
        
        # Open the circuit
        with pytest.raises(Exception):
            await breaker.execute(failing_func)
        
        assert breaker.state == CircuitState.OPEN
        
        # Wait for recovery timeout
        await asyncio.sleep(1.1)
        
        # Next call should transition to HALF_OPEN and execute
        result = await breaker.execute(success_func)
        
        assert result == "success"
        # After success in HALF_OPEN, should be CLOSED
        assert breaker.state == CircuitState.CLOSED


class TestCircuitBreakerHalfOpenState:
    """Tests for circuit breaker behavior in HALF_OPEN state."""
    
    @pytest.mark.asyncio
    async def test_allows_single_test_request(self):
        """
        Test that HALF_OPEN state allows a single test request.
        
        Validates Requirement 3.3: Allow a single test request when half-open.
        """
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=timedelta(seconds=0),  # Immediate recovery for testing
            half_open_max_calls=1
        )
        breaker = CircuitBreaker("test", config)
        failing_func = AsyncMock(side_effect=Exception("Error"))
        
        # Open the circuit
        with pytest.raises(Exception):
            await breaker.execute(failing_func)
        
        # Should transition to HALF_OPEN on next call attempt
        # and allow one test call
        success_func = AsyncMock(return_value="success")
        result = await breaker.execute(success_func)
        
        assert result == "success"
        success_func.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_closes_on_successful_test_request(self):
        """
        Test that circuit closes on successful test request in HALF_OPEN.
        
        Validates Requirement 3.3: Determine if service has recovered.
        """
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=timedelta(seconds=0)
        )
        breaker = CircuitBreaker("test", config)
        
        # Open the circuit
        with pytest.raises(Exception):
            await breaker.execute(AsyncMock(side_effect=Exception("Error")))
        
        assert breaker.state == CircuitState.OPEN
        
        # Successful test request should close the circuit
        result = await breaker.execute(AsyncMock(return_value="recovered"))
        
        assert result == "recovered"
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
    
    @pytest.mark.asyncio
    async def test_reopens_on_failed_test_request(self):
        """
        Test that circuit reopens on failed test request in HALF_OPEN.
        
        Validates Requirement 3.3: Determine if service has recovered.
        """
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=timedelta(seconds=0)
        )
        breaker = CircuitBreaker("test", config)
        failing_func = AsyncMock(side_effect=Exception("Still failing"))
        
        # Open the circuit
        with pytest.raises(Exception):
            await breaker.execute(failing_func)
        
        # Failed test request should reopen the circuit
        with pytest.raises(Exception, match="Still failing"):
            await breaker.execute(failing_func)
        
        assert breaker.state == CircuitState.OPEN


class TestCircuitBreakerReset:
    """Tests for manual circuit breaker reset."""
    
    @pytest.mark.asyncio
    async def test_manual_reset_closes_circuit(self):
        """Test that manual reset closes an open circuit."""
        config = CircuitBreakerConfig(failure_threshold=1)
        breaker = CircuitBreaker("test", config)
        
        # Open the circuit
        with pytest.raises(Exception):
            await breaker.execute(AsyncMock(side_effect=Exception("Error")))
        
        assert breaker.state == CircuitState.OPEN
        
        # Manual reset
        breaker.reset()
        
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
        assert breaker.last_failure_time is None
    
    @pytest.mark.asyncio
    async def test_reset_allows_new_calls(self):
        """Test that reset allows new calls to pass through."""
        config = CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=timedelta(seconds=60)
        )
        breaker = CircuitBreaker("test", config)
        
        # Open the circuit
        with pytest.raises(Exception):
            await breaker.execute(AsyncMock(side_effect=Exception("Error")))
        
        # Verify circuit is open
        with pytest.raises(CircuitOpenException):
            await breaker.execute(AsyncMock())
        
        # Reset and verify calls work
        breaker.reset()
        
        success_func = AsyncMock(return_value="success")
        result = await breaker.execute(success_func)
        
        assert result == "success"
        success_func.assert_called_once()


class TestCircuitOpenException:
    """Tests for CircuitOpenException."""
    
    def test_exception_message_includes_circuit_name(self):
        """Test that exception message includes circuit name."""
        exc = CircuitOpenException("elasticsearch")
        
        assert "elasticsearch" in str(exc)
        assert exc.circuit_name == "elasticsearch"
    
    def test_exception_message_includes_retry_time(self):
        """Test that exception message includes retry time when provided."""
        exc = CircuitOpenException("test", timedelta(seconds=25))
        
        assert "25 seconds" in str(exc)
        assert exc.time_until_retry == timedelta(seconds=25)
    
    def test_exception_without_retry_time(self):
        """Test exception when no retry time is provided."""
        exc = CircuitOpenException("test")
        
        assert exc.time_until_retry is None
        assert "retry" not in str(exc)


class TestCircuitBreakerRepr:
    """Tests for CircuitBreaker string representation."""
    
    def test_repr_includes_name_and_state(self):
        """Test that repr includes name and state."""
        breaker = CircuitBreaker("elasticsearch")
        
        repr_str = repr(breaker)
        
        assert "elasticsearch" in repr_str
        assert "closed" in repr_str
        assert "failure_count=0" in repr_str


class TestCircuitBreakerConcurrency:
    """Tests for circuit breaker thread safety."""
    
    @pytest.mark.asyncio
    async def test_concurrent_calls_in_closed_state(self):
        """Test that concurrent calls work correctly in CLOSED state."""
        breaker = CircuitBreaker("test")
        call_count = 0
        
        async def slow_func():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)
            return "success"
        
        # Execute multiple concurrent calls
        results = await asyncio.gather(
            breaker.execute(slow_func),
            breaker.execute(slow_func),
            breaker.execute(slow_func)
        )
        
        assert all(r == "success" for r in results)
        assert call_count == 3
        assert breaker.state == CircuitState.CLOSED
    
    @pytest.mark.asyncio
    async def test_concurrent_failures_open_circuit_once(self):
        """Test that concurrent failures properly open the circuit."""
        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker("test", config)
        
        async def failing_func():
            await asyncio.sleep(0.05)
            raise Exception("Error")
        
        # Execute concurrent failing calls
        tasks = [breaker.execute(failing_func) for _ in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # All should have raised exceptions
        assert all(isinstance(r, Exception) for r in results)
        # Circuit should be open
        assert breaker.state == CircuitState.OPEN
