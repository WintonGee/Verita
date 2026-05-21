"""
Django settings for Verita.

Two database connections by design:
  - 'default'  : connects as app_role (no UPDATE/DELETE on audit_log)
  - 'migrator' : connects as migrator_role (DDL); used only by `migrate`

Run migrations explicitly: `python manage.py migrate --database=migrator`.
The router prevents app-runtime ORM operations from accidentally using migrator.
"""

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-key-replace-me")
# Fail safe: DEBUG off and a restrictive host allowlist unless explicitly set.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,backend").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "rest_framework",
    "corsheaders",
    "drf_spectacular",

    "apps.tenancy",
    "apps.billing",
    "apps.audit",
    "apps.api",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "verita.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "verita.wsgi.application"


# --- Databases ---------------------------------------------------------------
# Two connections to the same Postgres, distinguished by role.
# The 'default' DB is what the application uses at runtime: app_role,
# which has no UPDATE/DELETE on audit_log (enforced in the audit_log migration).

_postgres_host = os.environ.get("POSTGRES_HOST", "localhost")
_postgres_port = os.environ.get("POSTGRES_PORT", "5432")
_postgres_db = os.environ.get("POSTGRES_DB", "verita")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _postgres_db,
        "USER": "app_role",
        "PASSWORD": os.environ.get("APP_ROLE_PASSWORD", "app_pass"),
        "HOST": _postgres_host,
        "PORT": _postgres_port,
    },
    "migrator": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _postgres_db,
        "USER": "migrator_role",
        "PASSWORD": os.environ.get("MIGRATOR_ROLE_PASSWORD", "migrator_pass"),
        "HOST": _postgres_host,
        "PORT": _postgres_port,
    },
}

DATABASE_ROUTERS = ["verita.routers.AppRuntimeRouter"]


# --- Auth --------------------------------------------------------------------
# auth.User is used for OPS STAFF ONLY (is_staff=True).
# Customer-side authentication uses apps.tenancy.CustomerUser, which has its
# own password_hash and is wired via a custom DRF authentication class.

AUTH_PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
]


# --- DRF ---------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],  # views/viewsets declare explicitly
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.audit.exception_handler.error_response_handler",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Verita Metered Billing API",
    "DESCRIPTION": "Customer-facing metered billing API (/v1). "
                   "Internal /ops console endpoints are excluded from the public schema.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "PREPROCESSING_HOOKS": ["verita.schema_hooks.exclude_internal_paths"],
}


# --- CORS --------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:5174",
    ).split(",") if o.strip()
]
CORS_ALLOW_CREDENTIALS = True

# Django checks Origin against this list for unsafe (POST/PATCH/...) methods.
# The SPAs post from these origins (via the Vite proxy), so they must be trusted
# or every ops mutation fails CSRF Origin checking. Browsers always send Origin
# (curl does not — which is why this only surfaces in a real browser).
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "CSRF_TRUSTED_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://localhost:8000",
    ).split(",") if o.strip()
]


# --- Webhook signing ---------------------------------------------------------

WEBHOOK_SECRET_CURRENT = os.environ.get("WEBHOOK_SECRET_CURRENT", "")
WEBHOOK_SECRET_PREVIOUS = os.environ.get("WEBHOOK_SECRET_PREVIOUS", "")  # rotation window
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60  # ±5 min


# --- Internationalization ----------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True


STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Logging -----------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "verita": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
