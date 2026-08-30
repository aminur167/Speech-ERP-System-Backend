"""
Production settings.

Everything secret comes from real environment variables — there is no .env in
production. Each `env(...)` call without a default will fail loudly at startup
if unset, which is intentional: a missing secret should stop the deploy, not
silently fall back to an insecure value.
"""

from .base import *  # noqa: F403

DEBUG = False

SECRET_KEY = env("DJANGO_SECRET_KEY")  # noqa: F405 — no default, must be set
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")  # noqa: F405

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

# Warn rather than crash if the error-tracking DSN isn't configured yet.
SENTRY_DSN = env("SENTRY_DSN", default="")  # noqa: F405

LOGGING["root"]["level"] = "WARNING"  # noqa: F405
