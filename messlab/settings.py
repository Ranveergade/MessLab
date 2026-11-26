"""Django settings for the MessLab prototype.

Everything is configured through environment variables with safe development
defaults. SQLite is used unless DATABASE_URL points at PostgreSQL.
"""
from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the project has no extra dependency."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-in-production")
DEBUG = os.environ.get("DEBUG", "1").lower() in {"1", "true", "yes", "on"}
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if h.strip()
]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "accounts",
    "menu",
    "dining",
    "forecasting",
    "insights",
    "messops",
    "rewards",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "messlab.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "messlab.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "messlab.context_processors.messlab_context",
            ],
        },
    },
]

WSGI_APPLICATION = "messlab.wsgi.application"
ASGI_APPLICATION = "messlab.asgi.application"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith(("postgres://", "postgresql://")):
    parsed = urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/") or "messlab",
            "USER": parsed.username or "messlab",
            "PASSWORD": parsed.password or "",
            "HOST": parsed.hostname or "localhost",
            "PORT": str(parsed.port or 5432),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {"timeout": 30},
        }
    }

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English")]
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "accounts.authentication.TokenVersionJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# ---------------------------------------------------------------------------
# Authentication: email OTP + JWT
# ---------------------------------------------------------------------------
OTP_TTL_SECONDS = int(os.environ.get("OTP_TTL_SECONDS", "600"))
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN = int(os.environ.get("OTP_RESEND_COOLDOWN", "60"))
OTP_EMAIL_THROTTLE = int(os.environ.get("OTP_EMAIL_THROTTLE", "5"))
OTP_IP_THROTTLE = int(os.environ.get("OTP_IP_THROTTLE", "20"))

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=int(os.environ.get("JWT_ACCESS_MINUTES", "15"))
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.environ.get("JWT_REFRESH_DAYS", "7"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": os.environ.get(
        "JWT_SIGNING_KEY",
        hashlib.sha256(f"messlab-jwt:{SECRET_KEY}".encode()).hexdigest(),
    ),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# Shared cache for OTP throttles. LocMem in DEBUG, DB cache otherwise.
if os.environ.get("MESSLAB_CACHE_BACKEND", "db" if not DEBUG else "locmem") == "db":
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "messlab_cache",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "messlab-otp",
        }
    }

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1").lower() in {"1", "true", "yes", "on"}
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "5"))
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "MessLab <no-reply@messlab.local>")

SPECTACULAR_SETTINGS = {
    "TITLE": "MessLab API",
    "DESCRIPTION": (
        "Intelligent residential dining: student intents in, operational "
        "decisions out. The deployment starts with no sample data."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    # Vendored Swagger UI: no CDN or external runtime request.
    "SWAGGER_UI_DIST": "/static/vendor/swagger-ui",
    "SWAGGER_UI_FAVICON_HREF": "/static/vendor/swagger-ui/favicon-32x32.png",
    # Never send the spec to the public Swagger validator.
    "SWAGGER_UI_SETTINGS": {"validatorUrl": None, "persistAuthorization": False},
    "ENUM_NAME_OVERRIDES": {
        "DietTypeEnum": "menu.models.Dish.DietType",
        "MealNameEnum": "menu.models.MealSlot.Name",
    },
}

# ---------------------------------------------------------------------------
# MessLab domain configuration (all tunable through environment variables)
# ---------------------------------------------------------------------------
MESSLAB = {
    # Forecasting / safety stock
    "SERVICE_LEVEL": float(os.environ.get("MESSLAB_SERVICE_LEVEL", "0.95")),
    "MIN_BUFFER_PCT": float(os.environ.get("MESSLAB_MIN_BUFFER_PCT", "0.02")),
    "MAX_BUFFER_PCT": float(os.environ.get("MESSLAB_MAX_BUFFER_PCT", "0.22")),
    "AVG_KG_PER_STUDENT": float(os.environ.get("MESSLAB_AVG_KG_PER_STUDENT", "0.58")),
    "DEFAULT_INTENT_COVERAGE": float(os.environ.get("MESSLAB_INTENT_COVERAGE", "0.55")),
    "ML_STORE_DIR": os.environ.get("MESSLAB_ML_STORE", str(BASE_DIR / "ml_store")),
    # Impact assumptions, surfaced in the UI next to every number.
    "COST_PER_KG_INR": float(os.environ.get("MESSLAB_COST_PER_KG_INR", "185")),
    "CO2E_PER_KG": float(os.environ.get("MESSLAB_CO2E_PER_KG", "2.5")),
    "SHORTAGE_THRESHOLD": 0.98,
    # QR passes are short-lived; the student page refreshes them automatically.
    "QR_TTL_SECONDS": int(os.environ.get("MESSLAB_QR_TTL_SECONDS", "90")),
    "ALLOW_PASSWORD_LOGIN": os.environ.get(
        "MESSLAB_ALLOW_PASSWORD_LOGIN", "1" if DEBUG else "0"
    ).lower()
    in {"1", "true", "yes", "on"},
    # Trust X-Forwarded-For only when a reverse proxy strips it from clients.
    "TRUST_X_FORWARDED_FOR": os.environ.get(
        "MESSLAB_TRUST_X_FORWARDED_FOR", "0"
    ).lower()
    in {"1", "true", "yes", "on"},
    # Public self-registration. Disable it if accounts are provisioned by staff.
    "ALLOW_SELF_REGISTRATION": os.environ.get(
        "MESSLAB_ALLOW_SELF_REGISTRATION", "1"
    ).lower()
    in {"1", "true", "yes", "on"},
    # Mess Committee SOP: names shown on the SOP page and notice board.
    "CHAIRPERSON_NAME": os.environ.get(
        "MESSLAB_CHAIRPERSON_NAME", "Chairperson – Hostel Residence"
    ),
    "MESS_OPERATOR_NAME": os.environ.get("MESSLAB_OPERATOR_NAME", "Mess operator"),
    # Optional voluntary weekend food-waste reduction form (the SOP annexure).
    "WEEKEND_WASTE_FORM_URL": os.environ.get("MESSLAB_WEEKEND_WASTE_FORM_URL", ""),
}

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# Upload ceilings. The CSV importer also enforces these at the parser level.
DATA_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024

# Headers that are safe in every environment.
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"

if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
