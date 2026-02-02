"""
Tool invocation logging wrapper for AI agent tools.

This module provides a decorator that wraps AI tools to log their invocations
with timing, input parameters, and success/failure status.

Validates:
- Requirement 5.5: WHEN an AI tool is invoked, THE Telemetry_Service SHALL log
  the tool name, input parameters, execution duration, and success/failure status
- Requirement 5.4: THE Telemetry_Service SHALL record custom metrics for
  tool usage counts
"""

import functools
import time
import logging
from typing import Callable, Any
from strands import tool

logger = logging.getLogger(__name__)


def get_telemetry_service():
    """
    Get the telemetry service instance.
    
    Returns None if telemetry is not initialized, allowing tools to work
    without telemetry in testing scenarios.
    """
    try:
        from telemetry.service import get_telemetry_service as _get_telemetry
        return _get_telemetry()
    except ImportError:
        return None


def logged_tool(func: Callable) -> Callable:
    """
    Decorator that wraps a tool function with invocation logging.
    
    This decorator:
    1. Records the start time before tool execution
    2. Captures input parameters
    3. Executes the tool
    4. Records duration and success/failure status
    5. Logs the invocation via TelemetryService
    6. Records metrics for tool usage
    
    Validates:
    - Requirement 5.5: Log tool name, input parameters, execution duration,
      and success/failure status
    - Requirement 5.4: Record custom metrics for tool usage counts
    
    Args:
        func: The tool function to wrap
        
    Returns:
        Wrapped function with logging capabilities
    """
    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs) -> Any:
        tool_name = func.__name__
        start_time = time.time()
        success = False
        error_message = None
        result = None
        
        # Capture input parameters (sanitize sensitive data)
        input_params = _sanitize_params(kwargs) if kwargs else {}
        if args:
            # Include positional args as well
            input_params["_positional_args"] = [
                _sanitize_value(arg) for arg in args
            ]
        
        try:
            # Execute the tool
            result = await func(*args, **kwargs)
            success = True
            return result
            
        except Exception as e:
            error_message = str(e)
            raise
            
        finally:
            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000
            
            # Log the invocation
            telemetry = get_telemetry_service()
            if telemetry:
                telemetry.log_tool_invocation(
                    tool_name=tool_name,
                    input_params=input_params,
                    duration_ms=duration_ms,
                    success=success,
                    error=error_message
                )
                
                # Record metrics for tool usage
                telemetry.record_metric(
                    name="tool_invocation_duration_ms",
                    value=duration_ms,
                    tags={
                        "tool_name": tool_name,
                        "success": str(success).lower()
                    }
                )
                
                telemetry.record_metric(
                    name="tool_invocation_count",
                    value=1,
                    tags={
                        "tool_name": tool_name,
                        "success": str(success).lower()
                    }
                )
            else:
                # Fallback to basic logging if telemetry not available
                log_level = logging.INFO if success else logging.ERROR
                logger.log(
                    log_level,
                    f"Tool invocation: {tool_name} - "
                    f"duration={duration_ms:.2f}ms, success={success}"
                    + (f", error={error_message}" if error_message else "")
                )
    
    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs) -> Any:
        tool_name = func.__name__
        start_time = time.time()
        success = False
        error_message = None
        result = None
        
        # Capture input parameters
        input_params = _sanitize_params(kwargs) if kwargs else {}
        if args:
            input_params["_positional_args"] = [
                _sanitize_value(arg) for arg in args
            ]
        
        try:
            result = func(*args, **kwargs)
            success = True
            return result
            
        except Exception as e:
            error_message = str(e)
            raise
            
        finally:
            duration_ms = (time.time() - start_time) * 1000
            
            telemetry = get_telemetry_service()
            if telemetry:
                telemetry.log_tool_invocation(
                    tool_name=tool_name,
                    input_params=input_params,
                    duration_ms=duration_ms,
                    success=success,
                    error=error_message
                )
                
                telemetry.record_metric(
                    name="tool_invocation_duration_ms",
                    value=duration_ms,
                    tags={
                        "tool_name": tool_name,
                        "success": str(success).lower()
                    }
                )
                
                telemetry.record_metric(
                    name="tool_invocation_count",
                    value=1,
                    tags={
                        "tool_name": tool_name,
                        "success": str(success).lower()
                    }
                )
            else:
                log_level = logging.INFO if success else logging.ERROR
                logger.log(
                    log_level,
                    f"Tool invocation: {tool_name} - "
                    f"duration={duration_ms:.2f}ms, success={success}"
                    + (f", error={error_message}" if error_message else "")
                )
    
    # Return appropriate wrapper based on function type
    import asyncio
    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


def _sanitize_params(params: dict) -> dict:
    """
    Sanitize parameters to remove or mask sensitive data.
    
    Args:
        params: Dictionary of parameters
        
    Returns:
        Sanitized dictionary safe for logging
    """
    sanitized = {}
    sensitive_keys = {'password', 'secret', 'token', 'api_key', 'credential'}
    
    for key, value in params.items():
        key_lower = key.lower()
        if any(sensitive in key_lower for sensitive in sensitive_keys):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = _sanitize_value(value)
    
    return sanitized


def _sanitize_value(value: Any) -> Any:
    """
    Sanitize a single value for logging.
    
    Truncates long strings and converts complex objects to string representation.
    
    Args:
        value: Value to sanitize
        
    Returns:
        Sanitized value safe for logging
    """
    if isinstance(value, str):
        # Truncate long strings
        if len(value) > 500:
            return value[:500] + "...[truncated]"
        return value
    elif isinstance(value, (int, float, bool, type(None))):
        return value
    elif isinstance(value, (list, tuple)):
        # Limit list size and sanitize elements
        if len(value) > 10:
            return [_sanitize_value(v) for v in value[:10]] + ["...[truncated]"]
        return [_sanitize_value(v) for v in value]
    elif isinstance(value, dict):
        return _sanitize_params(value)
    else:
        # Convert complex objects to string representation
        str_repr = str(value)
        if len(str_repr) > 200:
            return str_repr[:200] + "...[truncated]"
        return str_repr


def create_logged_tool(func: Callable) -> Callable:
    """
    Create a logged tool by combining the @tool decorator with logging.
    
    This function applies both the Strands @tool decorator and the
    logging wrapper to create a fully instrumented tool.
    
    Usage:
        @create_logged_tool
        async def my_tool(query: str) -> str:
            ...
    
    Or for existing tools:
        logged_search = create_logged_tool(search_fleet_data)
    
    Args:
        func: The function to convert to a logged tool
        
    Returns:
        A tool function with logging capabilities
    """
    # Apply logging wrapper first, then tool decorator
    logged_func = logged_tool(func)
    return tool(logged_func)
