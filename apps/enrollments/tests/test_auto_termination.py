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
        # Now let the *next* month lapse instead.
        second = months_of(enrollment)[1]
        year, month = (int(part) for part in second.split("-"))
        services.terminate_unpaid_monthly_services(
            on=services.add_months(date(year, month, 1), 1)
        )

        paid.refresh_from_db()
        assert paid.status == BillStatus.PAID
        assert paid.payment is not None

    def test_the_unbilled_lookahead_is_dropped(self, enrollment):
        """
        The months after the one that ended it were never payable and never
        will be. Left behind they would be charged as "previous due" for
        service nobody delivered.
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
    """
    start = services.add_months(timezone.localdate().replace(day=1), -months_back)

    for offset, bill in enumerate(enrollment.bills.order_by("month")):
        month_date = services.add_months(start, offset)
        bill.month = services.month_key(month_date)
        bill.label = services.month_label(month_date)
        bill.due_date = due_date_for_month(bill.month)
        # The past months genuinely fell due; anything from today on is still
        # a lookahead placeholder.
        bill.status = BillStatus.DUE if offset < due_months else BillStatus.UPCOMING
        bill.save()

    services.terminate_unpaid_monthly_services()
    enrollment.refresh_from_db()
    return enrollment


@pytest.fixture
def lapsed(enrollment):
    """A patient who stopped paying two months ago, with the job catching up today."""
    return lapse(enrollment)


@pytest.mark.money
class TestResume:
    def test_the_two_lapsed_months_are_what_is_owed(self, lapsed):
        assert lapsed.status == EnrollmentStatus.TERMINATED
        assert lapsed.unpaid_bills().count() == 2
        assert lapsed.outstanding_total() == Decimal("10000.00")

    def test_resuming_with_the_due_collects_every_unpaid_month(self, manager, lapsed):
        enrollment, payments = services.resume_monthly_service(
            actor=manager, enrollment=lapsed, carry_due=True, method="cash"
        )

        assert enrollment.status == EnrollmentStatus.ACTIVE
        # Two months owed, two receipts, each naming its own month —
        # collapsing them into one undated payment would lose which cycle was
        # settled.
        assert len(payments) == 2
        assert sum(payment.amount for payment in payments) == Decimal("10000.00")
        assert not enrollment.bills.filter(status=BillStatus.WRITTEN_OFF).exists()

    def test_resuming_without_the_due_writes_it_off_explicitly(self, manager, lapsed):
        """Waived, never quietly dropped — the same WRITTEN_OFF an admin sees."""
        enrollment, payments = services.resume_monthly_service(
            actor=manager, enrollment=lapsed, carry_due=False
        )

        assert payments == []
        assert enrollment.status == EnrollmentStatus.ACTIVE
        assert enrollment.bills.filter(status=BillStatus.WRITTEN_OFF).count() == 2

    def test_the_waived_amount_is_recorded_in_the_audit_log(self, manager, lapsed):
        from apps.common.models import AuditLog

        services.resume_monthly_service(
            actor=manager, enrollment=lapsed, carry_due=False
        )

        entry = AuditLog.objects.filter(
            target_type="MonthlyEnrollment", action=AuditLog.Action.WRITE_OFF
        ).latest("created_at")
        assert entry.changes["writtenOff"] == "10000.00"

    def test_the_new_cycle_starts_at_the_current_month(self, manager, lapsed):
        enrollment, _ = services.resume_monthly_service(
            actor=manager, enrollment=lapsed, carry_due=False
        )

        payable = enrollment.oldest_unpaid_bill()
        assert payable is not None
        assert payable.month == services.month_key(timezone.localdate())

    def test_the_arrears_are_not_re_billed_as_part_of_the_new_cycle(
        self, manager, lapsed
    ):
        """
        Settling the arrears must not leave them looking payable again — the
        previous due and the new cycle stay separate sums.
        """
        enrollment, _ = services.resume_monthly_service(
            actor=manager, enrollment=lapsed, carry_due=True, method="cash"
        )

        current = services.month_key(timezone.localdate())
        payable = enrollment.oldest_unpaid_bill()

        # Nothing before the new cycle is owed any more, and what is payable
        # is one month's fee — not the arrears coming back around.
        assert not enrollment.unpaid_bills().filter(month__lt=current).exists()
        assert payable.month == current
        assert payable.outstanding == Decimal("5000.00")

    def test_the_gap_month_is_never_billed(self, manager, enrollment):
        """
        Stopped after one lapsed month, resumed later: the months in between
        had no service and must not appear as bills.
        """
        today = timezone.localdate()
        start = services.add_months(today.replace(day=1), -3)
        lapse(enrollment, months_back=3, due_months=1)

        resumed, _ = services.resume_monthly_service(
            actor=manager, enrollment=enrollment, carry_due=False
        )

        lapsed_month = services.month_key(start)
        current = services.month_key(today)
        gap = [m for m in months_of(resumed) if lapsed_month < m < current]
        assert gap == []

    def test_resuming_twice_is_refused(self, manager, lapsed):
        services.resume_monthly_service(actor=manager, enrollment=lapsed, carry_due=False)
        lapsed.refresh_from_db()

        with pytest.raises(services.EnrollmentError) as caught:
            services.resume_monthly_service(
                actor=manager, enrollment=lapsed, carry_due=False
            )

        assert caught.value.code == "not_terminated"

    def test_a_manually_stopped_service_resumes_with_nothing_to_collect(
        self, manager, enrollment
    ):
        """Stopping it by hand already forgave the debt, so resuming is free."""
        services.terminate(actor=manager, container=enrollment)
        enrollment.refresh_from_db()

        resumed, payments = services.resume_monthly_service(
            actor=manager, enrollment=enrollment, carry_due=True, method="cash"
        )

        assert resumed.status == EnrollmentStatus.ACTIVE
        assert payments == []

    def test_a_manually_stopped_service_is_billable_again_once_resumed(
        self, manager, enrollment
    ):
        """
        The trap: stopping by hand writes off the lookahead months too, so a
        naive resume finds those rows already there and creates nothing —
        leaving the service active and quietly never invoicing again.
        """
        services.terminate(actor=manager, container=enrollment)
        enrollment.refresh_from_db()

        resumed, _ = services.resume_monthly_service(
            actor=manager, enrollment=enrollment, carry_due=False
        )

        payable = resumed.oldest_unpaid_bill()
        assert payable is not None
        assert payable.month == services.month_key(timezone.localdate())
        assert payable.outstanding == Decimal("5000.00")

    def test_reopening_a_written_off_month_is_recorded(self, manager, enrollment):
        """A write-off being undone is never allowed to be silent."""
        from apps.common.models import AuditLog

        services.terminate(actor=manager, container=enrollment)
        enrollment.refresh_from_db()
        services.resume_monthly_service(
            actor=manager, enrollment=enrollment, carry_due=False
        )

        entry = AuditLog.objects.filter(
            target_type="MonthlyEnrollment", action=AuditLog.Action.UPDATE
        ).latest("created_at")
        assert entry.changes["reinstatedMonths"]

    def test_an_already_paid_month_is_not_billed_twice_on_resume(
        self, manager, branch, enrollment
    ):
        """Reinstating must never reach a month the patient already settled."""
        paid = enrollment.oldest_unpaid_bill()
        services.collect_bill_payment(
            actor=manager, branch=branch, bill=paid, method="cash"
        )
        services.terminate(actor=manager, container=enrollment)
        enrollment.refresh_from_db()

        services.resume_monthly_service(
            actor=manager, enrollment=enrollment, carry_due=False
        )

        paid.refresh_from_db()
        assert paid.status == BillStatus.PAID
        assert paid.outstanding == Decimal("0.00")

    def test_settling_the_due_needs_a_payment_method(self, manager, lapsed):
        with pytest.raises(services.EnrollmentError) as caught:
            services.resume_monthly_service(
                actor=manager, enrollment=lapsed, carry_due=True
            )

        assert caught.value.code == "method_required"

    def test_a_failed_collection_leaves_the_service_terminated(
        self, manager, lapsed, monkeypatch
    ):
        """
        Never active with the arrears still unpaid: the whole resume is one
        transaction, so a collection that blows up takes the reopening with it.
        """
        def boom(*args, **kwargs):
            raise RuntimeError("gateway down")

        monkeypatch.setattr(services, "collect_bill_payment", boom)

        with pytest.raises(RuntimeError):
            services.resume_monthly_service(
                actor=manager, enrollment=lapsed, carry_due=True, method="cash"
            )

        lapsed.refresh_from_db()
        assert lapsed.status == EnrollmentStatus.TERMINATED
        assert lapsed.outstanding_total() == Decimal("10000.00")


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
    def test_resuming_with_the_due_returns_the_receipts_it_took(
        self, manager_client, lapsed
    ):
        response = manager_client.post(
            resume_url(lapsed), {"carryDue": True, "method": "cash"}
        )
        body = response.json()

        assert response.status_code == 200
        assert body["enrollment"]["status"] == EnrollmentStatus.ACTIVE
        assert len(body["payments"]) == 2
        assert all(payment["receiptNumber"] for payment in body["payments"])

    def test_resuming_without_the_due_takes_no_money(self, manager_client, lapsed):
        body = manager_client.post(resume_url(lapsed), {"carryDue": False}).json()

        assert body["enrollment"]["status"] == EnrollmentStatus.ACTIVE
        assert body["payments"] == []

    def test_settling_without_a_method_is_refused_with_a_readable_reason(
        self, manager_client, lapsed
    ):
        response = manager_client.post(resume_url(lapsed), {"carryDue": True})

        assert response.status_code == 400
        assert response.json()["code"] == "method_required"

    def test_a_resumed_service_leaves_the_terminated_list(self, manager_client, lapsed):
        manager_client.post(resume_url(lapsed), {"carryDue": False})

        assert manager_client.get(TERMINATED_URL).json()["count"] == 0

    def test_a_resumed_service_is_collectable_again_from_due_payments(
        self, manager_client, lapsed
    ):
        """The point of resuming: the patient shows up on the Due page again."""
        manager_client.post(resume_url(lapsed), {"carryDue": False})

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

    def test_admin_cannot_resume(self, admin_client, lapsed):
        """Restarting a service is a branch-desk action, like collecting."""
        assert admin_client.post(resume_url(lapsed), {"carryDue": False}).status_code == 403
