"""Production settings for MessLab.

Use with ``DJANGO_SETTINGS_MODULE=messlab.settings_prod``. The base module keeps
safe development defaults; this module makes the production choices explicit and
fails fast when a required secret is missing.

Run the release steps once per deploy::

    python manage.py migrate
    python manage.py createcachetable
    python manage.py collectstatic --noinput
"""
from __future__ import annotations

import os

# The base settings derive cache, email and demo flags from DEBUG. Force the
# production value *before* importing it so those defaults are correct too.
os.environ["DEBUG"] = "0"

from .settings import *  # noqa: E402,F401,F403
from .settings import BASE_DIR, MESSLAB, REST_FRAMEWORK  # noqa: E402


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = False

SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if not SECRET_KEY or SECRET_KEY.startswith("dev-only"):
    raise RuntimeError(
        "Set a strong SECRET_KEY before starting MessLab in production."
    )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "").split(",")
    if host.strip()
]
if not ALLOWED_HOSTS:
    raise RuntimeError("Set ALLOWED_HOSTS to the public host name(s).")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# ---------------------------------------------------------------------------
# HTTPS, cookies and headers
# ---------------------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = _env_bool("MESSLAB_SSL_REDIRECT", True)
SECURE_HSTS_SECONDS = int(os.environ.get("MESSLAB_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_SECURE = _env_bool("MESSLAB_SECURE_COOKIES", True)
CSRF_COOKIE_SECURE = _env_bool("MESSLAB_SECURE_COOKIES", True)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"

# ---------------------------------------------------------------------------
# Static files: collect once, serve compressed and fingerprinted by WhiteNoise
# ---------------------------------------------------------------------------
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
WHITENOISE_MAX_AGE = 60 * 60 * 24 * 365
WHITENOISE_ALLOW_ALL_ORIGINS = False

# ---------------------------------------------------------------------------
# API: JSON only, no browsable API in production
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# ---------------------------------------------------------------------------
# Explicit production switches
# ---------------------------------------------------------------------------
MESSLAB = {
    **MESSLAB,
    "ALLOW_PASSWORD_LOGIN": _env_bool("MESSLAB_ALLOW_PASSWORD_LOGIN", False),
    "TRUST_X_FORWARDED_FOR": _env_bool("MESSLAB_TRUST_X_FORWARDED_FOR", True),
    "ALLOW_SELF_REGISTRATION": _env_bool("MESSLAB_ALLOW_SELF_REGISTRATION", True),
}

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
)

# ---------------------------------------------------------------------------
# Fail closed on deployment choices that silently lose data or lock users out
# ---------------------------------------------------------------------------
if not os.environ.get("DATABASE_URL", "").strip() and not _env_bool(
    "MESSLAB_ALLOW_SQLITE", False
):
    raise RuntimeError(
        "Set DATABASE_URL for production, or MESSLAB_ALLOW_SQLITE=1 for a "
        "single-node SQLite deployment."
    )

if EMAIL_BACKEND.endswith("console.EmailBackend") and not _env_bool(
    "MESSLAB_ALLOW_CONSOLE_EMAIL", False
):
    raise RuntimeError(
        "Production must deliver OTP email. Configure EMAIL_BACKEND and SMTP "
        "settings, or set MESSLAB_ALLOW_CONSOLE_EMAIL=1 for an intentional "
        "terminal-only deployment."
    )

# ---------------------------------------------------------------------------
# Logging to stdout so the platform (systemd, Docker, PaaS) collects it
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# ---------------------------------------------------------------------------
# Optional error reporting. Install sentry-sdk to enable; no hard dependency.
# ---------------------------------------------------------------------------
SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
    except ImportError:
        pass
    else:
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration()],
            traces_sample_rate=float(
                os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.05")
            ),
            send_default_pii=False,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=os.environ.get("SENTRY_RELEASE") or None,
        )
