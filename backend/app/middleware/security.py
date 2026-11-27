"""Security middleware for adding security headers.

This serves the API, which returns JSON (and TeXML/TwiML to carriers), never
pages. The web app and the embeddable widget are served by the frontend, which
sets its own headers (``frontend/next.config.ts``); the widget is meant to be
framed by customer sites, so nothing here applies to it.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# One year, the value MDN and hstspreload.org recommend. No includeSubDomains
# or preload: those commit every sibling host and are the operator's call.
HSTS_VALUE = "max-age=31536000"

_LOCALHOST_CONNECT = "http://localhost:* https://localhost:* ws://localhost:* wss://localhost:*"
_DOCS_CDN = "https://cdn.jsdelivr.net"


def build_csp(*, debug: bool, docs_enabled: bool) -> str:
    """The Content-Security-Policy for an API response.

    Scripts and inline styles are allowed only for the interactive API docs
    (Swagger UI / ReDoc load from a CDN), and localhost only in development.
    """
    script_style = "'self'"
    if docs_enabled:
        script_style += f" 'unsafe-inline' {_DOCS_CDN}"
    connect = "'self'" + (f" {_LOCALHOST_CONNECT}" if debug else "")
    return (
        "default-src 'self'; "
        f"script-src {script_style}; "
        f"style-src {script_style}; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        f"connect-src {connect}; "
        "frame-ancestors 'none'"
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware to add security headers to all responses.

    ``debug`` is the development switch (``settings.DEBUG``); everything else is
    production. HSTS is sent when the request arrived over https or outside
    development. "Arrived over https" is ``request.url.scheme``, which uvicorn's
    proxy-headers middleware rewrites from ``X-Forwarded-Proto`` only for peers in
    ``TRUSTED_PROXY_IPS``; this class never reads that header itself, so a client
    cannot opt itself in.
    """

    def __init__(self, app: ASGIApp, *, debug: bool = False, docs_enabled: bool = False) -> None:
        super().__init__(app)
        self._debug = debug
        self._csp = build_csp(debug=debug, docs_enabled=docs_enabled)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Add security headers to response."""
        response = await call_next(request)

        # Skip security headers for CORS preflight requests
        # The CORS middleware handles these - we don't want to interfere
        if request.method == "OPTIONS":
            return response

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # X-XSS-Protection is deliberately not sent: the browsers that honoured
        # it removed it, and where it survived it introduced leaks (MDN, OWASP
        # Secure Headers Project). The CSP below is the control.

        if request.url.scheme == "https" or not self._debug:
            response.headers["Strict-Transport-Security"] = HSTS_VALUE

        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        response.headers["Content-Security-Policy"] = self._csp

        # Permissions Policy (formerly Feature-Policy)
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )

        return response
