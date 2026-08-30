"""
The OpenAPI schema (drf-spectacular) builds without a generation error.

Every write endpoint has its own tests already; this one guards a different
failure mode entirely — a serializer/view combination that pytest's API
tests happily exercise but that the schema generator cannot introspect (a
raw APIView with no serializer_class, a ChoiceField it can't resolve, a path
converter it can't type). That class of bug is invisible to the rest of the
suite and only shows up here, or in CI's separate `manage.py spectacular`
step — this test exists so a regression fails locally too, not only in CI.
"""

import pytest
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator

pytestmark = pytest.mark.django_db


def test_schema_generates_with_no_errors():
    """
    Zero errors, not zero warnings: the four remaining warnings are
    enum-naming collisions (several genuinely distinct "status"/"category"
    choice sets across different models sharing that field name), which
    drf-spectacular already resolves to unique hash-suffixed component names
    on its own -- cosmetic, not a sign anything was dropped from the schema.

    An error is different: it means a view got silently excluded from the
    docs entirely (e.g. a raw APIView with no way to introspect its response
    shape) -- exactly the bug this test would have caught before every
    reporting and due-payments endpoint got its docs-only serializer.
    """
    GENERATOR_STATS.reset()
    generator = SchemaGenerator()
    schema = generator.get_schema(request=None, public=True)

    assert schema is not None
    assert schema["paths"], "the generated schema has no paths at all"
    assert not GENERATOR_STATS._error_cache, dict(GENERATOR_STATS._error_cache)


def test_every_endpoint_group_is_represented():
    """
    A coarse sanity check that nothing was accidentally left off the
    router — one path per module, not an exhaustive endpoint inventory.
    """
    generator = SchemaGenerator()
    schema = generator.get_schema(request=None, public=True)
    paths = schema["paths"].keys()

    for prefix in (
        "/api/auth/", "/api/branches/", "/api/patients/", "/api/services/",
        "/api/payments/", "/api/enrollments/", "/api/materials/",
        "/api/due-payments/", "/api/expenses/", "/api/daily-closing/",
        "/api/transactions/",
    ):
        assert any(p.startswith(prefix) for p in paths), f"nothing under {prefix}"
