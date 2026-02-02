"""
Unit tests for security headers middleware.

Tests the SecurityHeadersMiddleware to ensure it correctly adds
security headers to all HTTP responses.

Validates: Requirement 14.5 - THE Backend_Service SHALL add security headers
(X-Content-Type-Options, X-Frame-Options, Content-Security-Policy) to all responses
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from middleware.security_headers import (
    SecurityHeadersMiddleware,
    setup_security_headers,
    build_csp_header,
    DEFAULT_CSP_DIRECTIVES,
)


class TestSecurityHeadersMiddleware:
    """Tests for the SecurityHeadersMiddleware class."""
    
    @pytest.fixture
    def app_with_middleware(self):
        """Create a FastAPI app with the SecurityHeadersMiddleware."""
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)
        
        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}
        
        @app.post("/test")
        async def test_post_endpoint():
            return {"message": "post test"}
        
        return app
    
    @pytest.fixture
    def client(self, app_with_middleware):
        """Create a test client for the app."""
        return TestClient(app_with_middleware)
    
    def test_adds_x_content_type_options_header(self, client):
        """Test that X-Content-Type-Options: nosniff header is added to responses."""
        response = client.get("/test")
        
        assert response.status_code == 200
        assert "X-Content-Type-Options" in response.headers
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    
    def test_adds_x_frame_options_header(self, client):
        """Test that X-Frame-Options: DENY header is added to responses."""
        response = client.get("/test")
        
        assert response.status_code == 200
        assert "X-Frame-Options" in response.headers
        assert response.headers["X-Frame-Options"] == "DENY"
    
    def test_adds_content_security_policy_header(self, client):
        """Test that Content-Security-Policy header is added to responses."""
        response = client.get("/test")
        
        assert response.status_code == 200
        assert "Content-Security-Policy" in response.headers
        
        # Verify CSP contains expected directives
        csp = response.headers["Content-Security-Policy"]
        assert "default-src" in csp
        assert "'self'" in csp
    
    def test_headers_added_to_all_http_methods(self, client):
        """Test that security headers are added to responses for all HTTP methods."""
        # Test GET
        get_response = client.get("/test")
        assert "X-Content-Type-Options" in get_response.headers
        assert "X-Frame-Options" in get_response.headers
        assert "Content-Security-Policy" in get_response.headers
        
        # Test POST
        post_response = client.post("/test")
        assert "X-Content-Type-Options" in post_response.headers
        assert "X-Frame-Options" in post_response.headers
        assert "Content-Security-Policy" in post_response.headers
    
    def test_headers_added_to_error_responses(self):
        """Test that security headers are added to error responses when exception handlers are registered.
        
        Note: When using BaseHTTPMiddleware, headers are only added to error responses
        if the exception is caught and converted to a proper Response object by an
        exception handler. Unhandled exceptions that propagate through the middleware
        will not have headers added because the middleware's dispatch method doesn't
        complete normally.
        
        In the actual application, exception handlers are registered that convert
        exceptions to JSONResponse objects, allowing the middleware to add headers.
        """
        from fastapi.responses import JSONResponse
        
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)
        
        # Register an exception handler to convert ValueError to a proper response
        @app.exception_handler(ValueError)
        async def value_error_handler(request, exc):
            return JSONResponse(
                status_code=500,
                content={"error": str(exc)}
            )
        
        @app.get("/error")
        async def error_endpoint():
            raise ValueError("Test error")
        
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/error")
        
        # With exception handler registered, error responses should have security headers
        assert response.status_code == 500
        assert "X-Content-Type-Options" in response.headers
        assert "X-Frame-Options" in response.headers
        assert "Content-Security-Policy" in response.headers


class TestSecurityHeadersMiddlewareCustomConfiguration:
    """Tests for custom configuration of SecurityHeadersMiddleware."""
    
    def test_custom_x_content_type_options(self):
        """Test that X-Content-Type-Options can be customized."""
        app = FastAPI()
        app.add_middleware(
            SecurityHeadersMiddleware,
            x_content_type_options="nosniff"
        )
        
        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}
        
        client = TestClient(app)
        response = client.get("/test")
        
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    
    def test_custom_x_frame_options(self):
        """Test that X-Frame-Options can be customized."""
        app = FastAPI()
        app.add_middleware(
            SecurityHeadersMiddleware,
            x_frame_options="SAMEORIGIN"
        )
        
        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}
        
        client = TestClient(app)
        response = client.get("/test")
        
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    
    def test_custom_content_security_policy_string(self):
        """Test that Content-Security-Policy can be set as a string."""
        custom_csp = "default-src 'none'; script-src 'self'"
        
        app = FastAPI()
        app.add_middleware(
            SecurityHeadersMiddleware,
            content_security_policy=custom_csp
        )
        
        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}
        
        client = TestClient(app)
        response = client.get("/test")
        
        assert response.headers["Content-Security-Policy"] == custom_csp
    
    def test_custom_csp_directives(self):
        """Test that CSP can be built from custom directives."""
        custom_directives = {
            "default-src": "'none'",
            "script-src": "'self' 'unsafe-inline'",
            "style-src": "'self'",
        }
        
        app = FastAPI()
        app.add_middleware(
            SecurityHeadersMiddleware,
            csp_directives=custom_directives
        )
        
        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}
        
        client = TestClient(app)
        response = client.get("/test")
        
        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp
        assert "script-src 'self' 'unsafe-inline'" in csp
        assert "style-src 'self'" in csp


class TestBuildCspHeader:
    """Tests for the build_csp_header helper function."""
    
    def test_builds_csp_from_default_directives(self):
        """Test that build_csp_header uses default directives when none provided."""
        csp = build_csp_header()
        
        # Should contain all default directives
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
    
    def test_builds_csp_from_custom_directives(self):
        """Test that build_csp_header uses custom directives when provided."""
        custom_directives = {
            "default-src": "'none'",
            "img-src": "'self' data:",
        }
        
        csp = build_csp_header(custom_directives)
        
        assert "default-src 'none'" in csp
        assert "img-src 'self' data:" in csp
        # Should not contain default directives not in custom
        assert "script-src" not in csp
    
    def test_csp_directives_separated_by_semicolon(self):
        """Test that CSP directives are properly separated by semicolons."""
        directives = {
            "default-src": "'self'",
            "script-src": "'self'",
        }
        
        csp = build_csp_header(directives)
        
        # Should be separated by "; "
        assert "; " in csp
        parts = csp.split("; ")
        assert len(parts) == 2


class TestDefaultCspDirectives:
    """Tests for the DEFAULT_CSP_DIRECTIVES constant."""
    
    def test_default_directives_exist(self):
        """Test that DEFAULT_CSP_DIRECTIVES contains expected directives."""
        assert "default-src" in DEFAULT_CSP_DIRECTIVES
        assert "script-src" in DEFAULT_CSP_DIRECTIVES
        assert "style-src" in DEFAULT_CSP_DIRECTIVES
        assert "img-src" in DEFAULT_CSP_DIRECTIVES
        assert "frame-ancestors" in DEFAULT_CSP_DIRECTIVES
    
    def test_default_directives_have_self_as_base(self):
        """Test that default directives use 'self' as the base source."""
        assert "'self'" in DEFAULT_CSP_DIRECTIVES["default-src"]
        assert "'self'" in DEFAULT_CSP_DIRECTIVES["script-src"]
    
    def test_frame_ancestors_is_none(self):
        """Test that frame-ancestors is set to 'none' to prevent framing."""
        assert DEFAULT_CSP_DIRECTIVES["frame-ancestors"] == "'none'"


class TestSetupSecurityHeaders:
    """Tests for the setup_security_headers convenience function."""
    
    def test_setup_adds_middleware_to_app(self):
        """Test that setup_security_headers adds the middleware to the app."""
        app = FastAPI()
        
        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}
        
        setup_security_headers(app)
        
        client = TestClient(app)
        response = client.get("/test")
        
        # Verify headers are present
        assert "X-Content-Type-Options" in response.headers
        assert "X-Frame-Options" in response.headers
        assert "Content-Security-Policy" in response.headers
    
    def test_setup_with_custom_options(self):
        """Test that setup_security_headers accepts custom options."""
        app = FastAPI()
        
        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}
        
        setup_security_headers(
            app,
            x_content_type_options="nosniff",
            x_frame_options="SAMEORIGIN",
            content_security_policy="default-src 'none'"
        )
        
        client = TestClient(app)
        response = client.get("/test")
        
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert response.headers["Content-Security-Policy"] == "default-src 'none'"


class TestSecurityHeadersProperty:
    """
    Property-style tests for security headers.
    
    These tests verify that security headers are present on ALL responses,
    which is the core property required by Requirement 14.5.
    
    **Validates: Requirement 14.5**
    """
    
    @pytest.fixture
    def app_with_various_endpoints(self):
        """Create a FastAPI app with various endpoint types."""
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)
        
        @app.get("/json")
        async def json_endpoint():
            return {"type": "json"}
        
        @app.get("/text")
        async def text_endpoint():
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("plain text")
        
        @app.get("/html")
        async def html_endpoint():
            from fastapi.responses import HTMLResponse
            return HTMLResponse("<html><body>HTML</body></html>")
        
        @app.get("/redirect")
        async def redirect_endpoint():
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/json")
        
        return app
    
    @pytest.fixture
    def client(self, app_with_various_endpoints):
        """Create a test client for the app."""
        return TestClient(app_with_various_endpoints, follow_redirects=False)
    
    def test_security_headers_on_json_response(self, client):
        """Test security headers are present on JSON responses."""
        response = client.get("/json")
        self._assert_all_security_headers_present(response)
    
    def test_security_headers_on_text_response(self, client):
        """Test security headers are present on plain text responses."""
        response = client.get("/text")
        self._assert_all_security_headers_present(response)
    
    def test_security_headers_on_html_response(self, client):
        """Test security headers are present on HTML responses."""
        response = client.get("/html")
        self._assert_all_security_headers_present(response)
    
    def test_security_headers_on_redirect_response(self, client):
        """Test security headers are present on redirect responses."""
        response = client.get("/redirect")
        self._assert_all_security_headers_present(response)
    
    def _assert_all_security_headers_present(self, response):
        """Helper to assert all required security headers are present."""
        assert "X-Content-Type-Options" in response.headers, \
            "X-Content-Type-Options header missing"
        assert response.headers["X-Content-Type-Options"] == "nosniff", \
            "X-Content-Type-Options should be 'nosniff'"
        
        assert "X-Frame-Options" in response.headers, \
            "X-Frame-Options header missing"
        assert response.headers["X-Frame-Options"] == "DENY", \
            "X-Frame-Options should be 'DENY'"
        
        assert "Content-Security-Policy" in response.headers, \
            "Content-Security-Policy header missing"
        assert len(response.headers["Content-Security-Policy"]) > 0, \
            "Content-Security-Policy should not be empty"
