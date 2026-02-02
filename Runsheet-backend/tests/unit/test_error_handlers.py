"""
Unit tests for error handlers.

Tests the error response model and exception handlers to ensure
they produce correctly structured responses.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock
from fastapi import Request
from fastapi.responses import JSONResponse

from errors.codes import ErrorCode
from errors.exceptions import AppException
from errors.handlers import (
    ErrorResponse,
    get_request_id,
    handle_app_exception,
    handle_unexpected_exception,
    register_exception_handlers,
)


class TestErrorResponse:
    """Tests for the ErrorResponse model."""
    
    def test_error_response_with_all_fields(self):
        """Test ErrorResponse with all fields populated."""
        response = ErrorResponse(
            error_code="VALIDATION_ERROR",
            message="Invalid input",
            details={"field": "email", "reason": "Invalid format"},
            request_id="req-123",
        )
        
        assert response.error_code == "VALIDATION_ERROR"
        assert response.message == "Invalid input"
        assert response.details == {"field": "email", "reason": "Invalid format"}
        assert response.request_id == "req-123"
    
    def test_error_response_without_details(self):
        """Test ErrorResponse without optional details field."""
        response = ErrorResponse(
            error_code="INTERNAL_ERROR",
            message="An error occurred",
            request_id="req-456",
        )
        
        assert response.error_code == "INTERNAL_ERROR"
        assert response.message == "An error occurred"
        assert response.details is None
        assert response.request_id == "req-456"
    
    def test_error_response_model_dump_excludes_none(self):
        """Test that model_dump excludes None values when specified."""
        response = ErrorResponse(
            error_code="INTERNAL_ERROR",
            message="An error occurred",
            request_id="req-789",
        )
        
        dumped = response.model_dump(exclude_none=True)
        assert "details" not in dumped
        assert dumped["error_code"] == "INTERNAL_ERROR"
        assert dumped["message"] == "An error occurred"
        assert dumped["request_id"] == "req-789"


class TestGetRequestId:
    """Tests for the get_request_id function."""
    
    def test_get_request_id_from_state(self):
        """Test getting request_id from request state."""
        request = MagicMock(spec=Request)
        request.state.request_id = "existing-request-id"
        
        result = get_request_id(request)
        
        assert result == "existing-request-id"
    
    def test_get_request_id_generates_uuid_when_not_set(self):
        """Test that a UUID is generated when request_id is not in state."""
        request = MagicMock(spec=Request)
        # Remove request_id attribute to simulate it not being set
        del request.state.request_id
        
        result = get_request_id(request)
        
        # Should be a valid UUID string (36 characters with hyphens)
        assert len(result) == 36
        assert result.count("-") == 4


class TestHandleAppException:
    """Tests for the handle_app_exception handler."""
    
    @pytest.mark.asyncio
    async def test_handle_app_exception_returns_json_response(self):
        """Test that handle_app_exception returns a JSONResponse."""
        request = MagicMock(spec=Request)
        request.state.request_id = "test-request-id"
        request.url.path = "/api/test"
        request.method = "GET"
        
        exc = AppException(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="Validation failed",
            status_code=400,
            details={"field": "name"},
        )
        
        response = await handle_app_exception(request, exc)
        
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400
    
    @pytest.mark.asyncio
    async def test_handle_app_exception_includes_all_fields(self):
        """Test that response includes error_code, message, details, and request_id."""
        request = MagicMock(spec=Request)
        request.state.request_id = "test-request-id"
        request.url.path = "/api/test"
        request.method = "POST"
        
        exc = AppException(
            error_code=ErrorCode.RESOURCE_NOT_FOUND,
            message="Resource not found",
            status_code=404,
            details={"resource_id": "123"},
        )
        
        response = await handle_app_exception(request, exc)
        
        # Parse the response body
        body = response.body.decode("utf-8")
        import json
        data = json.loads(body)
        
        assert data["error_code"] == "RESOURCE_NOT_FOUND"
        assert data["message"] == "Resource not found"
        assert data["details"] == {"resource_id": "123"}
        assert data["request_id"] == "test-request-id"
    
    @pytest.mark.asyncio
    async def test_handle_app_exception_uses_correct_status_code(self):
        """Test that the response uses the exception's status code."""
        request = MagicMock(spec=Request)
        request.state.request_id = "test-id"
        request.url.path = "/api/test"
        request.method = "GET"
        
        exc = AppException(
            error_code=ErrorCode.FORBIDDEN,
            message="Access denied",
            status_code=403,
        )
        
        response = await handle_app_exception(request, exc)
        
        assert response.status_code == 403


class TestHandleUnexpectedException:
    """Tests for the handle_unexpected_exception handler."""
    
    @pytest.mark.asyncio
    async def test_handle_unexpected_exception_returns_500(self):
        """Test that unexpected exceptions return 500 status."""
        request = MagicMock(spec=Request)
        request.state.request_id = "test-request-id"
        request.url.path = "/api/test"
        request.method = "GET"
        
        exc = ValueError("Something went wrong internally")
        
        response = await handle_unexpected_exception(request, exc)
        
        assert isinstance(response, JSONResponse)
        assert response.status_code == 500
    
    @pytest.mark.asyncio
    async def test_handle_unexpected_exception_hides_internal_details(self):
        """Test that internal error details are not exposed to client."""
        request = MagicMock(spec=Request)
        request.state.request_id = "test-request-id"
        request.url.path = "/api/test"
        request.method = "POST"
        
        # Create an exception with sensitive internal details
        exc = RuntimeError("Database connection string: postgres://user:password@host/db")
        
        response = await handle_unexpected_exception(request, exc)
        
        # Parse the response body
        body = response.body.decode("utf-8")
        import json
        data = json.loads(body)
        
        # Should NOT contain the sensitive error message
        assert "postgres" not in data["message"]
        assert "password" not in data["message"]
        assert "Database connection" not in data["message"]
        
        # Should contain generic message
        assert data["error_code"] == "INTERNAL_ERROR"
        assert "unexpected error" in data["message"].lower()
        assert data["request_id"] == "test-request-id"
        
        # Details should be None (not exposed)
        assert "details" not in data or data.get("details") is None
    
    @pytest.mark.asyncio
    async def test_handle_unexpected_exception_includes_request_id(self):
        """Test that request_id is included in the response."""
        request = MagicMock(spec=Request)
        request.state.request_id = "unique-request-123"
        request.url.path = "/api/test"
        request.method = "DELETE"
        
        exc = Exception("Any error")
        
        response = await handle_unexpected_exception(request, exc)
        
        body = response.body.decode("utf-8")
        import json
        data = json.loads(body)
        
        assert data["request_id"] == "unique-request-123"


class TestRegisterExceptionHandlers:
    """Tests for the register_exception_handlers function."""
    
    def test_register_exception_handlers_adds_handlers(self):
        """Test that handlers are registered with the app."""
        mock_app = MagicMock()
        
        register_exception_handlers(mock_app)
        
        # Should have called add_exception_handler twice
        assert mock_app.add_exception_handler.call_count == 2
        
        # Check that AppException handler was registered
        calls = mock_app.add_exception_handler.call_args_list
        exception_types = [call[0][0] for call in calls]
        
        assert AppException in exception_types
        assert Exception in exception_types
