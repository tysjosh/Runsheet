"""
Telemetry service for structured logging and observability.

This module provides structured JSON logging with request correlation,
OpenTelemetry integration for distributed tracing, and custom metrics recording.

Validates: Requirement 5.1 - THE Telemetry_Service SHALL output all logs in JSON
format with timestamp, level, message, and context fields.
"""

import logging
import json
import sys
from datetime import datetime
from typing import Any, Optional, Dict
from contextvars import ContextVar

# Import request_id_var from middleware to maintain consistency
# This allows the telemetry service to access the same request_id context
try:
    from middleware.request_id import request_id_var
except ImportError:
    # Fallback if middleware not available (e.g., during testing)
    request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class JSONFormatter(logging.Formatter):
    """
    Custom log formatter that outputs logs in JSON format.
    
    Each log entry contains:
    - timestamp: ISO 8601 formatted UTC timestamp
    - level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    - message: The log message
    - logger: Name of the logger that produced the entry
    - request_id: Correlation ID for request tracing
    
    Additional fields can be included via the 'extra_data' attribute
    on the log record.
    
    Validates: Requirement 5.1 - Output all logs in JSON format with
    timestamp, level, message, and context fields.
    """
    
    def format(self, record: logging.LogRecord) -> str:
        """
        Format a log record as a JSON string.
        
        Args:
            record: The log record to format
            
        Returns:
            JSON-formatted string containing the log entry
        """
        # Build the base log data structure
        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": request_id_var.get(""),
        }
        
        # Add module and function information for debugging
        if record.module:
            log_data["module"] = record.module
        if record.funcName and record.funcName != "<module>":
            log_data["function"] = record.funcName
        if record.lineno:
            log_data["line"] = record.lineno
        
        # Include any extra data attached to the record
        if hasattr(record, "extra_data") and record.extra_data:
            log_data.update(record.extra_data)
        
        # Include exception information if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Include stack trace if present (for non-exception stack info)
        if record.stack_info:
            log_data["stack_trace"] = record.stack_info
        
        return json.dumps(log_data, default=str)


class TelemetryService:
    """
    Centralized telemetry service for logging, metrics, and tracing.
    
    This service provides:
    - Structured JSON logging with request correlation
    - OpenTelemetry integration for distributed tracing
    - Custom metrics recording for monitoring
    - Tool invocation logging for AI agent observability
    
    Validates: Requirements 5.1, 5.3, 5.4, 5.5, 5.6, 5.7
    """
    
    def __init__(self, settings: Optional[Any] = None):
        """
        Initialize the telemetry service.
        
        Args:
            settings: Application settings containing log_level, otel_endpoint,
                     and otel_service_name configuration
        """
        self.settings = settings
        self.tracer = None
        self._logger = None
        self._setup_logging()
        self._setup_tracing()
    
    def _setup_logging(self) -> None:
        """
        Configure structured JSON logging.
        
        Sets up the root logger with JSONFormatter and configures
        the log level based on settings.
        """
        # Determine log level from settings or default to INFO
        log_level_str = "INFO"
        if self.settings and hasattr(self.settings, "log_level"):
            log_level_str = self.settings.log_level
        
        log_level = getattr(logging, log_level_str.upper(), logging.INFO)
        
        # Create JSON formatter
        json_formatter = JSONFormatter()
        
        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        
        # Remove existing handlers to avoid duplicate logs
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # Add stdout handler with JSON formatter
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(log_level)
        stdout_handler.setFormatter(json_formatter)
        root_logger.addHandler(stdout_handler)
        
        # Create service-specific logger
        self._logger = logging.getLogger("telemetry")
        self._logger.info("Telemetry service initialized", extra={
            "extra_data": {"log_level": log_level_str}
        })
    
    def _setup_tracing(self) -> None:
        """
        Configure OpenTelemetry tracing.
        
        Sets up the TracerProvider and span exporter if an OTEL endpoint
        is configured in settings.
        """
        if not self.settings:
            return
        
        otel_endpoint = getattr(self.settings, "otel_endpoint", None)
        if not otel_endpoint:
            self._logger.debug("OpenTelemetry endpoint not configured, tracing disabled")
            return
        
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource, SERVICE_NAME
            
            service_name = getattr(self.settings, "otel_service_name", "runsheet-backend")
            
            # Create resource with service name
            resource = Resource(attributes={
                SERVICE_NAME: service_name
            })
            
            # Create and configure tracer provider
            provider = TracerProvider(resource=resource)
            
            # Create OTLP exporter
            exporter = OTLPSpanExporter(endpoint=otel_endpoint)
            
            # Add batch span processor
            provider.add_span_processor(BatchSpanProcessor(exporter))
            
            # Set as global tracer provider
            trace.set_tracer_provider(provider)
            
            # Get tracer for this service
            self.tracer = trace.get_tracer(service_name)
            
            self._logger.info("OpenTelemetry tracing configured", extra={
                "extra_data": {
                    "otel_endpoint": otel_endpoint,
                    "service_name": service_name
                }
            })
        except ImportError as e:
            self._logger.warning(
                "OpenTelemetry packages not installed, tracing disabled",
                extra={"extra_data": {"error": str(e)}}
            )
        except Exception as e:
            self._logger.error(
                "Failed to configure OpenTelemetry tracing",
                extra={"extra_data": {"error": str(e)}}
            )
    
    def get_logger(self, name: str) -> logging.Logger:
        """
        Get a logger with the specified name.
        
        The returned logger will use the JSON formatter configured
        by this service.
        
        Args:
            name: Name for the logger (typically module name)
            
        Returns:
            Configured logger instance
        """
        return logging.getLogger(name)
    
    def log_tool_invocation(
        self,
        tool_name: str,
        input_params: Dict[str, Any],
        duration_ms: float,
        success: bool,
        error: Optional[str] = None
    ) -> None:
        """
        Log an AI tool invocation with metrics.
        
        Validates: Requirement 5.5 - Log tool name, input parameters,
        execution duration, and success/failure status.
        
        Args:
            tool_name: Name of the tool that was invoked
            input_params: Input parameters passed to the tool
            duration_ms: Execution duration in milliseconds
            success: Whether the tool execution succeeded
            error: Error message if the tool failed
        """
        log_data = {
            "tool_name": tool_name,
            "input_params": input_params,
            "duration_ms": duration_ms,
            "success": success,
        }
        
        if error:
            log_data["error"] = error
        
        level = logging.INFO if success else logging.ERROR
        self._logger.log(
            level,
            f"Tool invocation: {tool_name}",
            extra={"extra_data": log_data}
        )
    
    def log_audit_event(
        self,
        event_type: str,
        user_id: Optional[str],
        resource_type: str,
        resource_id: Optional[str],
        action: str,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log an audit event for compliance-sensitive operations.
        
        Validates: Requirement 5.7 - Implement audit logging for
        compliance-sensitive operations.
        
        Args:
            event_type: Type of audit event (e.g., "data_upload", "config_change")
            user_id: ID of the user performing the action
            resource_type: Type of resource being acted upon
            resource_id: ID of the specific resource
            action: Action being performed (e.g., "create", "update", "delete")
            details: Additional details about the event
        """
        audit_data = {
            "audit_event": True,
            "event_type": event_type,
            "user_id": user_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "action": action,
        }
        
        if details:
            audit_data["details"] = details
        
        self._logger.info(
            f"Audit: {event_type} - {action} on {resource_type}",
            extra={"extra_data": audit_data}
        )
    
    def record_metric(
        self,
        name: str,
        value: float,
        tags: Optional[Dict[str, str]] = None
    ) -> None:
        """
        Record a custom metric.
        
        Validates: Requirement 5.4 - Record custom metrics for request
        latency, AI response times, tool usage counts, and error rates.
        
        Args:
            name: Name of the metric
            value: Metric value
            tags: Optional tags for metric dimensions
        """
        metric_data = {
            "metric_name": name,
            "metric_value": value,
        }
        
        if tags:
            metric_data["tags"] = tags
        
        self._logger.debug(
            f"Metric: {name}={value}",
            extra={"extra_data": metric_data}
        )
    
    def create_span(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        """
        Create an OpenTelemetry span for distributed tracing.
        
        Validates: Requirement 5.3 - Integrate with OpenTelemetry
        for distributed tracing.
        
        Args:
            name: Name of the span
            attributes: Optional attributes to add to the span
            
        Returns:
            OpenTelemetry span context manager, or a no-op context manager if tracing not configured
        """
        if self.tracer:
            span = self.tracer.start_as_current_span(name)
            if attributes and hasattr(span, '__enter__'):
                # We'll add attributes after entering the context
                return _SpanContextManager(span, attributes)
            return span
        return _NoOpSpanContextManager()
    
    def create_external_service_span(
        self,
        service_name: str,
        operation: str,
        attributes: Optional[Dict[str, Any]] = None
    ):
        """
        Create a span specifically for external service calls.
        
        This method creates spans with standardized naming and attributes
        for external service calls like Elasticsearch, Gemini API, etc.
        
        Validates: Requirement 5.3 - Integrate with OpenTelemetry
        for distributed tracing across the Backend_Service and external calls.
        
        Args:
            service_name: Name of the external service (e.g., "elasticsearch", "gemini")
            operation: The operation being performed (e.g., "search", "index", "generate")
            attributes: Optional additional attributes for the span
            
        Returns:
            OpenTelemetry span context manager
        """
        span_name = f"{service_name}.{operation}"
        span_attributes = {
            "service.name": service_name,
            "operation.name": operation,
            "span.kind": "client",
        }
        
        if attributes:
            span_attributes.update(attributes)
        
        return self.create_span(span_name, span_attributes)


class _SpanContextManager:
    """
    Context manager wrapper that adds attributes to a span after entering.
    """
    
    def __init__(self, span_context, attributes: Dict[str, Any]):
        self._span_context = span_context
        self._attributes = attributes
        self._span = None
    
    def __enter__(self):
        self._span = self._span_context.__enter__()
        if self._span and hasattr(self._span, 'set_attribute'):
            for key, value in self._attributes.items():
                self._span.set_attribute(key, value)
        return self._span
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._span_context.__exit__(exc_type, exc_val, exc_tb)


class _NoOpSpanContextManager:
    """
    No-op context manager for when tracing is not configured.
    
    This allows code to use span context managers without checking
    if tracing is enabled.
    """
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False
    
    def set_attribute(self, key: str, value: Any) -> None:
        """No-op attribute setter."""
        pass
    
    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        """No-op event adder."""
        pass
    
    def record_exception(self, exception: Exception) -> None:
        """No-op exception recorder."""
        pass


# Global telemetry service instance
_telemetry_service: Optional[TelemetryService] = None


def get_telemetry_service() -> Optional[TelemetryService]:
    """
    Get the global telemetry service instance.
    
    Returns:
        The telemetry service instance, or None if not initialized
    """
    return _telemetry_service


def initialize_telemetry(settings: Optional[Any] = None) -> TelemetryService:
    """
    Initialize the global telemetry service.
    
    Args:
        settings: Application settings for configuration
        
    Returns:
        The initialized telemetry service
    """
    global _telemetry_service
    _telemetry_service = TelemetryService(settings)
    return _telemetry_service


def set_request_id(request_id: str) -> None:
    """
    Set the request ID for the current context.
    
    This function is provided for convenience when the middleware
    is not being used (e.g., in background tasks).
    
    Args:
        request_id: The request ID to set
    """
    request_id_var.set(request_id)


def get_request_id() -> str:
    """
    Get the current request ID from context.
    
    Returns:
        The current request ID, or empty string if not set
    """
    return request_id_var.get("")
