"""
Patient Directory tests — the derived-field rules from docs/02.

Nothing in a directory row is stored: `status`, `therapyType`, `paymentType`,
`serviceCategories` and `paymentMethods` are all computed from enrollments,
plans and payments at read time. That makes this the module where a wrong join
produces a plausible-looking answer, so each rule is asserted on its own.

`branchName` gets its own test against a non-Dhaka branch specifically: the
frontend shipped that bug twice by resolving branch names from a hardcoded id
map instead of the relation.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.enrollments import services as enrollment_services
from apps.enrollments.models import EnrollmentStatus
from apps.patients.directory import EMPTY, Status
from apps.patients.models import Patient
from apps.payments import services as payment_services
from apps.payments.models import Payment, PaymentCategory
from apps.services.models import Service

pytestmark = pytest.mark.django_db

DIRECTORY_URL = reverse("patients:patient-directory")
SUMMARY_URL = reverse("patients:patient-directory-summary")


def active_services_url(patient):
    return reverse("patients:patient-active-services", args=[patient.pk])


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(
        name="Speech Therapy Monthly", code="SVC-M1", category=Service.Category.MONTHLY
    )


@pytest.fixture
def installment_service(service_factory):
    return service_factory(
        name="Autism Care Package",
        code="SVC-I1",
        category=Service.Category.INSTALLMENT,
        fee=Decimal("18000.00"),
    )


@pytest.fixture
def enroll_monthly(manager, branch, monthly_service):
    def _enroll(patient, service=None, target_branch=None):
        return enrollment_services.create_monthly_enrollment(
            actor=manager,
            branch=target_branch or branch,
            patient=patient,
            service=service or monthly_service,
        )

    return _enroll


@pytest.fixture
def enroll_installment(manager, branch, installment_service):
    def _enroll(patient, service=None):
        return enrollment_services.create_installment_plan(
            actor=manager,
            branch=branch,
            patient=patient,
            service=service or installment_service,
            number_of_installments=3,
        )

    return _enroll


@pytest.fixture
def pay(manager, branch):
    def _pay(patient, amount="1000.00", *, method="cash", category="", when=None):
        payment, _ = payment_services.create_payment(
            actor=manager,
            branch=patient.branch,
            patient=patient,
            amount=Decimal(amount),
            method=method,
            category=category,
        )
        if when is not None:
            Payment.all_objects.filter(pk=payment.pk).update(created_at=when)
            payment.refresh_from_db()
        return payment

    return _pay


def row_for(client, patient, **params):
    body = client.get(DIRECTORY_URL, params).json()
    return next(r for r in body["results"] if r["id"] == patient.id)


class TestStatusAndTherapyType:
    def test_active_monthly_enrollment_is_active_care(
        self, manager_client, patient_factory, enroll_monthly
    ):
        patient = patient_factory()
        enroll_monthly(patient)

        row = row_for(manager_client, patient)

        assert row["status"] == Status.ACTIVE_CARE
        assert row["therapyType"] == "Speech Therapy Monthly"

    def test_installment_plan_only_is_in_progress(
        self, manager_client, patient_factory, enroll_installment
    ):
        patient = patient_factory()
        enroll_installment(patient)

        row = row_for(manager_client, patient)

        assert row["status"] == Status.IN_PROGRESS
        assert row["therapyType"] == "Autism Care Package"

    def test_neither_is_action_needed(self, manager_client, patient_factory):
        patient = patient_factory()

        row = row_for(manager_client, patient)

        assert row["status"] == Status.ACTION_NEEDED
        assert row["therapyType"] == EMPTY

    def test_a_terminated_enrollment_does_not_count_as_active(
        self, manager_client, patient_factory, enroll_monthly
    ):
        patient = patient_factory()
        enrollment = enroll_monthly(patient)
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        row = row_for(manager_client, patient)

        assert row["status"] == Status.ACTION_NEEDED
        assert row["therapyType"] == EMPTY

    def test_monthly_wins_when_a_patient_holds_both(
        self, manager_client, patient_factory, enroll_monthly, enroll_installment
    ):
        """
        A patient in ongoing therapy is described by that therapy, even while
        paying off a package. Matches the frontend's precedence.
        """
        patient = patient_factory()
        enroll_installment(patient)
        enroll_monthly(patient)

        row = row_for(manager_client, patient)

        assert row["status"] == Status.ACTIVE_CARE
        assert row["therapyType"] == "Speech Therapy Monthly"


class TestPaymentType:
    def test_reflects_the_latest_payment_not_the_first(
        self, manager_client, patient_factory, pay
    ):
        patient = patient_factory()
        pay(patient, method="cash", when=timezone.now() - timedelta(days=3))
        pay(patient, method="bkash", when=timezone.now() - timedelta(hours=1))

        assert row_for(manager_client, patient)["paymentType"] == "bKash"

    def test_a_patient_who_never_paid_shows_a_dash(self, manager_client, patient_factory):
        patient = patient_factory()

        assert row_for(manager_client, patient)["paymentType"] == EMPTY

    def test_the_label_is_returned_not_the_stored_key(
        self, manager_client, patient_factory, pay
    ):
        patient = patient_factory()
        pay(patient, method="bank_transfer")

        assert row_for(manager_client, patient)["paymentType"] == "Bank Transfer"

    def test_payment_methods_lists_every_method_ever_used(
        self, manager_client, patient_factory, pay
    ):
        patient = patient_factory()
        pay(patient, method="cash")
        pay(patient, method="cash")
        pay(patient, method="nagad")

        assert row_for(manager_client, patient)["paymentMethods"] == ["cash", "nagad"]

    def test_a_voided_payment_does_not_put_a_method_on_the_patient(
        self, manager_client, manager, patient_factory, pay
    ):
        """A transaction that never happened shouldn't describe the patient."""
        patient = patient_factory()
        voided = pay(patient, method="rocket")
        payment_services.void_payment(actor=manager, payment=voided, reason="wrong patient")

        row = row_for(manager_client, patient)

        assert row["paymentMethods"] == []
        assert row["paymentType"] == EMPTY


class TestServiceCategories:
    def test_lists_every_distinct_category_billed(
        self, manager_client, patient_factory, pay
    ):
        patient = patient_factory()
        pay(patient, category=PaymentCategory.MONTHLY)
        pay(patient, category=PaymentCategory.MONTHLY)
        pay(patient, category=PaymentCategory.DAILY)

        assert row_for(manager_client, patient)["serviceCategories"] == ["daily", "monthly"]

    def test_material_sales_are_excluded(self, manager_client, patient_factory, pay):
        """
        Materials are goods, not therapy. Including them would put "Material
        sale" in the Service Type column for anyone who ever bought a kit.
        """
        patient = patient_factory()
        pay(patient, category=PaymentCategory.MONTHLY)
        pay(patient, category=PaymentCategory.MATERIAL_SALE)

        assert row_for(manager_client, patient)["serviceCategories"] == ["monthly"]

    def test_a_patient_with_no_payments_has_an_empty_list(
        self, manager_client, patient_factory
    ):
        patient = patient_factory()

        assert row_for(manager_client, patient)["serviceCategories"] == []


class TestBranchNameAndAge:
    def test_branch_name_comes_from_the_relation(
        self, admin_client, patient_factory, other_branch
    ):
        """
        Explicitly a non-Dhaka branch. The frontend shipped this bug twice by
        resolving branch names from a hardcoded id-to-name map.
        """
        patient = patient_factory(branch=other_branch, patient_code="PT-CTG-2026-00001")

        row = row_for(admin_client, patient)

        assert row["branchName"] == other_branch.name
        assert row["branchId"] == str(other_branch.id)

    def test_age_is_derived_from_the_date_of_birth(
        self, manager_client, patient_factory
    ):
        today = timezone.localdate()
        patient = patient_factory(date_of_birth=today.replace(year=today.year - 30))

        assert row_for(manager_client, patient)["age"] == 30

    def test_age_accounts_for_a_birthday_that_has_not_happened_yet(
        self, manager_client, patient_factory
    ):
        """
        Born late in the year: the naive year subtraction reports one year too
        many for most of the calendar.
        """
        today = timezone.localdate()
        upcoming = today + timedelta(days=40)
        patient = patient_factory(
            date_of_birth=date(today.year - 30, upcoming.month, upcoming.day)
        )

        assert row_for(manager_client, patient)["age"] == 29

    def test_a_missing_date_of_birth_gives_a_null_age(
        self, manager_client, patient_factory
    ):
        patient = patient_factory(date_of_birth=None)

        assert row_for(manager_client, patient)["age"] is None


class TestDirectoryFilters:
    def test_status_filter(
        self, manager_client, patient_factory, enroll_monthly, enroll_installment
    ):
        in_care = patient_factory(name="In Care")
        enroll_monthly(in_care)
        paying = patient_factory(name="Paying Off")
        enroll_installment(paying)
        idle = patient_factory(name="Idle")

        def names(status):
            body = manager_client.get(DIRECTORY_URL, {"status": status}).json()
            return {r["name"] for r in body["results"]}

        assert names(Status.ACTIVE_CARE) == {"In Care"}
        assert names(Status.IN_PROGRESS) == {"Paying Off"}
        assert names(Status.ACTION_NEEDED) == {"Idle"}

    def test_a_patient_with_both_is_not_counted_as_in_progress(
        self, manager_client, patient_factory, enroll_monthly, enroll_installment
    ):
        """The status filter has to use the same precedence as the row."""
        both = patient_factory(name="Both")
        enroll_installment(both)
        enroll_monthly(both)

        body = manager_client.get(DIRECTORY_URL, {"status": Status.IN_PROGRESS}).json()
        assert body["count"] == 0

    def test_gender_filter(self, manager_client, patient_factory):
        patient_factory(gender=Patient.Gender.FEMALE)
        patient_factory(gender=Patient.Gender.MALE)

        body = manager_client.get(DIRECTORY_URL, {"gender": "female"}).json()
        assert body["count"] == 1

    def test_service_category_filter(self, manager_client, patient_factory, pay):
        monthly_payer = patient_factory(name="Monthly Payer")
        pay(monthly_payer, category=PaymentCategory.MONTHLY)
        daily_payer = patient_factory(name="Daily Payer")
        pay(daily_payer, category=PaymentCategory.DAILY)

        body = manager_client.get(DIRECTORY_URL, {"serviceCategory": "monthly"}).json()

        assert body["count"] == 1
        assert body["results"][0]["name"] == "Monthly Payer"

    def test_payment_type_filter(self, manager_client, patient_factory, pay):
        bkash_user = patient_factory(name="bKash User")
        pay(bkash_user, method="bkash")
        cash_user = patient_factory(name="Cash User")
        pay(cash_user, method="cash")

        body = manager_client.get(DIRECTORY_URL, {"paymentType": "bkash"}).json()

        assert body["count"] == 1
        assert body["results"][0]["name"] == "bKash User"

    def test_filters_combine_with_search(
        self, manager_client, patient_factory, enroll_monthly
    ):
        wanted = patient_factory(name="Rahim Uddin")
        enroll_monthly(wanted)
        other = patient_factory(name="Rahim Miah")
        enroll_monthly(other)
        patient_factory(name="Karim Uddin")

        body = manager_client.get(
            DIRECTORY_URL, {"search": "Rahim Uddin", "status": Status.ACTIVE_CARE}
        ).json()

        assert body["count"] == 1
        assert body["results"][0]["id"] == wanted.id

    def test_a_matching_patient_appears_once_not_once_per_payment(
        self, manager_client, patient_factory, pay
    ):
        """
        The filters join across to-many relations, so without a distinct the
        same patient comes back once per matching payment.
        """
        patient = patient_factory()
        pay(patient, method="bkash")
        pay(patient, method="bkash")
        pay(patient, method="bkash")

        body = manager_client.get(DIRECTORY_URL, {"paymentType": "bkash"}).json()
        assert body["count"] == 1

    def test_date_overrides_time_range(self, manager_client, patient_factory):
        target = timezone.now() - timedelta(days=20)
        old = patient_factory(name="Older Intake")
        Patient.all_objects.filter(pk=old.pk).update(created_at=target)
        patient_factory(name="Today Intake")

        body = manager_client.get(
            DIRECTORY_URL,
            {"timeRange": "today", "date": timezone.localtime(target).date().isoformat()},
        ).json()

        assert body["count"] == 1
        assert body["results"][0]["name"] == "Older Intake"

    def test_time_range_week_and_month(self, manager_client, patient_factory):
        old = patient_factory(name="Old")
        Patient.all_objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=40)
        )
        patient_factory(name="Recent")

        assert manager_client.get(DIRECTORY_URL, {"timeRange": "week"}).json()["count"] == 1
        assert manager_client.get(DIRECTORY_URL, {"timeRange": "month"}).json()["count"] == 1

    def test_pagination_count_reflects_the_filter(self, manager_client, patient_factory):
        patient_factory(gender=Patient.Gender.FEMALE)
        for _ in range(14):
            patient_factory(gender=Patient.Gender.MALE)

        body = manager_client.get(DIRECTORY_URL, {"gender": "female"}).json()

        assert body["count"] == 1
        assert len(body["results"]) == 1

    def test_page_size_is_honoured(self, manager_client, patient_factory):
        for _ in range(8):
            patient_factory()

        body = manager_client.get(DIRECTORY_URL, {"pageSize": "3"}).json()

        assert body["count"] == 8
        assert len(body["results"]) == 3


class TestDirectorySummary:
    def test_counts_match_the_listing(
        self, manager_client, patient_factory, enroll_monthly, enroll_installment
    ):
        enroll_monthly(patient_factory())
        enroll_installment(patient_factory())
        patient_factory()

        body = manager_client.get(SUMMARY_URL).json()

        assert body["total"] == 3
        assert body["activeCare"] == 1
        assert body["inProgress"] == 1
        assert body["actionNeeded"] == 1

    def test_a_patient_with_both_is_counted_once_as_active_care(
        self, manager_client, patient_factory, enroll_monthly, enroll_installment
    ):
        patient = patient_factory()
        enroll_installment(patient)
        enroll_monthly(patient)

        body = manager_client.get(SUMMARY_URL).json()

        assert body["total"] == 1
        assert body["activeCare"] == 1
        assert body["inProgress"] == 0
        assert body["actionNeeded"] == 0

    def test_intake_for_a_past_date_returns_that_months_intake(
        self, manager_client, patient_factory
    ):
        """
        The dashboard's date picker needs the selected month's intake, not
        whatever this month happens to be.
        """
        old = patient_factory()
        target = timezone.now() - timedelta(days=45)
        Patient.all_objects.filter(pk=old.pk).update(created_at=target)
        patient_factory()  # this month

        target_date = timezone.localtime(target).date()
        body = manager_client.get(SUMMARY_URL, {"date": target_date.isoformat()}).json()

        assert body["intake"] == 1
        assert body["total"] == 2  # total is not month-bound

    def test_an_empty_branch_returns_zeros(self, other_manager_client):
        body = other_manager_client.get(SUMMARY_URL).json()

        assert body["total"] == 0
        assert body["activeCare"] == 0

    @pytest.mark.isolation
    def test_summary_is_branch_scoped(
        self, manager_client, patient_factory, other_branch, other_manager, monthly_service
    ):
        patient_factory()
        foreign = patient_factory(branch=other_branch, patient_code="PT-CTG-2026-00001")
        enrollment_services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch, patient=foreign, service=monthly_service
        )

        body = manager_client.get(SUMMARY_URL).json()

        assert body["total"] == 1
        assert body["activeCare"] == 0


class TestActiveServices:
    def test_returns_several_active_services_newest_first(
        self, manager_client, patient_factory, enroll_monthly, enroll_installment
    ):
        patient = patient_factory()
        enroll_installment(patient)
        enroll_monthly(patient)

        items = manager_client.get(active_services_url(patient)).json()

        assert len(items) == 2
        assert items[0]["type"] == "monthly"  # created last
        assert [i["createdAt"] for i in items] == sorted(
            (i["createdAt"] for i in items), reverse=True
        )

    def test_terminated_services_are_excluded(
        self, manager_client, patient_factory, enroll_monthly, enroll_installment
    ):
        patient = patient_factory()
        enrollment = enroll_monthly(patient)
        enroll_installment(patient)
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        items = manager_client.get(active_services_url(patient)).json()

        assert len(items) == 1
        assert items[0]["type"] == "installment"

    def test_a_patient_with_none_gets_an_empty_list(
        self, manager_client, patient_factory
    ):
        patient = patient_factory()

        response = manager_client.get(active_services_url(patient))

        assert response.status_code == 200
        assert response.json() == []

    def test_monthly_items_carry_their_bills(
        self, manager_client, patient_factory, enroll_monthly
    ):
        patient = patient_factory()
        enroll_monthly(patient)

        item = manager_client.get(active_services_url(patient)).json()[0]

        assert item["serviceName"] == "Speech Therapy Monthly"
        assert len(item["enrollment"]["bills"]) == 3

    def test_installment_items_carry_their_installments_and_total(
        self, manager_client, patient_factory, enroll_installment
    ):
        patient = patient_factory()
        enroll_installment(patient)

        item = manager_client.get(active_services_url(patient)).json()[0]

        assert len(item["plan"]["installments"]) == 3
        assert Decimal(item["plan"]["totalAmount"]) == Decimal("18000.00")

    @pytest.mark.isolation
    def test_another_branchs_patient_is_not_reachable(
        self, manager_client, patient_factory, other_branch
    ):
        foreign = patient_factory(branch=other_branch, patient_code="PT-CTG-2026-00002")

        assert manager_client.get(active_services_url(foreign)).status_code == 404


@pytest.mark.isolation
class TestDirectoryBranchIsolation:
    def test_manager_sees_only_their_own_branch(
        self, manager_client, patient_factory, other_branch
    ):
        patient_factory()
        patient_factory(branch=other_branch, patient_code="PT-CTG-2026-00003")

        assert manager_client.get(DIRECTORY_URL).json()["count"] == 1

    def test_admin_sees_every_branch(self, admin_client, patient_factory, other_branch):
        patient_factory()
        patient_factory(branch=other_branch, patient_code="PT-CTG-2026-00004")

        assert admin_client.get(DIRECTORY_URL).json()["count"] == 2

    def test_admin_can_narrow_to_one_branch(
        self, admin_client, branch, patient_factory, other_branch
    ):
        patient_factory()
        patient_factory(branch=other_branch, patient_code="PT-CTG-2026-00005")

        body = admin_client.get(DIRECTORY_URL, {"branch": str(branch.id)}).json()
        assert body["count"] == 1


class TestDirectoryPerformance:
    """
    The whole point of building the rows the way `directory.py` does. Without
    the bulk lookups this is one query for patients plus five per patient, and
    at a decade of data that is fatal rather than merely slow.
    """

    def test_query_count_does_not_grow_with_the_page(
        self,
        manager_client,
        django_assert_max_num_queries,
        patient_factory,
        enroll_monthly,
        pay,
    ):
        for _ in range(10):
            patient = patient_factory()
            enroll_monthly(patient)
            pay(patient, category=PaymentCategory.MONTHLY, method="bkash")

        with django_assert_max_num_queries(12):
            body = manager_client.get(DIRECTORY_URL, {"pageSize": "10"}).json()

        assert body["count"] == 10
        assert all(r["status"] == Status.ACTIVE_CARE for r in body["results"])

    def test_a_larger_page_does_not_cost_more_queries(
        self,
        manager_client,
        django_assert_max_num_queries,
        patient_factory,
        enroll_monthly,
        pay,
    ):
        """
        The direct N+1 assertion: 30 rows must cost the same as 10. A regression
        that reintroduces per-row lookups fails here even if the numbers in the
        response stay correct.
        """
        for _ in range(30):
            patient = patient_factory()
            enroll_monthly(patient)
            pay(patient, category=PaymentCategory.MONTHLY)

        with django_assert_max_num_queries(12):
            body = manager_client.get(DIRECTORY_URL, {"pageSize": "30"}).json()

        assert len(body["results"]) == 30

    def test_the_summary_is_a_bounded_number_of_queries(
        self, manager_client, django_assert_max_num_queries, patient_factory, enroll_monthly
    ):
        for _ in range(15):
            enroll_monthly(patient_factory())

        with django_assert_max_num_queries(8):
            manager_client.get(SUMMARY_URL)
