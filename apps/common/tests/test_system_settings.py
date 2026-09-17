"""
Clinic-wide settings — currently the one knob (how many days without a visit
before the attendance sheet flags a patient as having stopped coming). Any
authenticated user may read it; only Admin may change it.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.models import SystemSettings

pytestmark = pytest.mark.django_db


def test_defaults_to_the_django_setting(settings):
    settings.PATIENT_ABSENCE_ALERT_DAYS = 21
    assert SystemSettings.get_solo().stopped_coming_after_days == 21


def test_get_solo_returns_the_same_row_every_time():
    first = SystemSettings.get_solo()
    first.stopped_coming_after_days = 30
    first.save()
    assert SystemSettings.get_solo().stopped_coming_after_days == 30


def test_unauthenticated_cannot_read(api_client):
    response = api_client.get(reverse("common:system-settings"))
    assert response.status_code == 401


def test_manager_can_read(manager_client):
    response = manager_client.get(reverse("common:system-settings"))
    assert response.status_code == 200
    assert "stoppedComingAfterDays" in response.json()


def test_manager_cannot_change_it(manager_client):
    response = manager_client.patch(
        reverse("common:system-settings"), {"stoppedComingAfterDays": 5}, format="json"
    )
    assert response.status_code == 403


def test_admin_can_change_it(admin_client):
    response = admin_client.patch(
        reverse("common:system-settings"), {"stoppedComingAfterDays": 10}, format="json"
    )
    assert response.status_code == 200
    assert response.json()["stoppedComingAfterDays"] == 10
    assert SystemSettings.get_solo().stopped_coming_after_days == 10


@pytest.mark.parametrize("value", [0, 366, -5])
def test_rejects_a_value_outside_the_allowed_range(admin_client, value):
    response = admin_client.patch(
        reverse("common:system-settings"), {"stoppedComingAfterDays": value}, format="json"
    )
    assert response.status_code == 400


def test_the_attendance_alert_actually_uses_the_configured_value(
    manager_client, manager, branch, patient_factory, service_factory,
):
    """
    The whole point of making this configurable: changing it here changes
    when the roster actually raises the "stopped coming" flag, not just what
    a settings screen displays.
    """
    from apps.enrollments import services as enrollment_services
    from apps.enrollments.models import MonthlyEnrollment
    from apps.services.models import Service

    SystemSettings.get_solo().delete()
    SystemSettings.objects.create(pk=1, stopped_coming_after_days=3)

    monthly_service = service_factory(
        name="Speech Therapy Monthly", code="STG-M1", category=Service.Category.MONTHLY
    )
    patient = patient_factory(name="Configured Threshold")
    enrollment = enrollment_services.create_monthly_enrollment(
        actor=manager, branch=branch, patient=patient, service=monthly_service
    )
    MonthlyEnrollment.objects.filter(pk=enrollment.pk).update(
        created_at=timezone.now() - timedelta(days=10)
    )

    response = manager_client.get(
        reverse("patients:patient-attendance-roster"), {"kind": "monthly", "alerts": "true"}
    )
    names = [row["patientName"] for row in response.json()["results"]]
    assert "Configured Threshold" in names
