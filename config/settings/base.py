"""
Settings shared by every environment.

Environment-specific modules (development / production / test) import * from
here and override. Nothing here may assume a particular environment — anything
that differs between local and production belongs in those modules or in .env.
"""

from datetime import timedelta
from pathlib import Path

import environ

# config/settings/base.py -> config/settings -> config -> project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
)

# Read .env if present. Absent in CI/production, where real env vars are used.
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(str(env_file))

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-override-in-env")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
]

LOCAL_APPS = [
    "apps.common",
    "apps.accounts",
    "apps.branches",
    "apps.patients",
    "apps.services",
    "apps.payments",
    "apps.enrollments",
    "apps.materials",
    "apps.duepayments",
    "apps.expenses",
    "apps.dailyclosing",
    "apps.reporting",
    "apps.notifications",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

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

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# PostgreSQL only. The money logic depends on real row-level locking
# (select_for_update) and sequences; SQLite silently no-ops the former, which
# would give false confidence in the concurrency tests.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="speech_erp"),
        "USER": env("POSTGRES_USER", default="postgres"),
        "PASSWORD": env("POSTGRES_PASSWORD", default=""),
        "HOST": env("POSTGRES_HOST", default="localhost"),
        "PORT": env("POSTGRES_PORT", default="5432"),
        "ATOMIC_REQUESTS": False,  # transactions are declared explicitly where needed
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# Internationalization
# --------------------------------------------------------------------------
# Asia/Dhaka is deliberate and load-bearing: "today's collection", daily
# closing, the 5th-of-month due date, and every date-filtered report are
# computed against this zone. Storing UTC with USE_TZ keeps history correct
# while day boundaries follow the clinic's actual working day.

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Dhaka"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------
# Defaults chosen to match what the frontend already expects (see
# docs/00-OVERVIEW.md): DRF's standard pagination envelope
# {count, next, previous, results} and its standard error shapes.

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 10,
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_THROTTLE_RATES": {
        # Brute-force protection on login (docs/01-auth-and-branches.md).
        "login": "10/min",
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# OpenAPI schema served at /api/schema/, interactive docs at /api/docs/
# (config/urls.py). Kept enabled in every environment, including production —
# the API has no public signup path, so the schema itself isn't sensitive,
# and having it live in prod is what makes it trustworthy as a reference.
SPECTACULAR_SETTINGS = {
    "TITLE": "Speech ERP API",
    "DESCRIPTION": (
        "Branch-scoped clinic management: patients, enrollments, payments, "
        "materials, expenses, daily closing, and reporting. See docs/ in the "
        "repository for the business rules behind each endpoint."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Every path here shares a literal "/api/" prefix. Without stripping it
    # first, drf-spectacular's tag auto-inference picks THAT shared segment
    # as the tag for any view not explicitly tagged -- every endpoint below
    # would group under a single meaningless "api" tag instead of by module,
    # and Swagger UI would show empty sections for every declared TAG entry
    # below since nothing actually carries those tag names.
    "SCHEMA_PATH_PREFIX": "/api/",
    # Every endpoint is namespaced under one of these; grouping by prefix
    # keeps the docs organised by module instead of one flat list.
    "TAGS": [
        {"name": "auth", "description": "Login, tokens, and the current user"},
        {"name": "branches", "description": "Branch CRUD and the Admin overview"},
        {"name": "patients", "description": "Registration and the patient directory"},
        {"name": "services", "description": "The service catalog"},
        {"name": "payments", "description": "Payments, void, and refunds"},
        {"name": "enrollments", "description": "Monthly, installment, and booking"},
        {"name": "materials", "description": "Stock and the POS sale flow"},
        {"name": "due-payments", "description": "Outstanding dues, current and historical"},
        {"name": "expenses", "description": "Branch expenses and Admin approval"},
        {"name": "daily-closing", "description": "Cash reconciliation and amendments"},
        {"name": "reporting", "description": "Transactions, dashboards, and analytics"},
    ],
}

# Short-lived access tokens with refresh rotation and blacklisting, so a stolen
# access token has a small window and a rotated refresh token can't be replayed.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True

# --------------------------------------------------------------------------
# Business rules
# --------------------------------------------------------------------------
# Configurable rather than hardcoded — a clinic will eventually want to change
# these, and a code deploy for that is poor design over a 10-year life.

# Expenses at or above this amount need Admin approval; below, auto-approved.
EXPENSE_AUTO_APPROVE_THRESHOLD = env.int("EXPENSE_AUTO_APPROVE_THRESHOLD", default=5000)

# Day of month a monthly bill falls due.
MONTHLY_BILL_DUE_DAY = env.int("MONTHLY_BILL_DUE_DAY", default=5)

# Age below which guardian details are required on patient registration.
PATIENT_MINOR_AGE = env.int("PATIENT_MINOR_AGE", default=18)

# Online booking advance = this fraction of the service fee, always computed
# server-side -- never trusted from the client.
BOOKING_ADVANCE_RATIO = env.float("BOOKING_ADVANCE_RATIO", default=0.5)

# Bookable hours, inclusive at both ends. Enforced server-side even though
# the picker already constrains it in the UI.
BOOKING_WINDOW_START_HOUR = env.int("BOOKING_WINDOW_START_HOUR", default=10)
BOOKING_WINDOW_END_HOUR = env.int("BOOKING_WINDOW_END_HOUR", default=18)

# --------------------------------------------------------------------------
# Cloudinary (material images — docs/06-materials.md)
# --------------------------------------------------------------------------
# The cloud name is used to validate that a submitted image URL belongs to
# this clinic's account, so a client can't point a material's image at
# arbitrary external content. The secret is server-side only and must never be
# exposed to the browser (no NEXT_PUBLIC_* equivalent).

CLOUDINARY_CLOUD_NAME = env("CLOUDINARY_CLOUD_NAME", default="")
CLOUDINARY_API_KEY = env("CLOUDINARY_API_KEY", default="")
CLOUDINARY_API_SECRET = env("CLOUDINARY_API_SECRET", default="")

# Upload constraints — a 12MP phone photo must not be served as-is to the POS.
CLOUDINARY_UPLOAD_FOLDER = env("CLOUDINARY_UPLOAD_FOLDER", default="speech-erp/materials")
CLOUDINARY_MAX_IMAGE_BYTES = env.int("CLOUDINARY_MAX_IMAGE_BYTES", default=5 * 1024 * 1024)
CLOUDINARY_ALLOWED_FORMATS = ["jpg", "jpeg", "png", "webp"]

# --------------------------------------------------------------------------
# Reminders (SMS/WhatsApp via Twilio, email) — apps/notifications
# --------------------------------------------------------------------------
# Same shape as the Cloudinary/Sentry settings above: blank by default, and
# every sender in apps/notifications/services.py no-ops to a log line rather
# than failing when these aren't set — a clinic's first deploy shouldn't be
# blocked on having a Twilio account, and this feature is still pending
# final confirmation with the client at all.

TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN", default="")
TWILIO_SMS_FROM = env("TWILIO_SMS_FROM", default="")
TWILIO_WHATSAPP_FROM = env("TWILIO_WHATSAPP_FROM", default="")

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@speechlab.test")

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",  # raise to DEBUG locally to inspect SQL
            "propagate": False,
        },
    },
}
