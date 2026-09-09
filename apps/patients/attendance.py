"""
Patient attendance — who came in today, and who has quietly stopped coming.

**This module imports no money code, and that is load-bearing rather than
tidiness.** Attendance must never change what a patient owes: billing here is
a monthly subscription, not a per-visit charge, so a patient who attended
twice owes exactly what a patient who attended ten times owes. Keeping the
import graph clean is what makes that claim checkable by reading the file
instead of by hoping. Enrollment *models* are imported to answer "who is on
the roster"; `apps.enrollments.services` and `apps.payments` are not.

**The warning is a gap clock, not an absence count.** The system has no
session schedule — no weekday, no session count, nothing that knows a patient
was expected on Tuesday. So "absent" cannot be computed, and any attempt
would generate false alarms for patients who were never due in. What can be
computed honestly is *how long since we last saw them*, which is the question
the clinic actually asks. An `informed_absence` pauses that clock, which is
what makes the alert mean "stopped coming **without saying so**".
"""

from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.enrollments.models import EnrollmentStatus, InstallmentPlan, MonthlyEnrollment
from apps.patients.models import Patient, PatientAttendance

Kind = PatientAttendance.ServiceKind


def _enrollment_model(kind: str):
    return MonthlyEnrollment if kind == Kind.MONTHLY else InstallmentPlan


def roster_patient_ids(*, kind: str, branch_id=None):
    """
    Patients with an active service of this kind — the day's sheet.

    Deliberately not "every patient": someone with no running service has
    nothing to attend, and padding the sheet with them would bury the people
    the manager is actually looking for.
    """
    enrollments = _enrollment_model(kind).objects.filter(status=EnrollmentStatus.ACTIVE)
    if branch_id:
        enrollments = enrollments.filter(branch_id=branch_id)
    return enrollments.values_list("patient_id", flat=True).distinct()


def service_names_by_patient(patient_ids, *, kind: str) -> dict[int, list[str]]:
    """
    Every active service name per patient, not just one.

    The directory's equivalent keeps only the newest per patient because it
    describes a patient in one phrase. Here the row has to show all of them:
    a patient holding two monthly services is marked once, and the manager
    needs to see which two that single mark covers.
    """
    rows = (
        _enrollment_model(kind)
        .objects.filter(patient_id__in=patient_ids, status=EnrollmentStatus.ACTIVE)
        .select_related("service")
        .order_by("patient_id", "service__name")
        .values_list("patient_id", "service__name")
    )
    found: dict[int, list[str]] = defaultdict(list)
    for patient_id, service_name in rows:
        if service_name not in found[patient_id]:
            found[patient_id].append(service_name)
    return found


def records_by_patient(patient_ids, *, kind: str, on: date) -> dict[int, PatientAttendance]:
    """That day's marks, keyed by patient. A missing key means not yet marked."""
    records = PatientAttendance.objects.filter(
        patient_id__in=patient_ids, service_kind=kind, date=on
    )
    return {record.patient_id: record for record in records}


def last_present_by_patient(patient_ids, *, kind: str) -> dict[int, date]:
    """One grouped pass rather than a query per patient — this is a page read."""
    rows = (
        PatientAttendance.objects.filter(
            patient_id__in=patient_ids,
            service_kind=kind,
            status=PatientAttendance.Status.PRESENT,
        )
        .values("patient_id")
        .annotate(last_seen=Max("date"))
    )
    return {row["patient_id"]: row["last_seen"] for row in rows}


def open_informed_absences(patient_ids, *, kind: str, on: date) -> dict[int, date]:
    """
    Per patient, the date their stated absence runs until.

    A stated return date wins; without one, a grace window from the day they
    told us. Only the most recent notice counts — someone who said "back in a
    week" and then "back tomorrow" is describing tomorrow.
    """
    grace = timedelta(days=settings.PATIENT_ABSENCE_GRACE_DAYS)
    rows = (
        PatientAttendance.objects.filter(
            patient_id__in=patient_ids,
            service_kind=kind,
            status=PatientAttendance.Status.INFORMED_ABSENCE,
        )
        .order_by("patient_id", "-date")
        .values_list("patient_id", "date", "expected_return_on")
    )

    until: dict[int, date] = {}
    for patient_id, told_on, expected_return in rows:
        if patient_id in until:
            continue  # newest notice per patient wins
        until[patient_id] = expected_return or (told_on + grace)
    return {pid: day for pid, day in until.items() if day >= on}


def days_since_last_visit(last_present: date | None, *, joined_on: date, on: date) -> int:
    """
    How long since we last saw them.

    A patient who has *never* attended counts from the day their service
    started, rather than being excused for having no record — someone who
    enrolled a month ago and never once came is the most urgent case on the
    sheet, not an invisible one.
    """
    return (on - (last_present or joined_on)).days


@transaction.atomic
def mark(
    *, actor, patient: Patient, kind: str, status: str,
    on: date | None = None, note: str = "", expected_return_on: date | None = None,
) -> PatientAttendance:
    """
    Record one patient's attendance for one day — an upsert, not an append.

    `get_or_create` under a row lock on the unique key, so pressing the button
    twice, or two managers marking the same person at once, leaves one row
    with the last answer rather than a duplicate the constraint would reject.
    """
    day = on or timezone.localdate()

    record, _created = PatientAttendance.objects.select_for_update().get_or_create(
        patient=patient,
        service_kind=kind,
        date=day,
        defaults={"branch": patient.branch, "status": status},
    )

    record.status = status
    record.note = note
    # Only an informed absence carries a return date; clearing it on every
    # other status stops a stale date from silencing the alert for someone
    # who has since been marked absent.
    record.expected_return_on = (
        expected_return_on if status == PatientAttendance.Status.INFORMED_ABSENCE else None
    )
    record.marked_by = actor if actor and actor.is_authenticated else None
    record.save(
        update_fields=["status", "note", "expected_return_on", "marked_by", "updated_at"]
    )
    return record


def build_roster(*, patients, kind: str, on: date, branch_id=None) -> list[dict]:
    """
    One page of the day's sheet.

    Assembled in a fixed number of queries regardless of page size, the same
    discipline `directory.py` documents: four grouped lookups keyed by patient
    id, then dictionary hits per row. Adding a patient to the page must not
    add a query.
    """
    ids = [patient.id for patient in patients]
    services = service_names_by_patient(ids, kind=kind)
    today_records = records_by_patient(ids, kind=kind, on=on)
    last_present = last_present_by_patient(ids, kind=kind)
    excused_until = open_informed_absences(ids, kind=kind, on=on)
    started = _service_started_by_patient(ids, kind=kind)

    alert_after = settings.PATIENT_ABSENCE_ALERT_DAYS

    rows = []
    for patient in patients:
        seen_on = last_present.get(patient.id)
        gap = days_since_last_visit(
            seen_on, joined_on=started.get(patient.id, on), on=on
        )
        excused = excused_until.get(patient.id)

        rows.append(
            {
                "patientId": str(patient.id),
                "patientCode": patient.patient_code,
                "patientName": patient.name,
                "patientPhone": patient.phone,
                "branchId": str(patient.branch_id),
                "serviceNames": services.get(patient.id, []),
                "record": today_records.get(patient.id),
                "lastPresentOn": seen_on,
                "daysSinceLastVisit": gap,
                "excusedUntil": excused,
                # Stopped coming *without saying so* — a stated absence holds
                # the flag off until the day they said they would be back.
                "alert": excused is None and gap >= alert_after,
            }
        )
    return rows


def _service_started_by_patient(patient_ids, *, kind: str) -> dict[int, date]:
    """When each patient's oldest active service of this kind began."""
    from django.db.models import Min

    rows = (
        _enrollment_model(kind)
        .objects.filter(patient_id__in=patient_ids, status=EnrollmentStatus.ACTIVE)
        .values("patient_id")
        .annotate(started=Min("created_at"))
    )
    return {row["patient_id"]: timezone.localtime(row["started"]).date() for row in rows}
