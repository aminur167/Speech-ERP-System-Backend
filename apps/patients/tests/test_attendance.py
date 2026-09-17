"""
Patient attendance.

Two properties carry the weight here:

  * **attendance never moves money.** Billing is a monthly subscription, not
    a per-visit charge, so a patient who came twice owes exactly what one who
    came ten times owes. `test_marking_attendance_moves_no_money` is the
    guard, and it is the most valuable test in this module.
  * **the alert means "stopped coming without saying so".** An informed
    absence has to hold it off, or the warning fires on the people who did
    exactly what the clinic asked of them.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.duepayments.services import due_summary
from apps.enrollments import services as enrollment_services
from apps.patients import attendance as attendance_services
from apps.patients.models import PatientAttendance
from apps.payments.models import Payment
from apps.services.models import Service

pytestmark = pytest.mark.django_db

ROSTER_URL = reverse("patients:patient-attendance-roster")

Kind = PatientAttendance.ServiceKind
Status = PatientAttendance.Status


def mark_url(patient):
    return reverse("patients:patient-mark-attendance", args=[patient.pk])


def mark_url_by_id(patient_id):
    return reverse("patients:patient-mark-attendance", args=[patient_id])


def history_url(patient):
    return reverse("patients:patient-attendance-history", args=[patient.pk])


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(
        name="Speech Therapy Monthly", code="ATT-M1", category=Service.Category.MONTHLY
    )


@pytest.fixture
def installment_service(service_factory):
    return service_factory(
        name="Autism Care Package", code="ATT-I1",
        category=Service.Category.INSTALLMENT, fee=Decimal("18000.00"),
    )


@pytest.fixture
def monthly_patient(manager, branch, patient_factory, monthly_service):
    patient = patient_factory(name="Nusrat Jahan")
    enrollment_services.create_monthly_enrollment(
        actor=manager, branch=branch, patient=patient, service=monthly_service
    )
    return patient


@pytest.fixture
def installment_patient(manager, branch, patient_factory, installment_service):
    patient = patient_factory(name="Rafiq Islam")
    enrollment_services.create_installment_plan(
        actor=manager, branch=branch, patient=patient,
        service=installment_service, number_of_installments=3,
    )
    return patient


class TestRoster:
    def test_it_lists_patients_with_a_running_service_of_that_kind(
        self, manager_client, monthly_patient, installment_patient
    ):
        monthly = manager_client.get(ROSTER_URL, {"kind": "monthly"}).json()
        installment = manager_client.get(ROSTER_URL, {"kind": "installment"}).json()

        assert [row["patientName"] for row in monthly["results"]] == ["Nusrat Jahan"]
        assert [row["patientName"] for row in installment["results"]] == ["Rafiq Islam"]

    def test_a_patient_with_no_running_service_is_not_on_any_sheet(
        self, manager_client, patient_factory
    ):
        patient_factory(name="Never Enrolled")

        body = manager_client.get(ROSTER_URL, {"kind": "monthly"}).json()

        assert body["count"] == 0

    def test_two_monthly_services_are_one_row_naming_both(
        self, manager_client, manager, branch, monthly_patient, service_factory
    , settle_dues):
        """
        The patient came in or they didn't — asking twice would be asking the
        same question twice. But the row has to say what the one mark covers.
        """
        second = service_factory(
            name="Group Therapy", code="ATT-M2", category=Service.Category.MONTHLY
        )
        # The first service's month has to be settled before a second one can
        # be started — a patient with an unpaid due cannot enroll again.
        settle_dues(monthly_patient)
        enrollment_services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=monthly_patient, service=second
        )

        rows = manager_client.get(ROSTER_URL, {"kind": "monthly"}).json()["results"]

        assert len(rows) == 1
        assert sorted(rows[0]["serviceNames"]) == ["Group Therapy", "Speech Therapy Monthly"]

    def test_a_terminated_service_leaves_the_sheet(
        self, manager_client, manager, monthly_patient
    ):
        from apps.enrollments.models import MonthlyEnrollment

        enrollment = MonthlyEnrollment.objects.get(patient=monthly_patient)
        enrollment_services.terminate(actor=manager, container=enrollment)

        assert manager_client.get(ROSTER_URL, {"kind": "monthly"}).json()["count"] == 0

    def test_unmarked_filter(self, manager_client, monthly_patient, patient_factory,
                             manager, branch, monthly_service):
        other = patient_factory(name="Second Patient")
        enrollment_services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=other, service=monthly_service
        )
        manager_client.post(
            mark_url(monthly_patient), {"serviceKind": "monthly", "status": "present"}
        )

        rows = manager_client.get(
            ROSTER_URL, {"kind": "monthly", "unmarked": "true"}
        ).json()["results"]

        assert [row["patientName"] for row in rows] == ["Second Patient"]

    def test_search_by_name(self, manager_client, monthly_patient):
        found = manager_client.get(ROSTER_URL, {"kind": "monthly", "search": "nusrat"})
        missing = manager_client.get(ROSTER_URL, {"kind": "monthly", "search": "zzz"})

        assert found.json()["count"] == 1
        assert missing.json()["count"] == 0

    def test_an_unknown_kind_is_rejected(self, manager_client):
        assert manager_client.get(ROSTER_URL, {"kind": "weekly"}).status_code == 400


class TestMarking:
    def test_marking_twice_leaves_one_row_with_the_last_answer(
        self, manager_client, monthly_patient
    ):
        manager_client.post(
            mark_url(monthly_patient), {"serviceKind": "monthly", "status": "present"}
        )
        manager_client.post(
            mark_url(monthly_patient), {"serviceKind": "monthly", "status": "absent"}
        )

        records = PatientAttendance.objects.filter(patient=monthly_patient)
        assert records.count() == 1
        assert records.first().status == Status.ABSENT

    def test_the_two_sheets_are_marked_independently(
        self, manager_client, manager, branch, monthly_patient, installment_service
    , settle_dues):
        """
        The same person can be in monthly therapy and paying off a package;
        each is its own attendance question, so each gets its own row.
        """
        settle_dues(monthly_patient)
        enrollment_services.create_installment_plan(
            actor=manager, branch=branch, patient=monthly_patient,
            service=installment_service, number_of_installments=3,
        )

        manager_client.post(
            mark_url(monthly_patient), {"serviceKind": "monthly", "status": "present"}
        )
        manager_client.post(
            mark_url(monthly_patient), {"serviceKind": "installment", "status": "absent"}
        )

        rows = PatientAttendance.objects.filter(patient=monthly_patient)
        assert rows.count() == 2
        assert set(rows.values_list("service_kind", "status")) == {
            ("monthly", "present"),
            ("installment", "absent"),
        }

    def test_it_records_who_marked_it(self, manager_client, manager, monthly_patient):
        manager_client.post(
            mark_url(monthly_patient), {"serviceKind": "monthly", "status": "present"}
        )

        assert PatientAttendance.objects.get(patient=monthly_patient).marked_by == manager

    def test_a_return_date_is_cleared_when_the_status_stops_being_an_absence(
        self, manager_client, monthly_patient
    ):
        """
        Otherwise a stale date goes on silencing the alert for someone who has
        since been marked absent.
        """
        later = (timezone.localdate() + timedelta(days=10)).isoformat()
        manager_client.post(
            mark_url(monthly_patient),
            {"serviceKind": "monthly", "status": "informed_absence", "expectedReturnOn": later},
        )
        manager_client.post(
            mark_url(monthly_patient), {"serviceKind": "monthly", "status": "absent"}
        )

        assert PatientAttendance.objects.get(patient=monthly_patient).expected_return_on is None

    def test_history_is_newest_first(self, manager_client, manager, monthly_patient):
        today = timezone.localdate()
        for offset in range(3):
            attendance_services.mark(
                actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
                status=Status.PRESENT, on=today - timedelta(days=offset),
            )

        rows = manager_client.get(history_url(monthly_patient)).json()

        assert [row["date"] for row in rows] == [
            (today - timedelta(days=offset)).isoformat() for offset in range(3)
        ]


@pytest.mark.money
class TestAttendanceMovesNoMoney:
    def test_marking_attendance_moves_no_money(
        self, manager_client, manager, branch, monthly_patient, installment_patient
    ):
        """
        The invariant the whole feature rests on. Billing is a subscription,
        not a per-visit charge — a patient who came twice owes exactly what
        one who came ten times owes. Enforced by test rather than convention,
        because the day someone wires attendance into billing this is what
        tells them they have changed the deal.
        """
        from apps.enrollments.models import Installment, MonthlyBill

        def snapshot():
            return {
                "due": due_summary(branch_id=branch.id),
                "bills": sorted(
                    MonthlyBill.objects.values_list(
                        "id", "status", "amount", "amount_paid", "paid_at"
                    )
                ),
                "installments": sorted(
                    Installment.objects.values_list(
                        "id", "status", "amount", "amount_paid", "paid_at"
                    )
                ),
                "payments": Payment.all_objects.count(),
            }

        before = snapshot()

        today = timezone.localdate()
        for offset in range(30):
            day = today - timedelta(days=offset)
            for patient, kind in (
                (monthly_patient, Kind.MONTHLY),
                (installment_patient, Kind.INSTALLMENT),
            ):
                attendance_services.mark(
                    actor=manager, patient=patient, kind=kind,
                    status=[Status.PRESENT, Status.ABSENT, Status.INFORMED_ABSENCE][offset % 3],
                    on=day,
                )

        assert snapshot() == before


class TestStoppedComingAlert:
    """
    The clock measures the gap since the patient was last seen, not missed
    sessions — nothing in this system knows who was expected on a given day,
    so an absence count would invent alarms for people who were never due in.
    """

    def _row(self, client, patient):
        rows = client.get(ROSTER_URL, {"kind": "monthly"}).json()["results"]
        return next(row for row in rows if row["patientId"] == str(patient.id))

    def test_a_patient_seen_recently_is_not_flagged(
        self, manager_client, manager, monthly_patient
    ):
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.PRESENT, on=timezone.localdate(),
        )

        row = self._row(manager_client, monthly_patient)
        assert row["alert"] is False
        assert row["daysSinceLastVisit"] == 0

    def test_a_long_gap_is_flagged(self, manager_client, manager, monthly_patient, settings):
        long_ago = timezone.localdate() - timedelta(
            days=settings.PATIENT_ABSENCE_ALERT_DAYS + 1
        )
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.PRESENT, on=long_ago,
        )

        assert self._row(manager_client, monthly_patient)["alert"] is True

    def test_the_threshold_is_exact_not_off_by_one(
        self, manager_client, manager, monthly_patient, settings
    ):
        on_the_line = timezone.localdate() - timedelta(
            days=settings.PATIENT_ABSENCE_ALERT_DAYS
        )
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.PRESENT, on=on_the_line,
        )

        row = self._row(manager_client, monthly_patient)
        assert row["daysSinceLastVisit"] == settings.PATIENT_ABSENCE_ALERT_DAYS
        assert row["alert"] is True

    def test_saying_so_holds_the_alert_off(
        self, manager_client, manager, monthly_patient, settings
    ):
        """
        The distinction the whole feature turns on: stopped coming *without
        saying so*. A patient who told the clinic they'd be away is not the
        one the manager needs to chase.
        """
        long_ago = timezone.localdate() - timedelta(
            days=settings.PATIENT_ABSENCE_ALERT_DAYS + 5
        )
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.PRESENT, on=long_ago,
        )
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.INFORMED_ABSENCE, on=long_ago + timedelta(days=1),
            expected_return_on=timezone.localdate() + timedelta(days=3),
        )

        row = self._row(manager_client, monthly_patient)
        assert row["alert"] is False
        assert row["excusedUntil"] is not None

    def test_the_alert_returns_once_the_stated_date_has_passed(
        self, manager_client, manager, monthly_patient, settings
    ):
        long_ago = timezone.localdate() - timedelta(
            days=settings.PATIENT_ABSENCE_ALERT_DAYS + 5
        )
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.PRESENT, on=long_ago,
        )
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.INFORMED_ABSENCE, on=long_ago + timedelta(days=1),
            expected_return_on=timezone.localdate() - timedelta(days=1),
        )

        assert self._row(manager_client, monthly_patient)["alert"] is True

    def test_a_patient_who_never_attended_counts_from_their_start_date(
        self, manager_client, manager, branch, patient_factory, monthly_service, settings
    ):
        """
        Someone who enrolled a month ago and never once came is the most
        urgent case on the sheet, not an invisible one.
        """
        from apps.enrollments.models import MonthlyEnrollment

        patient = patient_factory(name="Never Came")
        enrollment = enrollment_services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient, service=monthly_service
        )
        MonthlyEnrollment.objects.filter(pk=enrollment.pk).update(
            created_at=timezone.now()
            - timedelta(days=settings.PATIENT_ABSENCE_ALERT_DAYS + 3)
        )

        rows = manager_client.get(
            ROSTER_URL, {"kind": "monthly", "alerts": "true"}
        ).json()["results"]

        assert [row["patientName"] for row in rows] == ["Never Came"]

    def test_attendance_on_the_other_sheet_does_not_reset_this_clock(
        self, manager_client, manager, branch, monthly_patient, installment_service, settings
    , settle_dues):
        """Coming in for the package doesn't mean they showed up for therapy."""
        settle_dues(monthly_patient)
        enrollment_services.create_installment_plan(
            actor=manager, branch=branch, patient=monthly_patient,
            service=installment_service, number_of_installments=3,
        )
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.PRESENT,
            on=timezone.localdate() - timedelta(days=settings.PATIENT_ABSENCE_ALERT_DAYS + 1),
        )
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.INSTALLMENT,
            status=Status.PRESENT, on=timezone.localdate(),
        )

        assert self._row(manager_client, monthly_patient)["alert"] is True


@pytest.mark.isolation
class TestAttendanceAccess:
    def test_a_manager_sees_only_their_own_branchs_sheet(
        self, manager_client, other_manager, other_branch, patient_factory,
        service_factory, monthly_patient,
    ):
        other_patient = patient_factory(name="Other Branch", branch=other_branch)
        enrollment_services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch, patient=other_patient,
            service=service_factory(
                name="Other Monthly", code="ATT-OTHER",
                category=Service.Category.MONTHLY, branch=other_branch,
            ),
        )

        rows = manager_client.get(ROSTER_URL, {"kind": "monthly"}).json()["results"]

        assert [row["patientName"] for row in rows] == ["Nusrat Jahan"]

    def test_a_manager_cannot_mark_another_branchs_patient(
        self, manager_client, other_branch, patient_factory
    ):
        theirs = patient_factory(name="Theirs", branch=other_branch)

        response = manager_client.post(
            mark_url(theirs), {"serviceKind": "monthly", "status": "present"}
        )

        assert response.status_code == 404

    def test_admin_can_read_the_sheet_but_not_mark_it(
        self, admin_client, monthly_patient
    ):
        """Marking attendance is a branch-desk action, like taking payment."""
        assert admin_client.get(ROSTER_URL, {"kind": "monthly"}).status_code == 200
        assert (
            admin_client.post(
                mark_url(monthly_patient), {"serviceKind": "monthly", "status": "present"}
            ).status_code
            == 403
        )


class TestRosterCounts:
    """
    The filters are derived, not columns, so they have to be applied before
    paging. Filtering a database page instead would report the unfiltered
    count and hand back a short page while matches sat on later ones.
    """

    def test_the_unmarked_count_drops_as_patients_are_marked(
        self, manager_client, manager, branch, monthly_service, patient_factory
    ):
        for name in ("A", "B", "C"):
            patient = patient_factory(name=name)
            enrollment_services.create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient, service=monthly_service
            )

        def unmarked_count():
            return manager_client.get(
                ROSTER_URL, {"kind": "monthly", "unmarked": "true"}
            ).json()["count"]

        assert unmarked_count() == 3

        first = manager_client.get(ROSTER_URL, {"kind": "monthly"}).json()["results"][0]
        manager_client.post(
            mark_url_by_id(first["patientId"]),
            {"serviceKind": "monthly", "status": "present"},
        )

        assert unmarked_count() == 2

    def test_the_alert_count_matches_the_rows_returned(
        self, manager_client, manager, branch, monthly_service, patient_factory, settings
    ):
        stale = timezone.localdate() - timedelta(
            days=settings.PATIENT_ABSENCE_ALERT_DAYS + 2
        )
        for index, name in enumerate(("Gone", "Also Gone", "Still Coming")):
            patient = patient_factory(name=name)
            enrollment_services.create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient, service=monthly_service
            )
            attendance_services.mark(
                actor=manager, patient=patient, kind=Kind.MONTHLY,
                status=Status.PRESENT,
                on=stale if index < 2 else timezone.localdate(),
            )

        body = manager_client.get(
            ROSTER_URL, {"kind": "monthly", "alerts": "true"}
        ).json()

        assert body["count"] == 2
        assert len(body["results"]) == 2

    def test_paging_a_filtered_sheet_is_consistent(
        self, manager_client, manager, branch, monthly_service, patient_factory
    ):
        for index in range(5):
            patient = patient_factory(name=f"Patient {index}")
            enrollment_services.create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient, service=monthly_service
            )

        first = manager_client.get(
            ROSTER_URL, {"kind": "monthly", "unmarked": "true", "pageSize": 2}
        ).json()
        second = manager_client.get(
            ROSTER_URL,
            {"kind": "monthly", "unmarked": "true", "pageSize": 2, "page": 2},
        ).json()

        assert first["count"] == second["count"] == 5
        assert len(first["results"]) == len(second["results"]) == 2
        # No overlap between pages.
        assert {row["patientId"] for row in first["results"]}.isdisjoint(
            {row["patientId"] for row in second["results"]}
        )


class TestTheSheetBelongsToItsOwnDate:
    """
    A back-dated sheet has to answer as of that day.

    The bug this class exists to keep dead: the roster was assembled from
    `status=ACTIVE`, which is a question about *now*. Filtering to a day two
    months back therefore returned patients who had only enrolled last week —
    people listed on days that predate them — along with visits and absence
    notices that had not happened yet.
    """

    def backdate(self, enrollment, *, days):
        from apps.enrollments.models import MonthlyEnrollment

        MonthlyEnrollment.objects.filter(pk=enrollment.pk).update(
            created_at=timezone.now() - timedelta(days=days)
        )

    def enrollment_of(self, patient):
        from apps.enrollments.models import MonthlyEnrollment

        return MonthlyEnrollment.objects.get(patient=patient)

    def sheet_on(self, client, day):
        return client.get(
            ROSTER_URL, {"kind": "monthly", "date": day.isoformat()}
        ).json()

    def test_a_patient_is_absent_from_days_before_they_enrolled(
        self, manager_client, monthly_patient
    ):
        """
        The complaint that started this: enrolled a month ago, yet showing up
        on a sheet from two months back.
        """
        self.backdate(self.enrollment_of(monthly_patient), days=30)
        today = timezone.localdate()

        assert self.sheet_on(manager_client, today - timedelta(days=60))["count"] == 0
        assert self.sheet_on(manager_client, today - timedelta(days=20))["count"] == 1

    def test_the_first_day_on_the_sheet_is_the_day_they_enrolled(
        self, manager_client, monthly_patient
    ):
        """
        Inclusive at the bottom: a service that starts today is attendable
        today, not from tomorrow.
        """
        self.backdate(self.enrollment_of(monthly_patient), days=10)
        started = timezone.localdate() - timedelta(days=10)

        assert self.sheet_on(manager_client, started)["count"] == 1
        assert self.sheet_on(manager_client, started - timedelta(days=1))["count"] == 0

    def test_a_stopped_service_stays_on_the_days_it_was_running(
        self, manager_client, manager, monthly_patient
    ):
        """
        Termination is not retroactive. The patient leaves today's sheet, but
        last month's sheet is a record of what happened and must not change
        because of a decision taken afterwards.
        """
        enrollment = self.enrollment_of(monthly_patient)
        self.backdate(enrollment, days=30)
        enrollment_services.terminate(actor=manager, container=enrollment)
        today = timezone.localdate()

        assert self.sheet_on(manager_client, today)["count"] == 0
        assert self.sheet_on(manager_client, today - timedelta(days=15))["count"] == 1
        assert self.sheet_on(manager_client, today - timedelta(days=40))["count"] == 0

    def test_a_service_stopped_today_is_off_todays_sheet_only(
        self, manager_client, manager, monthly_patient
    ):
        """
        The window is half-open: the day it goes inactive is the first day off
        the sheet.
        """
        enrollment = self.enrollment_of(monthly_patient)
        self.backdate(enrollment, days=5)
        enrollment_services.terminate(actor=manager, container=enrollment)
        today = timezone.localdate()

        assert self.sheet_on(manager_client, today)["count"] == 0
        assert self.sheet_on(manager_client, today - timedelta(days=1))["count"] == 1

    def test_a_past_sheet_does_not_report_a_visit_from_after_it(
        self, manager_client, manager, monthly_patient
    ):
        """
        Last seen on the 1st cannot be the 10th. Reporting it that way also
        silences the gap clock on exactly the days it was meant to ring.
        """
        self.backdate(self.enrollment_of(monthly_patient), days=30)
        today = timezone.localdate()
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.PRESENT, on=today,
        )

        row = self.sheet_on(manager_client, today - timedelta(days=10))["results"][0]

        assert row["lastPresentOn"] is None

    def test_a_notice_given_later_does_not_excuse_an_earlier_day(
        self, manager_client, manager, monthly_patient
    ):
        """A back-dated sheet is answered with what the clinic knew that day."""
        self.backdate(self.enrollment_of(monthly_patient), days=60)
        today = timezone.localdate()
        attendance_services.mark(
            actor=manager, patient=monthly_patient, kind=Kind.MONTHLY,
            status=Status.INFORMED_ABSENCE, on=today,
            expected_return_on=today + timedelta(days=30),
        )

        earlier = self.sheet_on(manager_client, today - timedelta(days=10))["results"][0]
        now = self.sheet_on(manager_client, today)["results"][0]

        assert earlier["excusedUntil"] is None
        assert now["excusedUntil"] is not None

    def test_a_service_that_had_not_started_cannot_be_marked(
        self, manager_client, monthly_patient
    ):
        """
        The roster no longer offers the row, and the write refuses it too: the
        screen is not the last line of defence for a rule the data has to keep.
        """
        response = manager_client.post(
            mark_url(monthly_patient),
            {
                "serviceKind": "monthly",
                "status": "present",
                "date": (timezone.localdate() - timedelta(days=30)).isoformat(),
            },
        )

        assert response.status_code == 400
        assert not PatientAttendance.objects.filter(patient=monthly_patient).exists()

    def test_attendance_cannot_be_marked_ahead_of_time(
        self, manager_client, monthly_patient
    ):
        response = manager_client.post(
            mark_url(monthly_patient),
            {
                "serviceKind": "monthly",
                "status": "present",
                "date": (timezone.localdate() + timedelta(days=1)).isoformat(),
            },
        )

        assert response.status_code == 400


class TestAbsentByDefault:
    """
    Absent is the resting state; present is the assertion.

    The manager marks who walked in, and everyone left over did not come. That
    is the row's `status`, not a blank cell the reader has to interpret — and
    it has to stay reversible, because the person most likely to need
    correcting is the one marked present by mistake.
    """

    def row_for(self, client, patient):
        rows = client.get(ROSTER_URL, {"kind": "monthly"}).json()["results"]
        return next(row for row in rows if row["patientId"] == str(patient.id))

    def test_an_untouched_row_reads_absent(self, manager_client, monthly_patient):
        row = self.row_for(manager_client, monthly_patient)

        assert row["status"] == "absent"
        # Still distinguishable from a deliberate absent mark, which is the
        # question the not-yet-marked filter asks.
        assert row["record"] is None

    def test_marking_present_changes_the_status(self, manager_client, monthly_patient):
        manager_client.post(
            mark_url(monthly_patient), {"serviceKind": "monthly", "status": "present"}
        )

        assert self.row_for(manager_client, monthly_patient)["status"] == "present"

    def test_present_can_be_toggled_back_to_absent(
        self, manager_client, monthly_patient
    ):
        """
        One row holding the last answer, not a present row and a contradicting
        absent one.
        """
        for status_value in ("present", "absent"):
            manager_client.post(
                mark_url(monthly_patient),
                {"serviceKind": "monthly", "status": status_value},
            )

        row = self.row_for(manager_client, monthly_patient)

        assert row["status"] == "absent"
        assert row["record"]["status"] == "absent"
        assert PatientAttendance.objects.filter(patient=monthly_patient).count() == 1

    def test_an_informed_absence_is_not_folded_into_a_plain_absence(
        self, manager_client, monthly_patient
    ):
        manager_client.post(
            mark_url(monthly_patient),
            {"serviceKind": "monthly", "status": "informed_absence"},
        )

        row = self.row_for(manager_client, monthly_patient)

        assert row["status"] == "informed_absence"
