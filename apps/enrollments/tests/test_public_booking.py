"""
Public (unauthenticated) online booking — the clinic's own website, not a
Manager acting on a branch's behalf. No token, no existing Patient, and (per
the product decision behind this feature) no payment taken up front: the
advance is collected in person at the clinic.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.branches.models import Branch
from apps.enrollments.models import Booking
from apps.patients.models import Patient
from apps.services.models import Service

pytestmark = pytest.mark.django_db

BRANCHES_URL = reverse("public:branches")
SERVICES_URL = reverse("public:services")
AVAILABILITY_URL = reverse("public:booking-availability")
BOOKING_URL = reverse("public:booking-create")


@pytest.fixture
def online_service(service_factory):
    return service_factory(
        name="Online Consultation", code="SVC-PUB-ONLINE", category=Service.Category.ONLINE,
        fee=Decimal("4000.00"),
    )


def patient_payload(**overrides):
    data = {
        "name": "Nusrat Jahan",
        "phone": "01711000088",
        "date_of_birth": "1998-03-04",
        "gender": "female",
        "address": "Dhaka",
    }
    data.update(overrides)
    return data


def booking_payload(branch, service, **overrides):
    data = {
        "branch": branch.id,
        "service": service.id,
        "date": (timezone.localdate() + timedelta(days=3)).isoformat(),
        "time": "14:00",
        "patient": patient_payload(),
    }
    data.update(overrides)
    return data


class TestPublicBranchList:
    def test_lists_only_active_branches(self, api_client, branch, other_branch):
        other_branch.status = Branch.Status.INACTIVE
        other_branch.save(update_fields=["status"])

        response = api_client.get(BRANCHES_URL)

        names = [row["name"] for row in response.json()]
        assert branch.name in names
        assert other_branch.name not in names

    def test_no_auth_required(self, api_client):
        response = api_client.get(BRANCHES_URL)
        assert response.status_code == 200


class TestPublicServiceList:
    def test_requires_a_branch_param(self, api_client):
        response = api_client.get(SERVICES_URL)
        assert response.status_code == 400
        assert response.json()["code"] == "branch_required"

    def test_lists_only_active_online_approved_services_for_that_branch(
        self, api_client, branch, other_branch, service_factory, online_service
    ):
        daily_service = service_factory(branch=branch, category=Service.Category.DAILY)
        inactive_online = service_factory(
            branch=branch, category=Service.Category.ONLINE, is_active=False,
        )
        pending_online = service_factory(
            branch=branch, category=Service.Category.ONLINE,
            review_status=Service.ReviewStatus.PENDING,
        )
        other_branch_online = service_factory(branch=other_branch, category=Service.Category.ONLINE)

        response = api_client.get(SERVICES_URL, {"branch": branch.id})

        ids = [row["id"] for row in response.json()]
        assert online_service.id in ids
        assert daily_service.id not in ids
        assert inactive_online.id not in ids
        assert pending_online.id not in ids
        assert other_branch_online.id not in ids


class TestPublicBookingAvailability:
    def test_requires_branch_and_date(self, api_client):
        response = api_client.get(AVAILABILITY_URL)
        assert response.status_code == 400
        assert response.json()["code"] == "missing_params"

    def test_an_unbooked_day_is_all_open(self, api_client, branch):
        target = (timezone.localdate() + timedelta(days=3)).isoformat()
        response = api_client.get(AVAILABILITY_URL, {"branch": branch.id, "date": target})

        slots = response.json()
        assert len(slots) > 0
        assert all(slot["available"] for slot in slots)

    def test_a_confirmed_booking_marks_its_slot_taken(
        self, api_client, branch, online_service
    ):
        target = timezone.localdate() + timedelta(days=3)
        response = api_client.post(
            BOOKING_URL,
            booking_payload(branch, online_service, date=target.isoformat()),
            format="json",
        )
        assert response.status_code == 201

        availability = api_client.get(
            AVAILABILITY_URL, {"branch": branch.id, "date": target.isoformat()}
        ).json()
        taken = next(slot for slot in availability if slot["time"] == "14:00")
        assert taken["available"] is False


class TestPublicBookingCreation:
    def test_creates_a_new_patient_and_a_booking_with_no_payment(
        self, api_client, branch, online_service
    ):
        response = api_client.post(
            BOOKING_URL, booking_payload(branch, online_service), format="json"
        )

        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["status"] == Booking.Status.CONFIRMED
        assert Decimal(body["advanceAmount"]) == Decimal("2000.00")

        booking = Booking.objects.get(booking_code=body["bookingCode"])
        assert booking.payment_id is None
        assert booking.patient.phone == "01711000088"

    def test_a_repeat_phone_number_reuses_the_existing_patient(
        self, api_client, branch, online_service, patient_factory
    ):
        existing = patient_factory(name="Nusrat Jahan", phone="01711000088")

        response = api_client.post(
            BOOKING_URL, booking_payload(branch, online_service), format="json"
        )

        assert response.status_code == 201
        booking = Booking.objects.get(booking_code=response.json()["bookingCode"])
        assert booking.patient_id == existing.id
        assert Patient.objects.filter(phone="01711000088", branch=branch).count() == 1

    def test_a_taken_slot_is_rejected(self, api_client, branch, online_service):
        payload = booking_payload(branch, online_service)
        first = api_client.post(BOOKING_URL, payload, format="json")
        assert first.status_code == 201

        second = api_client.post(
            BOOKING_URL, booking_payload(branch, online_service, patient=patient_payload(phone="01799999999")),
            format="json",
        )
        assert second.status_code == 400
        assert second.json()["code"] == "slot_taken"

    def test_a_past_date_is_rejected(self, api_client, branch, online_service):
        response = api_client.post(
            BOOKING_URL,
            booking_payload(branch, online_service, date=(timezone.localdate() - timedelta(days=1)).isoformat()),
            format="json",
        )
        assert response.status_code == 400
        assert response.json()["code"] == "past_date"

    def test_outside_the_booking_window_is_rejected(self, api_client, branch, online_service):
        response = api_client.post(
            BOOKING_URL, booking_payload(branch, online_service, time="21:00"), format="json"
        )
        assert response.status_code == 400
        assert response.json()["code"] == "outside_booking_window"

    def test_a_service_from_another_branch_is_rejected(
        self, api_client, branch, other_branch, service_factory
    ):
        wrong_branch_service = service_factory(branch=other_branch, category=Service.Category.ONLINE)

        response = api_client.post(
            BOOKING_URL, booking_payload(branch, wrong_branch_service), format="json"
        )
        assert response.status_code == 404

    def test_a_non_online_service_is_rejected(self, api_client, branch, service_factory):
        daily_service = service_factory(branch=branch, category=Service.Category.DAILY)

        response = api_client.post(
            BOOKING_URL, booking_payload(branch, daily_service), format="json"
        )
        assert response.status_code == 404

    def test_a_minor_without_guardian_details_is_rejected(
        self, api_client, branch, online_service
    ):
        response = api_client.post(
            BOOKING_URL,
            booking_payload(
                branch, online_service,
                patient=patient_payload(date_of_birth=(timezone.localdate().replace(year=timezone.localdate().year - 10)).isoformat()),
            ),
            format="json",
        )
        assert response.status_code == 400
        assert "guardian_name" in response.json()["patient"]

    def test_the_endpoint_is_rate_limited(self):
        from apps.enrollments.public_views import PublicBookingCreateView

        assert PublicBookingCreateView.throttle_scope == "public_booking"
