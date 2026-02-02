"""
Telemetry module for structured logging and observability.

This module provides:
- JSONFormatter for structured JSON log output
- TelemetryService for centralized logging and metrics
- Integration with OpenTelemetry for distributed tracing

Validates: Requirement 5.1 - Output all logs in JSON format with timestamp,
level, message, and context fields.
"""

from telemetry.service import (
    JSONFormatter,
    TelemetryService,
    get_telemetry_service,
    set_request_id,
    get_request_id,
)

__all__ = [
    "JSONFormatter",
    "TelemetryService",
    "get_telemetry_service",
    "set_request_id",
    "get_request_id",
]
