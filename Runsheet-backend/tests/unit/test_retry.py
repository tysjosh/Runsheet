"""
Unit tests for the retry logic implementation.

These tests verify the retry behavior with exponential backoff:
- Successful calls return immediately without retry
- Failed calls are retried up to max_attempts
- Exponential backoff delays are calculated correctly
- RetryExhaustedException is raised when all retries fail
- Only specified exceptions trigger retries

Validates:
- Requirement 3.4: Retry with exponential backoff starting at 1 second
  with a maximum of 3 attempts
- Requirement 3.6: Log the failure with full context when all retries
  are exhausted
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from resilience.retry import (
    RetryConfig,
    RetryExhaustedException,
    calculate_delay,
    retry,
    retry_async,
)


class TestRetryConfig:
    """Tests for RetryConfig defaults and customization."""
    
    def test_default_config(self):
        """Test that default config matches requirements."""
        config = RetryConfig()
        
        # Requirement 3.4: Maximum of 3 attempts
        assert config.max_attempts == 3
        # Requirement 3.4: Starting at 1 second
        assert config.initial_delay == 1.0
        # Exponential base of 2 for doubling
        assert config.exponential_base == 2.0
        # No max delay by default
        assert config.max_delay is None
        # Retry all exceptions by default
        assert config.retryable_exceptions == (Exception,)
    
    def test_custom_config(self):
        """Test that config can be customized."""
        config = RetryConfig(
            max_attempts=5,
            initial_delay=0.5,
            exponential_base=3.0,
            max_delay=10.0,
            retryable_exceptions=(ConnectionError, TimeoutError)
        )
        
        assert config.max_attempts == 5
        assert config.initial_delay == 0.5
        assert config.exponential_base == 3.0
        assert config.max_delay == 10.0
        assert config.retryable_exceptions == (ConnectionError, TimeoutError)


class TestCalculateDelay:
    """Tests for the calculate_delay function."""
    
    def test_exponential_backoff_default_values(self):
        """
        Test exponential backoff with default values.
        
        Validates Requirement 3.4: Exponential backoff starting at 1 second.
        Expected delays: 1s, 2s, 4s
        """
        # Attempt 0: 1.0 * (2.0 ^ 0) = 1.0
        assert calculate_delay(0, 1.0, 2.0) == 1.0
        # Attempt 1: 1.0 * (2.0 ^ 1) = 2.0
        assert calculate_delay(1, 1.0, 2.0) == 2.0
        # Attempt 2: 1.0 * (2.0 ^ 2) = 4.0
        assert calculate_delay(2, 1.0, 2.0) == 4.0
    
    def test_exponential_backoff_custom_initial_delay(self):
        """Test exponential backoff with custom initial delay."""
        # Attempt 0: 0.5 * (2.0 ^ 0) = 0.5
        assert calculate_delay(0, 0.5, 2.0) == 0.5
        # Attempt 1: 0.5 * (2.0 ^ 1) = 1.0
        assert calculate_delay(1, 0.5, 2.0) == 1.0
        # Attempt 2: 0.5 * (2.0 ^ 2) = 2.0
        assert calculate_delay(2, 0.5, 2.0) == 2.0
    
    def test_exponential_backoff_custom_base(self):
        """Test exponential backoff with custom base."""
        # Attempt 0: 1.0 * (3.0 ^ 0) = 1.0
        assert calculate_delay(0, 1.0, 3.0) == 1.0
        # Attempt 1: 1.0 * (3.0 ^ 1) = 3.0
        assert calculate_delay(1, 1.0, 3.0) == 3.0
        # Attempt 2: 1.0 * (3.0 ^ 2) = 9.0
        assert calculate_delay(2, 1.0, 3.0) == 9.0
    
    def test_max_delay_cap(self):
        """Test that max_delay caps the calculated delay."""
        # Without cap: 1.0 * (2.0 ^ 5) = 32.0
        assert calculate_delay(5, 1.0, 2.0) == 32.0
        # With cap of 10.0
        assert calculate_delay(5, 1.0, 2.0, max_delay=10.0) == 10.0
    
    def test_max_delay_not_applied_when_below(self):
        """Test that max_delay doesn't affect delays below the cap."""
        # 1.0 * (2.0 ^ 1) = 2.0, which is below max_delay of 10.0
        assert calculate_delay(1, 1.0, 2.0, max_delay=10.0) == 2.0


class TestRetryDecorator:
    """Tests for the @retry decorator."""
    
    @pytest.mark.asyncio
    async def test_successful_call_no_retry(self):
        """Test that successful calls return immediately without retry."""
        call_count = 0
        
        @retry()
        async def successful_func():
            nonlocal call_count
            call_count += 1
            return "success"
        
        result = await successful_func()
        
        assert result == "success"
        assert call_count == 1
    
    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Test that failed calls are retried."""
        call_count = 0
        
        @retry(max_attempts=3, initial_delay=0.01)
        async def failing_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Temporary failure")
            return "success"
        
        result = await failing_then_success()
        
        assert result == "success"
        assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_max_attempts_respected(self):
        """
        Test that retry stops after max_attempts.
        
        Validates Requirement 3.4: Maximum of 3 attempts.
        """
        call_count = 0
        
        @retry(max_attempts=3, initial_delay=0.01)
        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise Exception("Always fails")
        
        with pytest.raises(RetryExhaustedException) as exc_info:
            await always_fails()
        
        assert call_count == 3
        assert exc_info.value.attempts == 3
    
    @pytest.mark.asyncio
    async def test_retry_exhausted_exception_contains_last_error(self):
        """Test that RetryExhaustedException contains the last error."""
        @retry(max_attempts=2, initial_delay=0.01)
        async def always_fails():
            raise ValueError("Specific error message")
        
        with pytest.raises(RetryExhaustedException) as exc_info:
            await always_fails()
        
        assert isinstance(exc_info.value.last_exception, ValueError)
        assert "Specific error message" in str(exc_info.value.last_exception)
    
    @pytest.mark.asyncio
    async def test_only_retryable_exceptions_trigger_retry(self):
        """Test that only specified exceptions trigger retry."""
        call_count = 0
        
        @retry(
            max_attempts=3,
            initial_delay=0.01,
            retryable_exceptions=(ConnectionError,)
        )
        async def raises_value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("Not retryable")
        
        with pytest.raises(ValueError, match="Not retryable"):
            await raises_value_error()
        
        # Should only be called once since ValueError is not retryable
        assert call_count == 1
    
    @pytest.mark.asyncio
    async def test_retryable_exception_is_retried(self):
        """Test that retryable exceptions trigger retry."""
        call_count = 0
        
        @retry(
            max_attempts=3,
            initial_delay=0.01,
            retryable_exceptions=(ConnectionError,)
        )
        async def raises_connection_error():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Connection failed")
        
        with pytest.raises(RetryExhaustedException):
            await raises_connection_error()
        
        # Should be called 3 times since ConnectionError is retryable
        assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_decorator_preserves_function_metadata(self):
        """Test that the decorator preserves function name and docstring."""
        @retry()
        async def my_function():
            """My docstring."""
            return "result"
        
        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."
    
    @pytest.mark.asyncio
    async def test_decorator_passes_arguments(self):
        """Test that arguments are passed correctly to the wrapped function."""
        @retry()
        async def func_with_args(a, b, c=None):
            return f"{a}-{b}-{c}"
        
        result = await func_with_args("x", "y", c="z")
        
        assert result == "x-y-z"
    
    @pytest.mark.asyncio
    async def test_config_object_is_used(self):
        """Test that RetryConfig object is used correctly."""
        call_count = 0
        config = RetryConfig(max_attempts=2, initial_delay=0.01)
        
        @retry(config)
        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise Exception("Fails")
        
        with pytest.raises(RetryExhaustedException):
            await always_fails()
        
        assert call_count == 2
    
    @pytest.mark.asyncio
    async def test_individual_params_override_config(self):
        """Test that individual parameters override config values."""
        call_count = 0
        config = RetryConfig(max_attempts=5, initial_delay=1.0)
        
        @retry(config, max_attempts=2, initial_delay=0.01)
        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise Exception("Fails")
        
        with pytest.raises(RetryExhaustedException):
            await always_fails()
        
        # Should use overridden max_attempts=2
        assert call_count == 2
    
    @pytest.mark.asyncio
    async def test_operation_name_in_exception(self):
        """Test that operation_name is included in exception."""
        @retry(max_attempts=1, initial_delay=0.01, operation_name="custom_operation")
        async def always_fails():
            raise Exception("Fails")
        
        with pytest.raises(RetryExhaustedException) as exc_info:
            await always_fails()
        
        assert exc_info.value.operation_name == "custom_operation"
        assert "custom_operation" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_function_name_used_when_no_operation_name(self):
        """Test that function name is used when operation_name not provided."""
        @retry(max_attempts=1, initial_delay=0.01)
        async def my_failing_function():
            raise Exception("Fails")
        
        with pytest.raises(RetryExhaustedException) as exc_info:
            await my_failing_function()
        
        assert "my_failing_function" in str(exc_info.value)


class TestRetryAsync:
    """Tests for the retry_async function."""
    
    @pytest.mark.asyncio
    async def test_successful_call(self):
        """Test that successful calls return immediately."""
        async def success_func():
            return "success"
        
        result = await retry_async(success_func)
        
        assert result == "success"
    
    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Test that failed calls are retried."""
        call_count = 0
        
        async def failing_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Temporary failure")
            return "success"
        
        config = RetryConfig(max_attempts=3, initial_delay=0.01)
        result = await retry_async(failing_then_success, config=config)
        
        assert result == "success"
        assert call_count == 2
    
    @pytest.mark.asyncio
    async def test_passes_arguments(self):
        """Test that arguments are passed to the function."""
        async def func_with_args(a, b, c=None):
            return f"{a}-{b}-{c}"
        
        result = await retry_async(func_with_args, "x", "y", c="z")
        
        assert result == "x-y-z"
    
    @pytest.mark.asyncio
    async def test_raises_retry_exhausted(self):
        """Test that RetryExhaustedException is raised when retries exhausted."""
        async def always_fails():
            raise Exception("Always fails")
        
        config = RetryConfig(max_attempts=2, initial_delay=0.01)
        
        with pytest.raises(RetryExhaustedException) as exc_info:
            await retry_async(always_fails, config=config)
        
        assert exc_info.value.attempts == 2


class TestRetryExhaustedException:
    """Tests for RetryExhaustedException."""
    
    def test_exception_contains_attempts(self):
        """Test that exception contains attempt count."""
        exc = RetryExhaustedException(
            "Operation failed",
            attempts=3,
            last_exception=Exception("Error")
        )
        
        assert exc.attempts == 3
    
    def test_exception_contains_last_exception(self):
        """Test that exception contains the last exception."""
        last_exc = ValueError("Specific error")
        exc = RetryExhaustedException(
            "Operation failed",
            attempts=3,
            last_exception=last_exc
        )
        
        assert exc.last_exception is last_exc
    
    def test_exception_contains_operation_name(self):
        """Test that exception contains operation name."""
        exc = RetryExhaustedException(
            "Operation failed",
            attempts=3,
            last_exception=Exception("Error"),
            operation_name="fetch_data"
        )
        
        assert exc.operation_name == "fetch_data"
    
    def test_exception_message(self):
        """Test that exception has correct message."""
        exc = RetryExhaustedException(
            "Custom message",
            attempts=3,
            last_exception=Exception("Error")
        )
        
        assert str(exc) == "Custom message"


class TestRetryLogging:
    """Tests for retry logging behavior."""
    
    @pytest.mark.asyncio
    async def test_logs_retry_attempts(self):
        """Test that retry attempts are logged."""
        call_count = 0
        
        @retry(max_attempts=3, initial_delay=0.01)
        async def failing_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Temporary failure")
            return "success"
        
        with patch("resilience.retry.logger") as mock_logger:
            await failing_then_success()
            
            # Should have logged 2 warning messages for the 2 retries
            assert mock_logger.warning.call_count == 2
    
    @pytest.mark.asyncio
    async def test_logs_exhausted_error(self):
        """
        Test that exhausted retries are logged as errors.
        
        Validates Requirement 3.6: Log the failure with full context.
        """
        @retry(max_attempts=2, initial_delay=0.01)
        async def always_fails():
            raise Exception("Always fails")
        
        with patch("resilience.retry.logger") as mock_logger:
            with pytest.raises(RetryExhaustedException):
                await always_fails()
            
            # Should have logged 1 warning (first retry) and 1 error (exhausted)
            assert mock_logger.warning.call_count == 1
            assert mock_logger.error.call_count == 1


class TestExponentialBackoffTiming:
    """Tests for exponential backoff timing behavior."""
    
    @pytest.mark.asyncio
    async def test_delays_increase_exponentially(self):
        """
        Test that delays increase exponentially between retries.
        
        Validates Requirement 3.4: Exponential backoff starting at 1 second.
        """
        delays = []
        original_sleep = asyncio.sleep
        
        async def mock_sleep(delay):
            delays.append(delay)
            # Don't actually sleep in tests
            await original_sleep(0.001)
        
        call_count = 0
        
        @retry(max_attempts=4, initial_delay=0.1, exponential_base=2.0)
        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise Exception("Fails")
        
        with patch("resilience.retry.asyncio.sleep", side_effect=mock_sleep):
            with pytest.raises(RetryExhaustedException):
                await always_fails()
        
        # Should have 3 delays (between 4 attempts)
        assert len(delays) == 3
        # Delays should be: 0.1, 0.2, 0.4 (exponential with base 2)
        assert delays[0] == pytest.approx(0.1)
        assert delays[1] == pytest.approx(0.2)
        assert delays[2] == pytest.approx(0.4)
    
    @pytest.mark.asyncio
    async def test_max_delay_caps_backoff(self):
        """Test that max_delay caps the exponential backoff."""
        delays = []
        original_sleep = asyncio.sleep
        
        async def mock_sleep(delay):
            delays.append(delay)
            await original_sleep(0.001)
        
        @retry(max_attempts=5, initial_delay=0.1, exponential_base=2.0, max_delay=0.3)
        async def always_fails():
            raise Exception("Fails")
        
        with patch("resilience.retry.asyncio.sleep", side_effect=mock_sleep):
            with pytest.raises(RetryExhaustedException):
                await always_fails()
        
        # Delays should be: 0.1, 0.2, 0.3 (capped), 0.3 (capped)
        assert len(delays) == 4
        assert delays[0] == pytest.approx(0.1)
        assert delays[1] == pytest.approx(0.2)
        assert delays[2] == pytest.approx(0.3)  # Capped
        assert delays[3] == pytest.approx(0.3)  # Capped
