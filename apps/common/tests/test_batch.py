"""
The batch endpoint must answer exactly as the endpoints it bundles.

Its whole claim is "same data, fewer round trips" — so the core test calls
every allowed path both ways, as Admin and as a Manager, and compares.
"""

import json

import pytest

from apps.common.batch import ALLOWED_PATHS
from apps.enrollments import services as enrollment_services

pytestmark = pytest.mark.django_db

URL = "/api/batch/"
DATE = {"date": "2026-09-14"}

# Parameters each dashboard part is called with.
PARAMS = {
    "/transactions/trend/": {"days": "7"},
    "/transactions/by-category/": {},
    "/transactions/by-method/": {},
    "/daily-closing/history/": {},
    "/branches/overview/": {},
}


def params_for(path):
    return PARAMS.get(path, DATE)


@pytest.fixture
def some_activity(manager, branch, patient_factory, service_factory):
    service = service_factory()
    for _ in range(3):
        enrollment = enrollment_services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=service
        )
        enrollment_services.collect_bill_payment(
            actor=manager, branch=branch, bill=enrollment.oldest_unpaid_bill(), method="cash"
        )


def batch(client, paths):
    return client.post(
        URL,
        {"requests": [{"path": path, "params": params_for(path)} for path in paths]},
        format="json",
    )


def as_json(value):
    """Compare what the browser receives: both sides through the same renderer."""
    return json.loads(json.dumps(value, default=str))


@pytest.mark.parametrize("who", ["admin_client", "manager_client"])
def test_every_part_matches_calling_the_endpoint_directly(who, request, some_activity):
    client = request.getfixturevalue(who)
    paths = sorted(ALLOWED_PATHS)

    response = batch(client, paths)

    assert response.status_code == 200
    parts = response.json()["responses"]
    for path, part in zip(paths, parts):
        direct = client.get("/api" + path, params_for(path))
        assert part["status"] == direct.status_code, path
        assert as_json(part["data"]) == direct.json(), path


def test_a_manager_is_still_confined_to_their_branch(other_manager_client, some_activity):
    """The parts run as the caller, so branch scoping applies exactly as usual."""
    part = batch(other_manager_client, ["/transactions/summary/"]).json()["responses"][0]
    direct = other_manager_client.get("/api/transactions/summary/", DATE).json()

    assert part["data"] == direct


def test_a_part_the_caller_may_not_see_fails_alone(manager_client):
    """/branches/overview/ is Admin-only; the other part still answers."""
    parts = batch(manager_client, ["/branches/overview/", "/transactions/summary/"]).json()[
        "responses"
    ]

    assert parts[0]["status"] == 403
    assert parts[1]["status"] == 200


def test_paths_outside_the_allowlist_are_refused(manager_client):
    response = manager_client.post(
        URL, {"requests": [{"path": "/patients/", "params": {}}]}, format="json"
    )

    assert response.status_code == 400


def test_writes_cannot_be_smuggled_in(manager_client):
    response = manager_client.post(
        URL, {"requests": [{"path": "/expenses/", "params": {}, "method": "POST"}]}, format="json"
    )

    assert response.status_code == 400


def test_the_number_of_parts_is_capped(manager_client):
    response = manager_client.post(
        URL,
        {"requests": [{"path": "/transactions/summary/"}] * 13},
        format="json",
    )

    assert response.status_code == 400


def test_requires_sign_in(api_client):
    assert api_client.post(URL, {"requests": []}, format="json").status_code == 401
