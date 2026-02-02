"""
Error handling module for the Runsheet backend.

This module provides structured error handling with:
- ErrorCode enum for standardized error codes
- AppException class for application-specific exceptions
- Error response models for consistent API responses
- Exception handlers for FastAPI integration
"""

from errors.codes import ErrorCode
from errors.exceptions import AppException
from errors.handlers import (
    ErrorResponse,
    handle_app_exception,
    handle_unexpected_exception,
    register_exception_handlers,
)

__all__ = [
    "ErrorCode",
    "AppException",
    "ErrorResponse",
    "handle_app_exception",
    "handle_unexpected_exception",
    "register_exception_handlers",
]
