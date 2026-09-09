"""
Automatic monthly termination, and resuming afterwards.

The confirmed rule: a patient has until the last day of the month to clear
that month's due. October's due unpaid when October ends stops the service;
cleared in time, nothing happens.

Two properties carry the weight here:

  * **the debt survives termination.** A manager stopping a service forgives
    what is owed; this doesn't, because the patient may come back in December
    and settle up. Getting that backwards would quietly write off real money.
  * **the gap is never billed.** A service stopped in October and resumed in
    December must not invoice November, when nobody was treated.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.enrollments import services
from apps.enrollments.models import (
    BillStatus,
    due_date_for_month,
    EnrollmentStatus,
    MonthlyBill,
    MonthlyEnrollment,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(name="Individual Therapy", code="MON-AT", fee=Decimal("5000.00"))


@pytest.fixture
def enrollment(manager, branch, patient_factory, monthly_service):
    return services.create_monthly_enrollment(
        actor=manager, branch=branch,
        patient=patient_factory(name="Nusrat Jahan", phone="01712345678"),
        service=monthly_service,
    )


def months_of(enrollment):
    return list(enrollment.bills.order_by("month").values_list("month", flat=True))


def next_month_day(enrollment, day=1):
    """The 1st of the month after the enrollment's first billed one."""
    first = months_of(enrollment)[0]
    year, month = (int(part) for part in first.split("-"))
    return services.add_months(date(year, month, 1), 1).replace(day=day)


class TestAutomaticTermination:
    def test_an_unpaid_month_ends_the_service_once_that_month_is_over(
        self, enrollment
    ):
        result = services.terminate_unpaid_monthly_services(on=next_month_day(enrollment))

        enrollment.refresh_from_db()
        assert result["terminated"] == 1
        assert enrollment.status == EnrollmentStatus.TERMINATED
        assert enrollment.terminated_month == months_of(enrollment)[0]
        assert enrollment.terminated_kind == MonthlyEnrollment.TerminationKind.UNPAID_DUE

    def test_nothing_happens_while_the_month_is_still_running(self, enrollment):
        """The patient has until the last day; the 20th is not the last day."""
        first = months_of(enrollment)[0]
        year, month = (int(part) for part in first.split("-"))

        result = services.terminate_unpaid_monthly_services(on=date(year, month, 20))

        enrollment.refresh_from_db()
        assert result["terminated"] == 0
        assert enrollment.status == EnrollmentStatus.ACTIVE

    def test_clearing_the_due_keeps_the_service_running(
        self, manager, branch, enrollment
    ):
        services.collect_bill_payment(
            actor=manager, branch=branch,
            bill=enrollment.oldest_unpaid_bill(), method="cash",
        )

        result = services.terminate_unpaid_monthly_services(on=next_month_day(enrollment))

        enrollment.refresh_from_db()
        assert result["terminated"] == 0
        assert enrollment.status == EnrollmentStatus.ACTIVE

    def test_the_debt_is_kept_rather_than_written_off(self, enrollment):
        """
        The whole difference from a manager stopping the service. Writing it
        off here would forgive money the patient still owes and leave nothing
        to collect if they come back.
        """
        services.terminate_unpaid_monthly_services(on=next_month_day(enrollment))

        enrollment.refresh_from_db()
        assert enrollment.outstanding_total() == Decimal("5000.00")
        assert not enrollment.bills.filter(status=BillStatus.WRITTEN_OFF).exists()

    def test_paid_history_survives(self, manager, branch, enrollment, monthly_service):
        """Termination must not cost the clinic its record of what was paid."""
        paid = enrollment.oldest_unpaid_bill()
        services.collect_bill_payment(
            actor=manager, branch=branch, bill=paid, method="cash"
        )
        # Let the next month arrive, then let *it* lapse instead.
        services.generate_due_bills(
            up_to=services.add_months(timezone.localdate(), 1)
        )
        second = months_of(enrollment)[1]
        year, month = (int(part) for part in second.split("-"))
        services.terminate_unpaid_monthly_services(
            on=services.add_months(date(year, month, 1), 1)
        )

        paid.refresh_from_db()
        assert paid.status == BillStatus.PAID
        assert paid.payment is not None

    def test_no_month_beyond_the_one_that_ended_it_is_left_behind(self, enrollment):
        """
        There is nothing to drop any more — future months are not created
        until they arrive — so the enrollment must end owning exactly the
        month that stopped it, and no invented one.
        """
        services.terminate_unpaid_monthly_services(on=next_month_day(enrollment))

        enrollment.refresh_from_db()
        assert months_of(enrollment) == [enrollment.terminated_month]

    def test_running_twice_terminates_once(self, enrollment):
        day = next_month_day(enrollment)

        first = services.terminate_unpaid_monthly_services(on=day)
        second = services.terminate_unpaid_monthly_services(on=day)

        assert first["terminated"] == 1
        assert second["terminated"] == 0

    def test_a_missed_run_still_catches_up(self, enrollment):
        """
        If the job doesn't run for months, the next run must still end the
        service rather than skipping it forever.
        """
        first = months_of(enrollment)[0]
        year, month = (int(part) for part in first.split("-"))
        much_later = services.add_months(date(year, month, 1), 6)

        result = services.terminate_unpaid_monthly_services(on=much_later)

        assert result["terminated"] == 1

    def test_an_already_stopped_service_is_left_alone(self, manager, enrollment):
        services.terminate(actor=manager, container=enrollment)

        result = services.terminate_unpaid_monthly_services(on=next_month_day(enrollment))

        enrollment.refresh_from_db()
        assert result["terminated"] == 0
        # Still the manager's own termination, not overwritten by the job.
        assert enrollment.terminated_kind == MonthlyEnrollment.TerminationKind.MANUAL


def lapse(enrollment, *, months_back: int = 2, due_months: int = 2):
    """
    Rewind an enrollment's billing so it starts `months_back` months ago,
    then let the job run for real.

    The bills are backdated rather than the clock being moved forward,
    because that is what production actually looks like: real time passes, so
    `timezone.localdate()` really is later than the months owed. Feeding the
    job a future `on` while today stays put would instead put the arrears and
    the new cycle in the same month — a state that cannot occur, and one that
    hides whether resuming starts the cycle correctly.

    Each bill gets its own month rather than a shared one: (enrollment,
    month) is unique, and stacking them would fail on the constraint instead
    of producing the history being described.

    The bills are written out rather than reshaped from a lookahead, because
    there is no lookahead any more — an enrollment opens with one month, and
    the rest arrive as time passes.
    """
    start = services.add_months(timezone.localdate().replace(day=1), -months_back)

    enrollment.bills.all().delete()
    for offset in range(due_months):
        month_date = services.add_months(start, offset)
        key = services.month_key(month_date)
        MonthlyBill.objects.create(
            enrollment=enrollment,
            month=key,
            label=services.month_label(month_date),
            amount=enrollment.service.fee,
            due_date=due_date_for_month(key),
            status=BillStatus.DUE,
        )

    services.terminate_unpaid_monthly_services()
    enrollment.refresh_from_db()
    return enrollment


@pytest.fixture
def lapsed(enrollment):
    """A patient who stopped paying two months ago, with the job catching up today."""
    return lapse(enrollment)


@pytest.mark.money
class TestReactivating:
    """
    Coming back means clearing the debt first, then the service runs again —
    in that order.

    The old resume offered a second choice at this moment: settle the arrears,
    or waive them. Waiving is gone. A due that survived an explicit keep
    decision could otherwise be forgiven later with no fresh justification,
    quietly undoing the reason the keep-or-cancel choice exists. The debt is
    collected on Due Payments, and reactivation is refused until it is.
    """

    def test_the_two_lapsed_months_are_what_is_owed(self, lapsed):
        assert lapsed.status == EnrollmentStatus.TERMINATED
        assert lapsed.unpaid_bills().count() == 2
        assert lapsed.outstanding_total() == Decimal("10000.00")

    def test_reactivating_is_refused_while_anything_is_owed(self, manager, lapsed):
        with pytest.raises(services.EnrollmentError) as caught:
            services.resume_monthly_service(actor=manager, enrollment=lapsed)

        assert caught.value.code == "outstanding_dues"
        lapsed.refresh_from_db()
        assert lapsed.status == EnrollmentStatus.TERMINATED

    def test_the_refusal_names_the_months_and_the_total(self, manager, lapsed):
        """The manager has to be able to say what has to be paid, not just that."""
        with pytest.raises(services.EnrollmentError) as caught:
            services.resume_monthly_service(actor=manager, enrollment=lapsed)

        assert caught.value.extra["total"] == "10000.00"
        assert len(caught.value.extra["items"]) == 2

    def test_a_due_on_another_service_blocks_it_too(
        self, manager, branch, lapsed, service_factory, settle_dues
    ):
        """
        The gate is the patient's whole balance, not this service's. Otherwise
        a patient walks away from one debt and reactivates around it.
        """
        settle_dues(lapsed.patient)
        other = services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=lapsed.patient,
            service=service_factory(name="Group Therapy", code="MON-AT2"),
        )
        assert other.outstanding_total() > 0

        with pytest.raises(services.EnrollmentError) as caught:
            services.resume_monthly_service(actor=manager, enrollment=lapsed)
        assert caught.value.code == "outstanding_dues"

    def test_clearing_the_arrears_allows_it(self, manager, lapsed, settle_dues):
        settle_dues(lapsed.patient)

        resumed = services.resume_monthly_service(actor=manager, enrollment=lapsed)

        assert resumed.status == EnrollmentStatus.ACTIVE

    def test_the_new_cycle_starts_at_the_current_month(
        self, manager, lapsed, settle_dues
    ):
        settle_dues(lapsed.patient)
        resumed = services.resume_monthly_service(actor=manager, enrollment=lapsed)

        payable = resumed.oldest_unpaid_bill()
        assert payable is not None
        assert payable.month == services.month_key(timezone.localdate())
        assert payable.outstanding == Decimal("5000.00")

    def test_the_arrears_are_not_re_billed_as_part_of_the_new_cycle(
        self, manager, lapsed, settle_dues
    ):
        """
        Settling the arrears must not leave them looking payable again — the
        previous due and the new cycle stay separate sums.
        """
        settle_dues(lapsed.patient)
        resumed = services.resume_monthly_service(actor=manager, enrollment=lapsed)

        current = services.month_key(timezone.localdate())
        assert not resumed.unpaid_bills().filter(month__lt=current).exists()

    def test_the_gap_month_is_never_billed(self, manager, enrollment, settle_dues):
        """
        Stopped after one lapsed month, reactivated later: the months in
        between had no service and must not appear as bills.
        """
        today = timezone.localdate()
        start = services.add_months(today.replace(day=1), -3)
        lapse(enrollment, months_back=3, due_months=1)
        settle_dues(enrollment.patient)

        resumed = services.resume_monthly_service(actor=manager, enrollment=enrollment)

        lapsed_month = services.month_key(start)
        current = services.month_key(today)
        gap = [m for m in months_of(resumed) if lapsed_month < m < current]
        assert gap == []

    def test_reactivating_twice_is_refused(self, manager, lapsed, settle_dues):
        settle_dues(lapsed.patient)
        services.resume_monthly_service(actor=manager, enrollment=lapsed)
        lapsed.refresh_from_db()

        with pytest.raises(services.EnrollmentError) as caught:
            services.resume_monthly_service(actor=manager, enrollment=lapsed)

        assert caught.value.code == "not_terminated"

    def test_a_manually_stopped_service_is_billable_again_once_reactivated(
        self, manager, enrollment
    ):
        """
        The trap: stopping by hand forgives the current month's bill, so a
        naive reactivation finds that row already there and creates nothing —
        leaving the service active and quietly never invoicing again.
        """
        services.terminate(actor=manager, container=enrollment)
        enrollment.refresh_from_db()

        resumed = services.resume_monthly_service(
            actor=manager, enrollment=enrollment
        )

        payable = resumed.oldest_unpaid_bill()
        assert payable is not None
        assert payable.month == services.month_key(timezone.localdate())
        assert payable.outstanding == Decimal("5000.00")

    def test_reopening_a_forgiven_month_is_recorded(self, manager, enrollment):
        """A write-off being undone is never allowed to be silent."""
        from apps.common.models import AuditLog

        services.terminate(actor=manager, container=enrollment)
        enrollment.refresh_from_db()
        services.resume_monthly_service(actor=manager, enrollment=enrollment)

        entry = AuditLog.objects.filter(
            target_type="MonthlyEnrollment", action=AuditLog.Action.UPDATE
        ).latest("created_at")
        assert entry.changes["reinstatedMonths"]

    def test_an_already_paid_month_is_not_billed_twice_on_reactivation(
        self, manager, branch, enrollment
    ):
        """Reinstating must never reach a month the patient already settled."""
        paid = enrollment.oldest_unpaid_bill()
        services.collect_bill_payment(
            actor=manager, branch=branch, bill=paid, method="cash"
        )
        services.terminate(actor=manager, container=enrollment)
        enrollment.refresh_from_db()

        services.resume_monthly_service(actor=manager, enrollment=enrollment)

        paid.refresh_from_db()
        assert paid.status == BillStatus.PAID
        assert paid.outstanding == Decimal("0.00")


TERMINATED_URL = reverse("enrollments:monthly-enrollment-terminated")


def resume_url(enrollment):
    return reverse("enrollments:monthly-enrollment-resume", args=[enrollment.pk])


class TestTerminatedServicesEndpoint:
    """
    The screen a manager reaches from the Due page. Tested at the API
    boundary because the service layer passing is no evidence the screen can
    reach it — a view that drops a field is exactly how a working service
    ends up looking broken.
    """

    def test_it_lists_what_the_screen_needs_to_show(self, manager_client, lapsed):
        row = manager_client.get(TERMINATED_URL).json()["results"][0]

        assert row["patientName"] == "Nusrat Jahan"
        assert row["patientPhone"] == "01712345678"
        assert row["patientCode"]
        assert row["serviceName"] == "Individual Therapy"
        assert row["serviceCode"] == "MON-AT"
        assert Decimal(row["previousDue"]) == Decimal("10000.00")
        assert row["terminatedMonth"] == lapsed.terminated_month
        assert row["terminatedMonthLabel"]
        assert row["terminatedAt"]
        assert row["status"] == EnrollmentStatus.TERMINATED
        # Which cycles the arrears are made of, so the manager can say what
        # they are collecting for.
        assert len(row["unpaidMonths"]) == 2

    def test_a_running_service_is_not_listed(self, manager_client, enrollment):
        assert manager_client.get(TERMINATED_URL).json()["count"] == 0

    def test_a_manually_stopped_service_is_listed_too(
        self, manager_client, manager, enrollment
    ):
        """
        Both kinds describe the same thing to whoever is looking: this
        patient's monthly service is not running. What differs is only what
        resuming costs.
        """
        services.terminate(actor=manager, container=enrollment)

        row = manager_client.get(TERMINATED_URL).json()["results"][0]

        assert row["terminatedKind"] == MonthlyEnrollment.TerminationKind.MANUAL
        # Stopping it already wrote the debt off, so there is nothing left.
        assert Decimal(row["previousDue"]) == Decimal("0.00")

    def test_the_two_kinds_can_be_told_apart(
        self, manager_client, manager, branch, patient_factory, monthly_service, lapsed
    ):
        stopped_by_hand = services.create_monthly_enrollment(
            actor=manager, branch=branch,
            patient=patient_factory(name="Rafiq Islam"), service=monthly_service,
        )
        services.terminate(actor=manager, container=stopped_by_hand)

        everything = manager_client.get(TERMINATED_URL).json()
        automatic = manager_client.get(TERMINATED_URL, {"kind": "unpaid_due"}).json()
        by_hand = manager_client.get(TERMINATED_URL, {"kind": "manual"}).json()

        assert everything["count"] == 2
        assert [row["id"] for row in automatic["results"]] == [lapsed.id]
        assert [row["id"] for row in by_hand["results"]] == [stopped_by_hand.id]

    @pytest.mark.parametrize(
        "term", ["nusrat", "01712", "MON-AT", "Individual"]
    )
    def test_search_finds_it_by_any_of_the_handles_a_manager_has(
        self, manager_client, lapsed, term
    ):
        body = manager_client.get(TERMINATED_URL, {"search": term}).json()
        assert body["count"] == 1

    def test_search_by_patient_code(self, manager_client, lapsed):
        body = manager_client.get(
            TERMINATED_URL, {"search": lapsed.patient.patient_code}
        ).json()
        assert body["count"] == 1

    def test_search_that_matches_nobody_returns_nothing(self, manager_client, lapsed):
        assert manager_client.get(TERMINATED_URL, {"search": "zzz"}).json()["count"] == 0

    def test_filter_by_terminated_month(self, manager_client, lapsed):
        hit = manager_client.get(TERMINATED_URL, {"month": lapsed.terminated_month})
        miss = manager_client.get(TERMINATED_URL, {"month": "2001-01"})

        assert hit.json()["count"] == 1
        assert miss.json()["count"] == 0


@pytest.mark.isolation
class TestTerminatedBranchIsolation:
    def test_a_manager_sees_only_their_own_branchs_terminations(
        self, manager_client, other_manager, other_branch, patient_factory,
        service_factory, lapsed,
    ):
        other = services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            service=service_factory(code="MON-OTHER"),
        )
        lapse(other)

        results = manager_client.get(TERMINATED_URL).json()["results"]

        assert [row["branchId"] for row in results] == [str(lapsed.branch_id)]

    def test_a_manager_cannot_resume_another_branchs_service(
        self, manager_client, other_manager, other_branch, patient_factory,
        service_factory,
    ):
        other = services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            service=service_factory(code="MON-OTHER-2"),
        )
        lapse(other)

        assert manager_client.post(
            resume_url(other), {"carryDue": False}
        ).status_code == 404


@pytest.mark.money
class TestResumeEndpoint:
    def test_reactivating_is_refused_while_anything_is_owed(
        self, manager_client, lapsed
    ):
        response = manager_client.post(resume_url(lapsed), {})

        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "outstanding_dues"
        assert body["total"] == "10000.00"

    def test_reactivating_succeeds_once_the_due_is_cleared(
        self, manager_client, lapsed, settle_dues
    ):
        settle_dues(lapsed.patient)

        response = manager_client.post(resume_url(lapsed), {})

        assert response.status_code == 200
        assert response.json()["status"] == EnrollmentStatus.ACTIVE

    def test_a_kept_due_on_an_inactive_service_is_listed_on_due_payments(
        self, manager_client, lapsed
    ):
        """
        The only route back runs through this screen, so the debt has to be
        reachable from it even though the service is no longer running.
        """
        results = manager_client.get(reverse("duepayments:due-list")).json()["results"]

        assert [row["serviceActive"] for row in results] == [False]

    def test_a_reactivated_service_leaves_the_terminated_list(
        self, manager_client, lapsed, settle_dues
    ):
        settle_dues(lapsed.patient)
        manager_client.post(resume_url(lapsed), {})

        assert manager_client.get(TERMINATED_URL).json()["count"] == 0

    def test_a_reactivated_service_is_collectable_again_from_due_payments(
        self, manager_client, lapsed, settle_dues
    ):
        """The point of reactivating: the patient shows up on the Due page again."""
        settle_dues(lapsed.patient)
        manager_client.post(resume_url(lapsed), {})

        results = manager_client.get(reverse("duepayments:due-list")).json()["results"]

        assert [row["month"] for row in results] == [
            services.month_key(timezone.localdate())
        ]

    def test_admin_can_read_the_list(self, admin_client, lapsed):
        """
        Admin's branch drill-down shows this screen read-only. Listing is a
        read, so it must not fall under the manager-only rule that guards
        collecting and terminating.
        """
        body = admin_client.get(TERMINATED_URL).json()

        assert body["count"] == 1
        assert body["results"][0]["patientName"] == lapsed.patient.name

    def test_admin_cannot_reactivate(self, admin_client, lapsed):
        """Restarting a service is a branch-desk action, like collecting."""
        assert admin_client.post(resume_url(lapsed), {}).status_code == 403
