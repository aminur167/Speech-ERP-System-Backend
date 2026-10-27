"""
The branch report — the payload behind the PDF a Manager hands to the Admin.

What these guard:

1. **It agrees with the Summary screen.** The money half is `branch_summary`
   unchanged; a test pins that, because the whole reason for composing rather
   than re-deriving is that a report and a screen must never disagree about a
   figure someone is about to be asked to explain.
2. **"Collected by service" adds up.** A Payment has no service FK, so each
   row is reached through the bill, installment or booking that took the
   money — and anything with no package behind it still has to appear, or
   the column silently stops totalling to Collected.
3. **Every receipt is listed**, and only the ones that count — a voided
   payment is money that never arrived, so it is in neither.
4. **Branch isolation**, as everywhere.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.payments import services as payment_services
from apps.reporting import services

pytestmark = pytest.mark.django_db

URL = reverse("reporting:branch-report")


@pytest.fixture
def paid(manager, branch, patient_factory):
    """Three collections today, two methods."""
    def _pay(amount, method="cash", category="daily", patient=None):
        payment, _ = payment_services.create_payment(
            actor=manager, branch=branch, patient=patient or patient_factory(),
            amount=Decimal(amount), method=method, category=category,
        )
        return payment

    return _pay


def today():
    return timezone.localdate().isoformat()


class TestItAgreesWithTheSummaryScreen:
    def test_the_money_figures_match_branch_summary(
        self, manager_client, manager, branch, paid
    ):
        paid("800.00")
        paid("1200.00", method="bkash")

        report = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()
        summary = manager_client.get(
            reverse("reporting:branch-summary"), {"dateFrom": today(), "dateTo": today()}
        ).json()

        # Compared as values, not as text. This endpoint stringifies its
        # decimals (money is never a float here); the older reporting views
        # return the service dict unserialized, so the same figure arrives
        # from them as a JSON float. The numbers must agree -- the wire
        # format of the older endpoints is a separate matter.
        for field in (
            "grossCollected", "refunded", "expenses", "netRevenue", "outstandingDue",
        ):
            assert Decimal(str(report[field])) == Decimal(str(summary[field])), field
        for field in ("paymentCount", "newPatients"):
            assert report[field] == summary[field], field
        assert [row["method"] for row in report["byMethod"]] == [
            row["method"] for row in summary["byMethod"]
        ]

    def test_it_carries_every_section(self, manager_client, paid):
        paid("800.00")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        for section in (
            "expensesByCategory", "byService", "attendance", "newlyRegistered",
            "services", "materialsSold", "staffAttendance", "enquiries", "payments",
        ):
            assert section in body, section


class TestTheReceiptList:
    def test_every_collection_is_listed_with_its_receipt(self, manager_client, paid):
        one = paid("800.00")
        two = paid("1200.00", method="bkash")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        receipts = [row["receiptNumber"] for row in body["payments"]]
        assert receipts == [one.receipt_number, two.receipt_number]
        assert body["payments"][0]["amount"] == "800.00"
        assert body["payments"][0]["collectedBy"]

    def test_the_list_sums_to_the_collected_total(self, manager_client, paid):
        """An Admin adding the list up must land on the headline figure."""
        paid("800.00")
        paid("1200.00", method="bkash")
        paid("500.00")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        listed = sum(Decimal(row["amount"]) for row in body["payments"])
        assert listed == Decimal(body["grossCollected"])

    def test_a_voided_payment_leaves_the_list_and_the_total(
        self, manager_client, manager, paid
    ):
        kept = paid("800.00")
        scrapped = paid("1200.00")
        payment_services.void_payment(
            actor=manager, payment=scrapped, reason="rang up twice"
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert [row["receiptNumber"] for row in body["payments"]] == [kept.receipt_number]
        # And the headline no longer counts it.
        assert Decimal(body["grossCollected"]) == Decimal("800.00")
        assert scrapped.receipt_number


class TestTheOtherSections:
    def test_expenses_split_by_category_and_approval(
        self, manager_client, manager, branch
    ):
        from apps.expenses import services as expense_services

        expense_services.create_expense(
            actor=manager, branch=branch,
            data={
                "category": "supplies", "amount": Decimal("9000.00"),
                "description": "Cards", "paid_to": "Shop", "payment_method": "cash",
            },
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert body["expensesByCategory"][0]["category"] == "supplies"
        assert Decimal(body["expensesByCategory"][0]["amount"]) == Decimal("9000.00")
        # Large enough to need review, so it lands in pending, not approved.
        assert Decimal(body["expensesPending"]) == Decimal("9000.00")

    def test_enquiries_are_counted_by_source(self, manager_client, manager, branch):
        from apps.enquiries import services as enquiry_services

        enquiry_services.create_enquiry(
            actor=manager, branch=branch,
            data={"caller_name": "Shirin", "caller_phone": "01712345678",
                  "source": "facebook"},
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert body["enquiries"]["total"] == 1
        assert body["enquiries"]["bySource"] == [{"source": "facebook", "count": 1}]

    def test_materials_sold_are_listed(
        self, manager_client, manager, branch, patient_factory, material_factory
    ):
        from apps.materials import services as material_services

        item = material_factory(name="Flashcards", quantity=10, selling_price=Decimal("350.00"))
        material_services.sell_materials(
            actor=manager, branch=branch, patient=patient_factory(),
            items=[{"material_id": item.id, "quantity": 2}], method="cash",
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert body["materialsSold"] == [
            {"name": "Flashcards", "quantity": 2, "amount": "700.00"}
        ]


class TestCollectedByService:
    def test_a_package_is_named_with_its_intake_and_its_takings(
        self, manager_client, manager, branch, patient_factory, service_factory
    ):
        from apps.enrollments import services as enrollment_services

        service = service_factory(name="Speech Therapy — Monthly")
        enrollment_services.enroll_monthly_with_admission(
            actor=manager, branch=branch, patient=patient_factory(),
            service=service, method="cash",
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        row = next(r for r in body["byService"] if r["service"] == service.name)
        assert row["enrolled"] == 1
        assert Decimal(row["amount"]) > 0

    def test_money_with_no_package_behind_it_is_still_reported(
        self, manager_client, paid
    ):
        """A daily visit reaches Payment with a description and no service."""
        paid("800.00")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert [row["service"] for row in body["byService"]] == [
            services.UNLINKED_SERVICE_LABEL
        ]
        assert Decimal(body["byService"][0]["amount"]) == Decimal("800.00")

    def test_the_column_totals_to_the_collected_figure(
        self, manager_client, manager, branch, patient_factory, service_factory, paid
    ):
        """The reason the unlinked row exists: this has to hold."""
        from apps.enrollments import services as enrollment_services

        enrollment_services.enroll_monthly_with_admission(
            actor=manager, branch=branch, patient=patient_factory(),
            service=service_factory(), method="cash",
        )
        paid("800.00")
        paid("250.00", method="bkash")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert sum(Decimal(row["amount"]) for row in body["byService"]) == Decimal(
            body["grossCollected"]
        )


class TestAttendanceAndNewPatients:
    def test_attendance_is_counted_per_service_kind(
        self, manager_client, branch, patient_factory
    ):
        from apps.patients.models import PatientAttendance

        for kind, status in (
            ("monthly", PatientAttendance.Status.PRESENT),
            ("monthly", PatientAttendance.Status.ABSENT),
            ("installment", PatientAttendance.Status.PRESENT),
        ):
            PatientAttendance.objects.create(
                patient=patient_factory(), branch=branch, service_kind=kind,
                date=timezone.localdate(), status=status,
            )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert body["attendance"]["monthly"] == {
            "present": 1, "informed": 0, "absent": 1
        }
        assert body["attendance"]["installment"] == {
            "present": 1, "informed": 0, "absent": 0
        }

    def test_both_kinds_are_reported_even_when_nobody_was_marked(self, manager_client):
        """The PDF prints both rows; a missing key would print a blank."""
        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert set(body["attendance"]) == {"monthly", "installment"}

    def test_the_new_patients_are_listed_with_what_they_enrolled_in(
        self, manager_client, manager, branch, patient_factory, service_factory
    ):
        from apps.enrollments import services as enrollment_services

        service = service_factory(name="Autism Care — Monthly")
        patient = patient_factory(name="Rafi Ahmed")
        enrollment_services.enroll_monthly_with_admission(
            actor=manager, branch=branch, patient=patient,
            service=service, method="cash",
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        listed = next(
            row for row in body["newlyRegistered"] if row["code"] == patient.patient_code
        )
        assert listed["name"] == "Rafi Ahmed"
        assert listed["service"] == service.name
        assert listed["phone"]
        # The count and the list are the same people.
        assert len(body["newlyRegistered"]) == body["newPatients"]

    def test_a_walk_in_buyer_is_not_a_new_patient(
        self, manager_client, patient_factory
    ):
        """A counter sale creates a Patient row; it is not an intake."""
        patient_factory(name="Counter Buyer", is_walk_in=True)
        registered = patient_factory(name="Real Patient")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert [row["code"] for row in body["newlyRegistered"]] == [
            registered.patient_code
        ]


class TestRangeAndScope:
    def test_yesterday_is_excluded_from_todays_report(self, manager_client, paid):
        old = paid("800.00")
        from apps.payments.models import Payment

        Payment.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        paid("1200.00")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert len(body["payments"]) == 1
        assert Decimal(body["grossCollected"]) == Decimal("1200.00")

    def test_a_backwards_range_is_refused(self, manager_client):
        response = manager_client.get(
            URL, {"dateFrom": today(), "dateTo": "2020-01-01"}
        )

        assert response.status_code == 400

    def test_it_defaults_to_today(self, manager_client, paid):
        paid("800.00")

        body = manager_client.get(URL).json()

        assert body["dateFrom"] == today()
        assert body["dateTo"] == today()


@pytest.mark.isolation
class TestBranchIsolation:
    def test_a_manager_sees_only_their_own_branch(
        self, other_manager_client, paid
    ):
        paid("800.00")

        body = other_manager_client.get(
            URL, {"dateFrom": today(), "dateTo": today()}
        ).json()

        assert body["payments"] == []
        assert Decimal(body["grossCollected"]) == Decimal("0.00")

    def test_admin_can_narrow_to_one_branch(
        self, admin_client, branch, other_branch, paid
    ):
        paid("800.00")

        mine = admin_client.get(
            URL, {"branch": branch.id, "dateFrom": today(), "dateTo": today()}
        ).json()
        theirs = admin_client.get(
            URL, {"branch": other_branch.id, "dateFrom": today(), "dateTo": today()}
        ).json()

        assert Decimal(mine["grossCollected"]) == Decimal("800.00")
        assert Decimal(theirs["grossCollected"]) == Decimal("0.00")
