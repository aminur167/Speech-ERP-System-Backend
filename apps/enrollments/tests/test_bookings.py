"""
Booking tests — the required-test list in docs/05's "Booking" section.

This endpoint had zero coverage until now, and running the rest of the suite
against real Postgres for the first time surfaced why that mattered: the
advance amount was being taken directly from the request body, with no
server-side recomputation, no booking-window check, and no past-date check —
exactly the class of bug docs/05's required tests exist to catch, just never
written down as a test until this file.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.enrollments.models import Booking
from apps.payments.models import Payment, PaymentCategory
from apps.services.models import Service

pytestmark = pytest.mark.django_db

LIST_URL = reverse("enrollments:booking-list")


@pytest.fixture
def online_service(service_factory):
    return service_factory(
        name="Online Consultation",
        code="SVC-ONLINE-1",
        category=Service.Category.ONLINE,
        fee=Decimal("4000.00"),
    )


@pytest.fixture
def patient(patient_factory):
    return patient_factory(name="Rakib Hasan")


def payload(patient, service, **overrides):
    data = {
        "patient": patient.id,
        "service": service.id,
        "date": (timezone.localdate() + timedelta(days=3)).isoformat(),
        "time": "14:00",
        "method": "bkash",
    }
    data.update(overrides)
    return data


class TestBookingCreation:
    def test_creates_a_booking_and_takes_the_advance(
        self, manager_client, patient, online_service
    ):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service), format="json"
        )

        assert response.status_code == 201
        body = response.json()
        assert body["booking"]["status"] == Booking.Status.CONFIRMED
        assert Decimal(body["payment"]["amount"]) == Decimal("2000.00")

    def test_creates_a_payment_with_category_online(
        self, manager_client, patient, online_service
    ):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service), format="json"
        )

        payment = Payment.objects.get(pk=response.json()["payment"]["id"])
        assert payment.category == PaymentCategory.ONLINE

    def test_the_booking_is_linked_to_its_payment(
        self, manager_client, patient, online_service
    ):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service), format="json"
        )

        booking = Booking.objects.get(pk=response.json()["booking"]["id"])
        assert booking.payment_id == response.json()["payment"]["id"]


@pytest.mark.money
class TestAdvanceRecomputedServerSide:
    """
    The escalation this endpoint used to be wide open to: the mock's
    `advanceAmount` field, posted straight from the client with nothing
    checking it against the service fee.
    """

    def test_advance_is_half_the_service_fee(
        self, manager_client, patient, online_service
    ):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service), format="json"
        )

        assert Decimal(response.json()["payment"]["amount"]) == Decimal("2000.00")
        assert Decimal(response.json()["booking"]["advanceAmount"]) == Decimal("2000.00")

    def test_the_serializer_has_no_advance_amount_field_to_tamper_with(
        self, manager_client, patient, online_service
    ):
        """
        Posting a client-chosen amount is simply ignored -- there is no field
        for it, the same pattern as the materials sale endpoint.
        """
        response = manager_client.post(
            LIST_URL,
            payload(patient, online_service, advanceAmount="1.00"),
            format="json",
        )

        assert response.status_code == 201
        assert Decimal(response.json()["payment"]["amount"]) == Decimal("2000.00")

    @override_settings(BOOKING_ADVANCE_RATIO=0.25)
    def test_the_ratio_is_configurable(self, manager_client, patient, online_service):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service), format="json"
        )

        assert Decimal(response.json()["payment"]["amount"]) == Decimal("1000.00")

    def test_advance_is_decimal_exact_on_an_odd_fee(
        self, manager_client, patient, service_factory
    ):
        service = service_factory(
            code="SVC-ONLINE-ODD", category=Service.Category.ONLINE, fee=Decimal("4999.00")
        )

        response = manager_client.post(LIST_URL, payload(patient, service), format="json")

        assert Decimal(response.json()["payment"]["amount"]) == Decimal("2499.50")


class TestBookingWindow:
    """Server-side re-validation of the UI's 10:00-18:00 picker constraint."""

    @pytest.mark.parametrize("time_value", ["09:59", "18:01", "23:30", "00:00"])
    def test_outside_the_window_is_rejected(
        self, manager_client, patient, online_service, time_value
    ):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service, time=time_value), format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "outside_booking_window"

    @pytest.mark.parametrize("time_value", ["10:00", "13:30", "18:00"])
    def test_inside_the_window_is_accepted(
        self, manager_client, patient, online_service, time_value
    ):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service, time=time_value), format="json"
        )

        assert response.status_code == 201

    def test_no_booking_or_payment_is_created_on_rejection(
        self, manager_client, patient, online_service
    ):
        manager_client.post(
            LIST_URL, payload(patient, online_service, time="20:00"), format="json"
        )

        assert Booking.objects.count() == 0
        assert Payment.objects.count() == 0

    def test_a_malformed_time_is_rejected(self, manager_client, patient, online_service):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service, time="not-a-time"), format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_time"

    @override_settings(BOOKING_WINDOW_START_HOUR=9, BOOKING_WINDOW_END_HOUR=20)
    def test_the_window_is_configurable(self, manager_client, patient, online_service):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service, time="19:30"), format="json"
        )

        assert response.status_code == 201


class TestBookingDate:
    def test_a_past_date_is_rejected(self, manager_client, patient, online_service):
        response = manager_client.post(
            LIST_URL,
            payload(
                patient, online_service,
                date=(timezone.localdate() - timedelta(days=1)).isoformat(),
            ),
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "past_date"

    def test_today_is_accepted(self, manager_client, patient, online_service):
        response = manager_client.post(
            LIST_URL,
            payload(patient, online_service, date=timezone.localdate().isoformat()),
            format="json",
        )

        assert response.status_code == 201

    def test_no_booking_or_payment_is_created_on_a_past_date(
        self, manager_client, patient, online_service
    ):
        manager_client.post(
            LIST_URL,
            payload(
                patient, online_service,
                date=(timezone.localdate() - timedelta(days=1)).isoformat(),
            ),
            format="json",
        )

        assert Booking.objects.count() == 0
        assert Payment.objects.count() == 0


@pytest.mark.django_db(transaction=True)
class TestBookingCode:
    """
    transaction=True: the concurrency test below runs real threads against
    real committed rows, which requires escaping the default per-test
    rollback wrapper -- see conftest.py's fixture-sharing note and the same
    marker on TestPatientCodeConcurrency / TestConcurrentReceiptNumbers.
    """

    def test_code_format(self, manager_client, patient, online_service, branch):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service), format="json"
        )

        code = response.json()["booking"]["bookingCode"]
        year = timezone.localdate().year
        assert code.startswith(f"BKG-{branch.short_code}-{year}-")
        assert len(code.rsplit("-", 1)[1]) == 5

    def test_concurrent_creates_produce_unique_codes(
        self, manager, branch, patient_factory, online_service, django_db_blocker
    ):
        import threading

        from django.db import connection

        from apps.enrollments import services as enrollment_services

        patients = [patient_factory() for _ in range(8)]
        codes, errors = [], []

        def worker(p):
            try:
                with django_db_blocker.unblock():
                    booking, _ = enrollment_services.create_booking(
                        actor=manager, branch=branch, patient=p, service=online_service,
                        booking_date=timezone.localdate() + timedelta(days=3),
                        booking_time="14:00", method="cash",
                    )
                    codes.append(booking.booking_code)
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(p,)) for p in patients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"workers raised: {errors}"
        assert len(codes) == 8
        assert len(set(codes)) == 8


@pytest.mark.isolation
class TestBookingBranchIsolation:
    def test_manager_cannot_book_for_another_branchs_patient(
        self, manager_client, other_branch, patient_factory, online_service
    ):
        foreign = patient_factory(branch=other_branch)

        response = manager_client.post(
            LIST_URL, payload(foreign, online_service), format="json"
        )

        assert response.status_code == 404

    def test_created_booking_always_carries_the_managers_own_branch(
        self, manager_client, manager, patient, online_service, other_branch
    ):
        response = manager_client.post(
            LIST_URL,
            {**payload(patient, online_service), "branch": str(other_branch.id)},
            format="json",
        )

        booking = Booking.objects.get(pk=response.json()["booking"]["id"])
        assert booking.branch_id == manager.branch_id

    def test_manager_list_excludes_other_branches(
        self, manager_client, patient, online_service, other_manager, other_branch,
        patient_factory,
    ):
        manager_client.post(LIST_URL, payload(patient, online_service), format="json")

        foreign_patient = patient_factory(branch=other_branch)
        from apps.enrollments import services as enrollment_services

        enrollment_services.create_booking(
            actor=other_manager, branch=other_branch, patient=foreign_patient,
            service=online_service, booking_date=timezone.localdate() + timedelta(days=3),
            booking_time="14:00", method="cash",
        )

        assert manager_client.get(LIST_URL).json()["count"] == 1

    def test_admin_sees_every_branch(
        self, admin_client, manager, branch, other_branch, patient, online_service,
        other_manager, patient_factory,
    ):
        from apps.enrollments import services as enrollment_services

        enrollment_services.create_booking(
            actor=manager, branch=branch, patient=patient, service=online_service,
            booking_date=timezone.localdate() + timedelta(days=3),
            booking_time="14:00", method="cash",
        )
        enrollment_services.create_booking(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch), service=online_service,
            booking_date=timezone.localdate() + timedelta(days=3),
            booking_time="14:00", method="cash",
        )

        assert admin_client.get(LIST_URL).json()["count"] == 2


class TestBookingPermissions:
    def test_admin_cannot_create_a_booking(self, admin_client, patient, online_service):
        """
        A branch-desk action, like enrolling or collecting: Admin can see
        every branch's bookings but doesn't transact on a branch's behalf.
        """
        response = admin_client.post(
            LIST_URL, payload(patient, online_service), format="json"
        )
        assert response.status_code == 403

    def test_manager_may_list_and_read(self, manager_client, patient, online_service):
        response = manager_client.post(
            LIST_URL, payload(patient, online_service), format="json"
        )
        booking_id = response.json()["booking"]["id"]

        assert manager_client.get(LIST_URL).status_code == 200
        assert manager_client.get(
            reverse("enrollments:booking-detail", args=[booking_id])
        ).status_code == 200
