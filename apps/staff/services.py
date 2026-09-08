"""
Staff HR business logic: roster codes, attendance check-in/out, and bonuses.

Attendance and bonus writes go through here rather than a plain serializer
`.save()` so that "late" is always derived from the check-in time server-side
(a client claiming "present" at 2pm would otherwise be trusted) and so a
bonus is always attributed to the manager who is actually authenticated,
never to a name the client sends.
"""

from collections import defaultdict
from datetime import date, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.staff.models import StaffAttendance, StaffBonus, StaffMember

# Check-ins at or after this hour are "late" rather than "present" — mirrors
# the frontend mock's LATE_AFTER_HOUR so behaviour doesn't change when the
# real endpoint replaces it.
LATE_AFTER_HOUR = 10


@transaction.atomic
def create_staff_member(*, actor, branch, data: dict) -> StaffMember:
    value = next_value(f"staff:{branch.code}", 0)
    member = StaffMember.objects.create(
        staff_code=f"STF-{branch.short_code}-{str(value).zfill(3)}", branch=branch, **data
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=member,
        branch=branch,
        changes={"staffCode": member.staff_code, "name": member.name},
    )
    return member


def _today_status_for_check_in(check_in_at) -> str:
    return (
        StaffAttendance.Status.LATE
        if check_in_at.time() >= time(LATE_AFTER_HOUR, 0)
        else StaffAttendance.Status.PRESENT
    )


@transaction.atomic
def check_in(*, staff: StaffMember) -> StaffAttendance:
    now = timezone.now()
    today = timezone.localdate()

    record, _created = StaffAttendance.objects.select_for_update().get_or_create(
        staff=staff,
        date=today,
        defaults={"branch": staff.branch, "status": StaffAttendance.Status.PRESENT},
    )
    record.check_in_at = now
    record.check_out_at = None
    record.status = _today_status_for_check_in(now)
    record.save(update_fields=["check_in_at", "check_out_at", "status"])
    return record


@transaction.atomic
def check_out(*, staff: StaffMember) -> StaffAttendance:
    today = timezone.localdate()
    record, _created = StaffAttendance.objects.select_for_update().get_or_create(
        staff=staff,
        date=today,
        defaults={
            "branch": staff.branch,
            "status": StaffAttendance.Status.PRESENT,
            "check_in_at": timezone.now(),
        },
    )
    record.check_out_at = timezone.now()
    record.save(update_fields=["check_out_at"])
    return record


@transaction.atomic
def mark_attendance(*, staff: StaffMember, status: str) -> StaffAttendance:
    """Manager override for a day the person didn't check in themselves — on leave or absent."""
    today = timezone.localdate()
    record, _created = StaffAttendance.objects.select_for_update().get_or_create(
        staff=staff, date=today, defaults={"branch": staff.branch, "status": status}
    )
    record.status = status
    record.check_in_at = None
    record.check_out_at = None
    record.save(update_fields=["status", "check_in_at", "check_out_at"])
    return record


@transaction.atomic
def add_bonus(*, actor, staff: StaffMember, amount, reason: str) -> StaffBonus:
    bonus = StaffBonus.objects.create(
        staff=staff, branch=staff.branch, amount=amount, reason=reason, awarded_by=actor
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=bonus,
        branch=staff.branch,
        reason=reason,
        changes={"amount": str(amount), "staff": staff.name},
    )
    return bonus


def _month_range(year: int, month: int) -> tuple[date, date]:
    """[start, end) — end is the 1st of the following month, so date/datetime filters can use a plain `lt`."""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def monthly_report(staff_queryset, *, year: int, month: int) -> list[dict]:
    """
    Per-staff payroll + attendance for one calendar month.

    Three aggregated queries regardless of roster size — not one query per
    metric per staff member — then merged in Python by staff id. Combining
    the bonus sum and the attendance count in a single `annotate()` on the
    same queryset was deliberately avoided: they're two different reverse
    relations, and Django would join both before aggregating, multiplying
    each bonus by every attendance row (and vice versa) for the same staff
    member — silently wrong totals, not just slow.
    """
    staff_list = list(staff_queryset)
    staff_ids = [member.id for member in staff_list]
    start, end = _month_range(year, month)

    bonus_totals = dict(
        StaffBonus.objects.filter(staff_id__in=staff_ids, created_at__date__gte=start, created_at__date__lt=end)
        .values("staff_id")
        .annotate(total=Sum("amount"))
        .values_list("staff_id", "total")
    )

    counts_by_staff: dict[int, dict[str, int]] = defaultdict(dict)
    attendance_counts = (
        StaffAttendance.objects.filter(staff_id__in=staff_ids, date__gte=start, date__lt=end)
        .values("staff_id", "status")
        .annotate(count=Count("id"))
    )
    for row in attendance_counts:
        counts_by_staff[row["staff_id"]][row["status"]] = row["count"]

    rows = []
    for member in staff_list:
        bonus_total = bonus_totals.get(member.id) or Decimal("0.00")
        counts = counts_by_staff.get(member.id, {})
        rows.append(
            {
                "staffId": str(member.id),
                "staffCode": member.staff_code,
                "name": member.name,
                "designation": member.designation,
                "monthlySalary": member.monthly_salary,
                "bonusTotal": bonus_total,
                "netPayable": member.monthly_salary + bonus_total,
                "presentCount": counts.get(StaffAttendance.Status.PRESENT, 0),
                "lateCount": counts.get(StaffAttendance.Status.LATE, 0),
                "absentCount": counts.get(StaffAttendance.Status.ABSENT, 0),
                "leaveCount": counts.get(StaffAttendance.Status.ON_LEAVE, 0),
            }
        )
    return rows
