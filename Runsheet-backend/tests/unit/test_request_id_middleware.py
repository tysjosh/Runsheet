"""
Unit tests for request ID middleware.

Tests the RequestIDMiddleware to ensure it correctly generates,
extracts, and propagates request IDs for correlation.

Validates: Requirement 5.2 - Generate a unique request_id and include it
in all log entries for that request.
"""

import pytest
import uuid
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import Response

from middleware.request_id import (
    RequestIDMiddleware,
    request_id_var,
    get_request_id,
    REQUEST_ID_HEADER,
)


class TestRequestIDMiddleware:
    """Tests for the RequestIDMiddleware class."""
    
    @pytest.fixture
    def app_with_middleware(self):
        """Create a FastAPI app with the RequestIDMiddleware."""
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)
        
        @app.get("/test")
        async def test_endpoint(request: Request):
            return {
                "request_id_from_state": request.state.request_id,
                "request_id_from_context": request_id_var.get(),
            }
        
        return app
    
    @pytest.fixture
    def client(self, app_with_middleware):
        """Create a test client for the app."""
        return TestClient(app_with_middleware)
    
    def test_generates_request_id_when_not_provided(self, client):
        """Test that a new UUID is generated when X-Request-ID header is not present."""
        response = client.get("/test")
        
        assert response.status_code == 200
        
        # Check that X-Request-ID header is in response
        assert REQUEST_ID_HEADER in response.headers
        
        # Verify it's a valid UUID format
        request_id = response.headers[REQUEST_ID_HEADER]
        assert len(request_id) == 36
        assert request_id.count("-") == 4
        
        # Verify it can be parsed as a UUID
        parsed_uuid = uuid.UUID(request_id)
        assert str(parsed_uuid) == request_id
    
    def test_uses_existing_request_id_from_header(self, client):
        """Test that existing X-Request-ID header is used when provided."""
        existing_request_id = "existing-request-id-12345"
        
        response = client.get(
            "/test",
            headers={REQUEST_ID_HEADER: existing_request_id}
        )
        
        assert response.status_code == 200
        
        # Check that the same request ID is returned in response header
        assert response.headers[REQUEST_ID_HEADER] == existing_request_id
    
    def test_stores_request_id_in_request_state(self, client):
        """Test that request_id is stored in request.state."""
        existing_request_id = "state-test-request-id"
        
        response = client.get(
            "/test",
            headers={REQUEST_ID_HEADER: existing_request_id}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify request_id was stored in request.state
        assert data["request_id_from_state"] == existing_request_id
    
    def test_stores_request_id_in_context_variable(self, client):
        """Test that request_id is stored in context variable during request."""
        existing_request_id = "context-test-request-id"
        
        response = client.get(
            "/test",
            headers={REQUEST_ID_HEADER: existing_request_id}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify request_id was stored in context variable during request
        assert data["request_id_from_context"] == existing_request_id
    
    def test_adds_request_id_to_response_headers(self, client):
        """Test that request_id is added to response headers."""
        response = client.get("/test")
        
        assert response.status_code == 200
        assert REQUEST_ID_HEADER in response.headers
        
        # The response header should match what was used in the request
        response_request_id = response.headers[REQUEST_ID_HEADER]
        data = response.json()
        assert data["request_id_from_state"] == response_request_id
    
    def test_context_variable_reset_after_request(self, client):
        """Test that context variable is reset after request completes."""
        # Make a request with a specific request ID
        response = client.get(
            "/test",
            headers={REQUEST_ID_HEADER: "first-request-id"}
        )
        assert response.status_code == 200
        
        # After the request, the context variable should be reset to default
        assert request_id_var.get() == ""
    
    def test_different_requests_get_different_ids(self, client):
        """Test that different requests without X-Request-ID get different IDs."""
        response1 = client.get("/test")
        response2 = client.get("/test")
        
        request_id_1 = response1.headers[REQUEST_ID_HEADER]
        request_id_2 = response2.headers[REQUEST_ID_HEADER]
        
        # Each request should get a unique ID
        assert request_id_1 != request_id_2
    
    def test_empty_request_id_header_generates_new_id(self, client):
        """Test that empty X-Request-ID header results in new ID generation."""
        response = client.get(
            "/test",
            headers={REQUEST_ID_HEADER: ""}
        )
        
        assert response.status_code == 200
        
        # Should generate a new UUID since the header was empty
        request_id = response.headers[REQUEST_ID_HEADER]
        assert len(request_id) == 36
        assert request_id.count("-") == 4


class TestGetRequestIdFunction:
    """Tests for the get_request_id helper function."""
    
    def test_get_request_id_returns_current_context_value(self):
        """Test that get_request_id returns the current context variable value."""
        # Set a value in the context variable
        token = request_id_var.set("test-context-id")
        
        try:
            result = get_request_id()
            assert result == "test-context-id"
        finally:
            # Clean up
            request_id_var.reset(token)
    
    def test_get_request_id_returns_empty_string_when_not_set(self):
        """Test that get_request_id returns empty string when not in request context."""
        # Ensure context variable is at default
        result = get_request_id()
        assert result == ""


class TestRequestIDMiddlewareWithExceptions:
    """Tests for middleware behavior when exceptions occur."""
    
    @pytest.fixture
    def app_with_error_endpoint(self):
        """Create a FastAPI app with an endpoint that raises an exception."""
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)
        
        @app.get("/error")
        async def error_endpoint():
            raise ValueError("Test error")
        
        @app.get("/success")
        async def success_endpoint(request: Request):
            return {"request_id": request.state.request_id}
        
        return app
    
    @pytest.fixture
    def client(self, app_with_error_endpoint):
        """Create a test client for the app."""
        return TestClient(app_with_error_endpoint, raise_server_exceptions=False)
    
    def test_context_variable_reset_even_on_exception(self, client):
        """Test that context variable is reset even when an exception occurs."""
        # Make a request that will raise an exception
        response = client.get(
            "/error",
            headers={REQUEST_ID_HEADER: "error-request-id"}
        )
        
        # After the request (even with error), context should be reset
        assert request_id_var.get() == ""
    
    def test_subsequent_request_works_after_exception(self, client):
        """Test that subsequent requests work correctly after an exception."""
        # First request raises an exception
        client.get("/error", headers={REQUEST_ID_HEADER: "error-request-id"})
        
        # Second request should work normally
        response = client.get(
            "/success",
            headers={REQUEST_ID_HEADER: "success-request-id"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["request_id"] == "success-request-id"


class TestRequestIDMiddlewareIntegrationWithErrorHandlers:
    """Tests for middleware integration with error handlers."""
    
    @pytest.fixture
    def app_with_error_handler(self):
        """Create a FastAPI app with middleware and custom error handler."""
        from errors.exceptions import AppException
        from errors.codes import ErrorCode
        from errors.handlers import handle_app_exception
        
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)
        app.add_exception_handler(AppException, handle_app_exception)
        
        @app.get("/app-error")
        async def app_error_endpoint():
            raise AppException(
                error_code=ErrorCode.VALIDATION_ERROR,
                message="Test validation error",
                status_code=400,
            )
        
        return app
    
    @pytest.fixture
    def client(self, app_with_error_handler):
        """Create a test client for the app."""
        return TestClient(app_with_error_handler)
    
    def test_error_response_includes_request_id_from_middleware(self, client):
        """Test that error responses include the request_id set by middleware."""
        test_request_id = "error-handler-test-id"
        
        response = client.get(
            "/app-error",
            headers={REQUEST_ID_HEADER: test_request_id}
        )
        
        assert response.status_code == 400
        data = response.json()
        
        # The error response should include the request_id from middleware
        assert data["request_id"] == test_request_id
        
        # The response header should also have the request_id
        assert response.headers[REQUEST_ID_HEADER] == test_request_id
