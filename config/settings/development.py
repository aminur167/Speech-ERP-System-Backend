"""Local development settings. Never used in production."""

from .base import *  # noqa: F403

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0"]

# Reminder emails print to the console instead of actually sending — see
# apps/notifications/services.py's DEFAULT_FROM_EMAIL comment for the same
# reasoning applied to SMS/WhatsApp.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# The Next.js dev server.
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

# Surface real errors rather than swallowing them.
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
}
