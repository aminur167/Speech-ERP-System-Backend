"""
Test settings.

Still PostgreSQL — never SQLite. The money logic relies on select_for_update()
and real sequences, and SQLite silently no-ops row locking, which would make
the concurrency tests pass while proving nothing.
"""

from .base import *  # noqa: F403

DEBUG = False

ALLOWED_HOSTS = ["testserver", "localhost"]

# Fast, deterministic hashing — the real hasher makes user-creating tests
# needlessly slow, and password *strength* isn't what these tests verify.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Throttling would make repeated login tests flaky; the throttle itself is
# tested explicitly by re-enabling it in that one test.
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_RATES": {"login": None},
}

# Keep test output readable — only warnings and above.
LOGGING["root"]["level"] = "WARNING"  # noqa: F405
