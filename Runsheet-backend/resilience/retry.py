"""
Retry logic with exponential backoff for the Runsheet backend.

This module implements retry functionality with exponential backoff
to handle transient failures when calling external services.

Validates:
- Requirement 3.4: Retry with exponential backoff starting at 1 second
  with a maximum of 3 attempts
- Requirement 3.6: Log the failure with full context when all retries
  are exhausted
"""

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple, Type, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryConfig:
    """
    Configuration for retry behavior.
    
    Attributes:
        max_attempts: Maximum number of retry attempts. Default is 3
            per Requirement 3.4.
        initial_delay: Initial delay before first retry in seconds.
            Default is 1.0 per Requirement 3.4.
        exponential_base: Base for exponential backoff calculation.
            Default is 2.0 (delays: 1s, 2s, 4s).
        max_delay: Maximum delay between retries in seconds.
            Default is None (no maximum).
        retryable_exceptions: Tuple of exception types that should
            trigger a retry. Default is (Exception,) to retry all.
    """
    max_attempts: int = 3
    initial_delay: float = 1.0
    exponential_base: float = 2.0
    max_delay: Optional[float] = None
    retryable_exceptions: Tuple[Type[Exception], ...] = field(
        default_factory=lambda: (Exception,)
    )


class RetryExhaustedException(Exception):
    """
    Exception raised when all retry attempts have been exhausted.
    
    This exception wraps the last exception that caused the retry
    to fail, providing context about the retry attempts.
    
    Validates: Requirement 3.6 - Return appropriate error response
    when all retries are exhausted.
    """
    
    def __init__(
        self,
        message: str,
        attempts: int,
        last_exception: Exception,
        operation_name: Optional[str] = None
    ):
        """
        Initialize a RetryExhaustedException.
        
        Args:
            message: Human-readable error message
            attempts: Number of attempts made
            last_exception: The last exception that caused failure
            operation_name: Optional name of the operation that failed
        """
        self.attempts = attempts
        self.last_exception = last_exception
        self.operation_name = operation_name
        super().__init__(message)


def calculate_delay(
    attempt: int,
    initial_delay: float,
    exponential_base: float,
    max_delay: Optional[float] = None
) -> float:
    """
    Calculate the delay for a given retry attempt using exponential backoff.
    
    The delay is calculated as: initial_delay * (exponential_base ^ attempt)
    
    For default values (initial_delay=1.0, exponential_base=2.0):
    - Attempt 0: 1.0 * (2.0 ^ 0) = 1.0 second
    - Attempt 1: 1.0 * (2.0 ^ 1) = 2.0 seconds
    - Attempt 2: 1.0 * (2.0 ^ 2) = 4.0 seconds
    
    Args:
        attempt: The current attempt number (0-indexed)
        initial_delay: The initial delay in seconds
        exponential_base: The base for exponential calculation
        max_delay: Optional maximum delay cap
        
    Returns:
        The calculated delay in seconds
        
    Validates: Requirement 3.4 - Exponential backoff starting at 1 second
    """
    delay = initial_delay * (exponential_base ** attempt)
    
    if max_delay is not None:
        delay = min(delay, max_delay)
    
    return delay


def retry(
    config: Optional[RetryConfig] = None,
    *,
    max_attempts: Optional[int] = None,
    initial_delay: Optional[float] = None,
    exponential_base: Optional[float] = None,
    max_delay: Optional[float] = None,
    retryable_exceptions: Optional[Tuple[Type[Exception], ...]] = None,
    operation_name: Optional[str] = None
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator that adds retry logic with exponential backoff to async functions.
    
    This decorator wraps an async function and automatically retries it
    when specified exceptions occur, using exponential backoff between
    retry attempts.
    
    The decorator can be configured either by passing a RetryConfig object
    or by specifying individual parameters. Individual parameters take
    precedence over the config object.
    
    Example usage:
        # Using default configuration (3 attempts, 1s initial delay)
        @retry()
        async def fetch_data():
            return await external_service.get_data()
        
        # Using custom configuration
        @retry(max_attempts=5, initial_delay=0.5)
        async def fetch_data():
            return await external_service.get_data()
        
        # Using RetryConfig object
        config = RetryConfig(max_attempts=5, initial_delay=0.5)
        @retry(config)
        async def fetch_data():
            return await external_service.get_data()
        
        # Specifying retryable exceptions
        @retry(retryable_exceptions=(ConnectionError, TimeoutError))
        async def fetch_data():
            return await external_service.get_data()
    
    Args:
        config: Optional RetryConfig object with retry settings
        max_attempts: Maximum number of retry attempts (overrides config)
        initial_delay: Initial delay in seconds (overrides config)
        exponential_base: Base for exponential backoff (overrides config)
        max_delay: Maximum delay cap in seconds (overrides config)
        retryable_exceptions: Tuple of exception types to retry (overrides config)
        operation_name: Optional name for logging purposes
        
    Returns:
        A decorator function that wraps async functions with retry logic
        
    Validates:
    - Requirement 3.4: Retry with exponential backoff starting at 1 second
      with a maximum of 3 attempts
    - Requirement 3.6: Log the failure with full context when all retries
      are exhausted
    """
    # Build effective configuration
    base_config = config or RetryConfig()
    
    effective_max_attempts = max_attempts if max_attempts is not None else base_config.max_attempts
    effective_initial_delay = initial_delay if initial_delay is not None else base_config.initial_delay
    effective_exponential_base = exponential_base if exponential_base is not None else base_config.exponential_base
    effective_max_delay = max_delay if max_delay is not None else base_config.max_delay
    effective_retryable_exceptions = retryable_exceptions if retryable_exceptions is not None else base_config.retryable_exceptions
    
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            op_name = operation_name or func.__name__
            last_exception: Optional[Exception] = None
            
            for attempt in range(effective_max_attempts):
                try:
                    return await func(*args, **kwargs)
                except effective_retryable_exceptions as e:
                    last_exception = e
                    
                    # Check if this was the last attempt
                    if attempt == effective_max_attempts - 1:
                        # Log failure with full context
                        logger.error(
                            "Retry exhausted for operation '%s' after %d attempts. "
                            "Last error: %s",
                            op_name,
                            effective_max_attempts,
                            str(e),
                            exc_info=True,
                            extra={
                                "operation": op_name,
                                "attempts": effective_max_attempts,
                                "last_error": str(e),
                                "error_type": type(e).__name__
                            }
                        )
                        raise RetryExhaustedException(
                            f"Operation '{op_name}' failed after {effective_max_attempts} attempts",
                            attempts=effective_max_attempts,
                            last_exception=e,
                            operation_name=op_name
                        ) from e
                    
                    # Calculate delay for next retry
                    delay = calculate_delay(
                        attempt,
                        effective_initial_delay,
                        effective_exponential_base,
                        effective_max_delay
                    )
                    
                    # Log retry attempt
                    logger.warning(
                        "Retry attempt %d/%d for operation '%s' failed with %s: %s. "
                        "Retrying in %.2f seconds...",
                        attempt + 1,
                        effective_max_attempts,
                        op_name,
                        type(e).__name__,
                        str(e),
                        delay,
                        extra={
                            "operation": op_name,
                            "attempt": attempt + 1,
                            "max_attempts": effective_max_attempts,
                            "delay_seconds": delay,
                            "error_type": type(e).__name__,
                            "error_message": str(e)
                        }
                    )
                    
                    # Wait before retrying
                    await asyncio.sleep(delay)
            
            # This should never be reached, but just in case
            raise RetryExhaustedException(
                f"Operation '{op_name}' failed after {effective_max_attempts} attempts",
                attempts=effective_max_attempts,
                last_exception=last_exception or Exception("Unknown error"),
                operation_name=op_name
            )
        
        return wrapper
    
    return decorator


async def retry_async(
    func: Callable[..., T],
    *args: Any,
    config: Optional[RetryConfig] = None,
    operation_name: Optional[str] = None,
    **kwargs: Any
) -> T:
    """
    Execute an async function with retry logic.
    
    This is a functional alternative to the @retry decorator for cases
    where you need to apply retry logic dynamically or to functions
    you don't control.
    
    Example usage:
        result = await retry_async(
            external_service.get_data,
            "param1",
            config=RetryConfig(max_attempts=5),
            operation_name="fetch_external_data"
        )
    
    Args:
        func: The async function to execute
        *args: Positional arguments to pass to the function
        config: Optional RetryConfig object with retry settings
        operation_name: Optional name for logging purposes
        **kwargs: Keyword arguments to pass to the function
        
    Returns:
        The result of the function call
        
    Raises:
        RetryExhaustedException: When all retry attempts are exhausted
        
    Validates:
    - Requirement 3.4: Retry with exponential backoff starting at 1 second
      with a maximum of 3 attempts
    """
    effective_config = config or RetryConfig()
    op_name = operation_name or func.__name__
    last_exception: Optional[Exception] = None
    
    for attempt in range(effective_config.max_attempts):
        try:
            return await func(*args, **kwargs)
        except effective_config.retryable_exceptions as e:
            last_exception = e
            
            # Check if this was the last attempt
            if attempt == effective_config.max_attempts - 1:
                logger.error(
                    "Retry exhausted for operation '%s' after %d attempts. "
                    "Last error: %s",
                    op_name,
                    effective_config.max_attempts,
                    str(e),
                    exc_info=True,
                    extra={
                        "operation": op_name,
                        "attempts": effective_config.max_attempts,
                        "last_error": str(e),
                        "error_type": type(e).__name__
                    }
                )
                raise RetryExhaustedException(
                    f"Operation '{op_name}' failed after {effective_config.max_attempts} attempts",
                    attempts=effective_config.max_attempts,
                    last_exception=e,
                    operation_name=op_name
                ) from e
            
            # Calculate delay for next retry
            delay = calculate_delay(
                attempt,
                effective_config.initial_delay,
                effective_config.exponential_base,
                effective_config.max_delay
            )
            
            logger.warning(
                "Retry attempt %d/%d for operation '%s' failed with %s: %s. "
                "Retrying in %.2f seconds...",
                attempt + 1,
                effective_config.max_attempts,
                op_name,
                type(e).__name__,
                str(e),
                delay,
                extra={
                    "operation": op_name,
                    "attempt": attempt + 1,
                    "max_attempts": effective_config.max_attempts,
                    "delay_seconds": delay,
                    "error_type": type(e).__name__,
                    "error_message": str(e)
                }
            )
            
            await asyncio.sleep(delay)
    
    # This should never be reached
    raise RetryExhaustedException(
        f"Operation '{op_name}' failed after {effective_config.max_attempts} attempts",
        attempts=effective_config.max_attempts,
        last_exception=last_exception or Exception("Unknown error"),
        operation_name=op_name
    )
