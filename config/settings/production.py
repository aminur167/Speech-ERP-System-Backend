"""
Production settings.

Everything secret comes from real environment variables — there is no .env in
production. Each `env(...)` call without a default will fail loudly at startup
if unset, which is intentional: a missing secret should stop the deploy, not
silently fall back to an insecure value.
"""

import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration

from .base import *  # noqa: F403

DEBUG = False

SECRET_KEY = env("DJANGO_SECRET_KEY")  # noqa: F405 — no default, must be set
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")  # noqa: F405

# Serves STATIC_ROOT (Django Admin, /api/docs/) directly from the app
# process -- no separate static host needed on a platform like Render.
# Inserted right after SecurityMiddleware per whitenoise's own docs; only
# installed via requirements/prod.txt, so this stays out of base.py, which
# every environment (including dev, without whitenoise installed) imports.
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")  # noqa: F405
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# --------------------------------------------------------------------------
# HTTPS / transport security
# --------------------------------------------------------------------------

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# HSTS: start with a short max-age when first deploying, then raise it once
# HTTPS is confirmed stable — a long max-age locks browsers into HTTPS and is
# painful to undo if the certificate setup turns out to be broken.
SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Behind Nginx: trust its forwarded protocol header so Django knows the
# original request was HTTPS. Only correct when Nginx actually sets it.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # the SPA must read the CSRF token

CONN_MAX_AGE = 60  # persistent DB connections

# Warn rather than crash if the error-tracking DSN isn't configured yet — a
# clinic's first deploy shouldn't be blocked on having Sentry set up.
SENTRY_DSN = env("SENTRY_DSN", default="")  # noqa: F405

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        environment=env("SENTRY_ENVIRONMENT", default="production"),  # noqa: F405
        release=env("SENTRY_RELEASE", default=None),  # noqa: F405
        # A modest sample of request traces, not every request -- full tracing
        # on a low-traffic clinic backend is pure overhead for no real signal.
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1),  # noqa: F405
        # Patient names, phone numbers, and financial details pass through
        # this app constantly. Never let Sentry capture request bodies,
        # headers, or user PII by default.
        send_default_pii=False,
    )

LOGGING["root"]["level"] = "WARNING"  # noqa: F405

# Same "warn, don't crash" shape as Sentry above: defaults to logging instead
# of sending, so reminders (still pending final confirmation with the
# client) never block a deploy on SMTP credentials being ready.
EMAIL_BACKEND = env(  # noqa: F405
    "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = env("EMAIL_HOST", default="")  # noqa: F405
EMAIL_PORT = env.int("EMAIL_PORT", default=587)  # noqa: F405
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")  # noqa: F405
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")  # noqa: F405
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)  # noqa: F405
