"""
Deleting a patient from the row menu.

A soft delete hides the patient everywhere, so it is refused while anything
about them is still live: a running service would keep billing someone nobody
can find, and a debt on a hidden patient could never be collected.
"""

from decimal import Decimal

import pytest

from apps.common.models import AuditLog
from apps.enrollments import services as enrollment_services
from apps.patients.models import Patient

pytestmark = pytest.mark.django_db


def url(patient):
    return f"/api/patients/{patient.pk}/"


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(name="Speech Therapy Monthly", code="DEL-M1", fee=Decimal("5000.00"))


class TestDeletingAPatient:
    def test_a_patient_with_nothing_running_is_deleted(self, manager_client, patient_factory):
        patient = patient_factory(name="Nobody Owes")

        response = manager_client.delete(url(patient))

        assert response.status_code == 204
        assert not Patient.objects.filter(pk=patient.pk).exists()

    def test_the_delete_is_audited(self, manager_client, patient_factory):
        patient = patient_factory()

        manager_client.delete(url(patient))

        entry = AuditLog.objects.filter(action=AuditLog.Action.SOFT_DELETE).latest("created_at")
        assert entry.changes["code"] == patient.patient_code

    def test_refused_while_a_service_is_active(
        self, manager_client, manager, branch, patient_factory, monthly_service, settle_dues
    ):
        patient = patient_factory()
        enrollment_services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient, service=monthly_service
        )
        settle_dues(patient)

        response = manager_client.delete(url(patient))

        assert response.status_code == 400
        assert response.json()["code"] == "has_active_services"
        assert Patient.objects.filter(pk=patient.pk).exists()

    def test_refused_while_a_kept_due_is_owed(
        self, manager_client, manager, branch, patient_factory, monthly_service
    ):
        """
        The service is inactive, but the manager kept its month as a due — so
        the patient still owes it, and hiding them would bury the debt.
        """
        patient = patient_factory()
        enrollment = enrollment_services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient, service=monthly_service
        )
        bill = enrollment.oldest_unpaid_bill()
        enrollment_services.stop_monthly_service(
            actor=manager, enrollment=enrollment, decisions={bill.pk: {"action": "keep"}}
        )

        response = manager_client.delete(url(patient))

        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "outstanding_dues"
        assert body["total"] == "5000.00"

    def test_allowed_once_the_service_is_inactive_and_nothing_is_owed(
        self, manager_client, manager, branch, patient_factory, monthly_service, settle_dues
    ):
        patient = patient_factory()
        enrollment = enrollment_services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient, service=monthly_service
        )
        settle_dues(patient)
        enrollment_services.stop_monthly_service(
            actor=manager, enrollment=enrollment, decisions={}
        )

        assert manager_client.delete(url(patient)).status_code == 204

    def test_another_branch_cannot_see_the_patient_to_delete_it(
        self, other_manager_client, patient_factory
    ):
        patient = patient_factory()

        assert other_manager_client.delete(url(patient)).status_code == 404
        assert Patient.objects.filter(pk=patient.pk).exists()
