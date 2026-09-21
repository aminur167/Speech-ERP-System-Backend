"""
Query budget for the due-payment endpoints.

These three screens once issued a query per patient (each row's remaining
total and installment count read the related rows one enrollment at a time).
On a hosted database every query is a network round trip, so the page slowed
with every patient added. The number of queries must not depend on how many
patients owe money.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.enrollments import services as enrollment_services

pytestmark = pytest.mark.django_db

ENDPOINTS = [
    "/api/due-payments/",
    "/api/due-payments/summary/",
    "/api/transactions/branch-summary/",
]


def _enrol(n, *, start, manager, branch, patient_factory, service_factory):
    monthly = service_factory(code=f"MON-QB-{start}")
    installment = service_factory(code=f"INS-QB-{start}", category="installment")
    for i in range(n):
        idx = start + i
        patient = patient_factory(patient_code=f"PT-QB-{idx:05d}", phone=f"0181{idx:07d}")
        if i % 2:
            enrollment_services.create_installment_plan(
                actor=manager, branch=branch, patient=patient, service=installment,
                number_of_installments=3,
            )
        else:
            enrollment_services.create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient, service=monthly
            )


def _queries(client, url):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200
    return len(ctx.captured_queries)


@pytest.mark.parametrize("url", ENDPOINTS)
def test_queries_do_not_grow_with_the_number_of_patients(
    url, manager_client, manager, branch, patient_factory, service_factory
):
    common = dict(
        manager=manager, branch=branch,
        patient_factory=patient_factory, service_factory=service_factory,
    )
    _enrol(4, start=1, **common)
    few = _queries(manager_client, url)

    _enrol(16, start=100, **common)
    many = _queries(manager_client, url)

    assert many == few, f"{url}: {few} queries with 4 patients, {many} with 20"
