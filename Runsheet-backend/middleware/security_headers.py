"""
Security headers middleware for API security.

This module implements security headers middleware to protect against common
web vulnerabilities by adding security-related HTTP headers to all responses.

Validates:
- Requirement 14.5: THE Backend_Service SHALL add security headers
  (X-Content-Type-Options, X-Frame-Options, Content-Security-Policy) to all responses
"""

import logging
from typing import Callable, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

#: HSTS value sent outside local environments (staging finding F6). One year,
#: covering subdomains. ``preload`` is deliberately not set.
HSTS_VALUE = "max-age=31536000; includeSubDomains"

#: Defaults for the two headers added for F6.
DEFAULT_REFERRER_POLICY = "strict-origin-when-cross-origin"
DEFAULT_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"


# Default Content-Security-Policy directives
# These are restrictive defaults that can be customized via configuration
DEFAULT_CSP_DIRECTIVES = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self' 'unsafe-inline'",  # Allow inline styles for UI frameworks
    "img-src": "'self' data: https:",  # Allow images from self, data URIs, and HTTPS
    "font-src": "'self'",
    "connect-src": "'self'",  # Allow API connections to self
    "frame-ancestors": "'none'",  # Prevent framing (complements X-Frame-Options)
    "base-uri": "'self'",
    "form-action": "'self'",
}


def build_csp_header(directives: dict[str, str] = None) -> str:
    """
    Build a Content-Security-Policy header string from directives.
    
    Args:
        directives: Dictionary of CSP directives. If None, uses defaults.
        
    Returns:
        CSP header string in the format "directive1 value1; directive2 value2"
    """
    if directives is None:
        directives = DEFAULT_CSP_DIRECTIVES
    
    return "; ".join(f"{key} {value}" for key, value in directives.items())


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware that adds security headers to all HTTP responses.
    
    This middleware adds the following security headers:
    - X-Content-Type-Options: nosniff
      Prevents browsers from MIME-sniffing a response away from the declared content-type
    
    - X-Frame-Options: DENY
      Prevents the page from being displayed in a frame/iframe, protecting against clickjacking
    
    - Content-Security-Policy
      Restricts the sources from which content can be loaded, protecting against XSS and
      data injection attacks

    - Referrer-Policy and Permissions-Policy (staging finding F6)

    - Strict-Transport-Security, only when ``strict_transport_security`` is set
      (outside development/test; see ``bootstrap/middleware.py``)
    
    Validates:
    - Requirement 14.5: THE Backend_Service SHALL add security headers
      (X-Content-Type-Options, X-Frame-Options, Content-Security-Policy) to all responses
    """
    
    def __init__(
        self,
        app: ASGIApp,
        x_content_type_options: str = "nosniff",
        x_frame_options: str = "DENY",
        content_security_policy: str = None,
        csp_directives: dict[str, str] = None,
        strict_transport_security: Optional[str] = None,
        referrer_policy: str = DEFAULT_REFERRER_POLICY,
        permissions_policy: str = DEFAULT_PERMISSIONS_POLICY,
    ):
        """
        Initialize the security headers middleware.
        
        Args:
            app: The ASGI application to wrap
            x_content_type_options: Value for X-Content-Type-Options header (default: "nosniff")
            x_frame_options: Value for X-Frame-Options header (default: "DENY")
            content_security_policy: Full CSP header string (overrides csp_directives if provided)
            csp_directives: Dictionary of CSP directives to build the CSP header
            strict_transport_security: HSTS header value; ``None`` (default)
                means the header is not sent
            referrer_policy: Value for the Referrer-Policy header
            permissions_policy: Value for the Permissions-Policy header
        """
        super().__init__(app)
        self.x_content_type_options = x_content_type_options
        self.x_frame_options = x_frame_options
        self.strict_transport_security = strict_transport_security
        self.referrer_policy = referrer_policy
        self.permissions_policy = permissions_policy
        
        # Build CSP header from directives or use provided string
        if content_security_policy:
            self.content_security_policy = content_security_policy
        else:
            self.content_security_policy = build_csp_header(csp_directives)
        
        logger.info(
            "Security headers middleware initialized",
            extra={"extra_data": {
                "x_content_type_options": self.x_content_type_options,
                "x_frame_options": self.x_frame_options,
                "content_security_policy": self.content_security_policy[:100] + "..."
                if len(self.content_security_policy) > 100 else self.content_security_policy
            }}
        )
    
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        """
        Process the request and add security headers to the response.
        
        Args:
            request: The incoming FastAPI request
            call_next: The next middleware or route handler
            
        Returns:
            The response with security headers added
        """
        # Process the request through the rest of the middleware chain
        response = await call_next(request)
        
        # Add security headers to the response
        # X-Content-Type-Options: Prevents MIME-sniffing
        response.headers["X-Content-Type-Options"] = self.x_content_type_options
        
        # X-Frame-Options: Prevents clickjacking
        response.headers["X-Frame-Options"] = self.x_frame_options
        
        # Content-Security-Policy: Restricts content sources
        response.headers["Content-Security-Policy"] = self.content_security_policy

        response.headers["Referrer-Policy"] = self.referrer_policy
        response.headers["Permissions-Policy"] = self.permissions_policy

        # Strict-Transport-Security: configured only outside local environments
        if self.strict_transport_security:
            response.headers["Strict-Transport-Security"] = (
                self.strict_transport_security
            )
        
        return response


def setup_security_headers(
    app,
    x_content_type_options: str = "nosniff",
    x_frame_options: str = "DENY",
    content_security_policy: str = None,
    csp_directives: dict[str, str] = None,
    strict_transport_security: Optional[str] = None,
) -> None:
    """
    Configure security headers middleware for a FastAPI application.
    
    This is a convenience function to add the SecurityHeadersMiddleware
    to a FastAPI application with the specified configuration.
    
    Validates:
    - Requirement 14.5: THE Backend_Service SHALL add security headers
      (X-Content-Type-Options, X-Frame-Options, Content-Security-Policy) to all responses
    
    Args:
        app: The FastAPI application instance
        x_content_type_options: Value for X-Content-Type-Options header (default: "nosniff")
        x_frame_options: Value for X-Frame-Options header (default: "DENY")
        content_security_policy: Full CSP header string (overrides csp_directives if provided)
        csp_directives: Dictionary of CSP directives to build the CSP header
        strict_transport_security: HSTS header value; ``None`` means not sent
    """
    app.add_middleware(
        SecurityHeadersMiddleware,
        x_content_type_options=x_content_type_options,
        x_frame_options=x_frame_options,
        content_security_policy=content_security_policy,
        csp_directives=csp_directives,
        strict_transport_security=strict_transport_security,
    )
    
    logger.info(
        f"Security headers configured: X-Content-Type-Options={x_content_type_options}, "
        f"X-Frame-Options={x_frame_options}, "
        f"HSTS={'on' if strict_transport_security else 'off'}"
    )
