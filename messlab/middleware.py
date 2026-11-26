"""Small response-header hardening shared by every environment.

Django already sets several of these through ``SecurityMiddleware`` and its
settings. This middleware adds the ones Django has no setting for, makes the
camera permission explicit for the canteen scanner, and publishes a
Content-Security-Policy that matches the app's static-only front end.
"""
from __future__ import annotations

PERMISSIONS_POLICY = "camera=(self), microphone=(), geolocation=()"

# The app ships its own JavaScript and CSS. Inline styles remain allowed because
# templates use them for layout; inline scripts do not, except in Django admin
# and on the API docs page, which are tools rather than app surfaces.
CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "media-src 'self' blob:"
)

RELAXED_CSP = CSP.replace(
    "script-src 'self';",
    "script-src 'self' 'unsafe-inline';",
)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        headers = response.headers
        headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        path = request.path
        relaxed = path.startswith("/admin/") or path.startswith("/api/docs")
        headers.setdefault(
            "Content-Security-Policy", RELAXED_CSP if relaxed else CSP
        )
        return response
