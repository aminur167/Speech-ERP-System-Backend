"""
Shared pytest fixtures.

Fixtures here are the building blocks every module's tests reuse: the two
roles, a couple of branches, and authenticated API clients. Having a second
branch available by default matters — most isolation bugs only show up when
there is something to leak *from*.
"""

from datetime import date

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.branches.models import Branch


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def branch(db):
    """The primary branch used by most tests."""
    return Branch.objects.create(
        name="Dhaka Main Branch",
        code="BR-DHK-001",
        status=Branch.Status.ACTIVE,
        address="House 42, Road 8, Dhanmondi R/A, Dhaka 1209",
        phone="+880 2-9611230",
        therapist_count=18,
        support_count=8,
        opened_at=date(2023, 2, 10),
    )


@pytest.fixture
def other_branch(db):
    """
    A second branch, present in most tests on purpose.

    Branch isolation can only be proven when another branch's data exists to
    be wrongly exposed — a single-branch test suite would pass while leaking.
    """
    return Branch.objects.create(
        name="Chattogram Branch",
        code="BR-CTG-001",
        status=Branch.Status.ACTIVE,
        address="GEC Circle, 1259 CDA Avenue, Chattogram 4000",
        phone="+880 31-2556710",
        therapist_count=12,
        support_count=6,
        opened_at=date(2023, 12, 19),
    )


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        email="admin@speechlab.test",
        password="admin-pass-123",
        name="Admin User",
        role=User.Role.ADMIN,
        branch=None,
    )


@pytest.fixture
def manager(db, branch):
    user = User.objects.create_user(
        email="manager@speechlab.test",
        password="manager-pass-123",
        name="Farhana Rahman",
        role=User.Role.MANAGER,
        branch=branch,
        staff_code="MGR-DHK-001",
    )
    branch.manager = user
    branch.save(update_fields=["manager"])
    return user


@pytest.fixture
def other_manager(db, other_branch):
    """The other branch's manager — the attacker in isolation tests."""
    user = User.objects.create_user(
        email="manager.ctg@speechlab.test",
        password="manager-pass-123",
        name="Nusrat Jahan",
        role=User.Role.MANAGER,
        branch=other_branch,
        staff_code="MGR-CTG-001",
    )
    other_branch.manager = user
    other_branch.save(update_fields=["manager"])
    return user


@pytest.fixture
def patient_factory(db, branch):
    """
    Creates patients without going through the API.

    Phone numbers auto-increment because the same number twice would make
    search tests ambiguous, and several suites need a handful of patients
    whose identities don't matter.
    """
    from apps.patients.models import Patient

    counter = {"n": 0}

    def _create(**overrides):
        counter["n"] += 1
        n = counter["n"]
        defaults = {
            "patient_code": f"PT-DHK-2026-{n:05d}",
            "name": f"Test Patient {n}",
            "phone": f"017110000{n:02d}",
            "date_of_birth": date(1995, 1, 1),
            "gender": Patient.Gender.MALE,
            "address": "Dhaka",
            "branch": branch,
        }
        defaults.update(overrides)
        return Patient.objects.create(**defaults)

    return _create


@pytest.fixture
def service_factory(db):
    """Catalog entries. Not branch-scoped — the catalog is organisation-wide."""
    from decimal import Decimal

    from apps.services.models import Service

    counter = {"n": 0}

    def _create(**overrides):
        counter["n"] += 1
        n = counter["n"]
        defaults = {
            "name": f"Service {n}",
            "code": f"SVC-{n:03d}",
            "category": Service.Category.MONTHLY,
            "fee": Decimal("5000.00"),
        }
        defaults.update(overrides)
        return Service.objects.create(**defaults)

    return _create


@pytest.fixture
def material_factory(db, branch):
    """Stock items. Branch-scoped, unlike services."""
    from decimal import Decimal

    from apps.materials.models import Material

    counter = {"n": 0}

    def _create(**overrides):
        counter["n"] += 1
        n = counter["n"]
        defaults = {
            "name": f"Material {n}",
            "code": f"MAT-{n:05d}",
            "unit": Material.Unit.PIECE,
            "quantity": 20,
            "unit_cost": Decimal("250.00"),
            "selling_price": Decimal("350.00"),
            "reorder_level": 5,
            "branch": branch,
        }
        defaults.update(overrides)
        return Material.objects.create(**defaults)

    return _create


def _authenticate(client: APIClient, user: User) -> APIClient:
    """
    Attach a real JWT rather than force_authenticate.

    force_authenticate skips the authentication layer entirely, so it would
    hide a misconfigured token setup. Going through the real flow means the
    tests exercise what production actually does.
    """
    from rest_framework_simplejwt.tokens import RefreshToken

    token = RefreshToken.for_user(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return client


@pytest.fixture
def admin_client(api_client, admin_user):
    return _authenticate(api_client, admin_user)


@pytest.fixture
def manager_client(api_client, manager):
    return _authenticate(api_client, manager)


@pytest.fixture
def other_manager_client(other_manager):
    """Separate client instance so both managers can be used in one test."""
    return _authenticate(APIClient(), other_manager)


@pytest.fixture
def authenticate():
    """Authenticate an arbitrary user, for tests that build their own."""

    def _inner(user, client=None):
        return _authenticate(client or APIClient(), user)

    return _inner
