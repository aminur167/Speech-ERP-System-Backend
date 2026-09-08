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
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.notifications.inapp import notify, notify_many
from apps.staff.models import SalaryPayment, StaffAttendance, StaffBonus, StaffMember

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


class SalaryPaymentError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


# A request already at one of these is "live" — a second request for the
# same staff member and month would either duplicate a pending ask or pay
# twice, so it's blocked until this one is resolved.
_ACTIVE_SALARY_PAYMENT_STATUSES = [
    SalaryPayment.Status.PENDING_APPROVAL,
    SalaryPayment.Status.APPROVED,
    SalaryPayment.Status.PAID,
]


@transaction.atomic
def request_salary_payment(*, actor, staff: StaffMember, month: str) -> SalaryPayment:
    """
    A Manager's ask to pay one staff member for one month — nothing moves
    until Admin approves it (see `review_salary_payment`).

    The amount is never taken from the client: it's this month's net payable
    (base salary plus any bonus already awarded), computed the same way the
    monthly report computes it, and frozen onto the request at the moment
    it's made.
    """
    # Locks any existing row for this staff+month first, so two concurrent
    # requests can't both see "nothing active yet" and both insert.
    existing = (
        SalaryPayment.objects.select_for_update()
        .filter(staff=staff, month=month, status__in=_ACTIVE_SALARY_PAYMENT_STATUSES)
        .first()
    )
    if existing is not None:
        raise SalaryPaymentError(
            f"A salary payment for {month} is already {existing.get_status_display().lower()}.",
            code="duplicate",
        )

    year, month_number = (int(part) for part in month.split("-"))
    rows = monthly_report([staff], year=year, month=month_number)
    amount = rows[0]["netPayable"] if rows else staff.monthly_salary

    payment = SalaryPayment.objects.create(
        staff=staff, branch=staff.branch, month=month, amount=amount, requested_by=actor,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=payment,
        branch=staff.branch,
        changes={"amount": str(amount), "month": month, "staff": staff.name},
    )

    from apps.accounts.models import User

    notify_many(
        recipients=User.objects.filter(role=User.Role.ADMIN),
        title="New salary payment request",
        message=(
            f"{staff.branch.name} requested BDT {amount} for {staff.name}'s "
            f"salary ({month})."
        ),
        link="/admin/salary-approvals",
    )
    return payment


@transaction.atomic
def review_salary_payment(
    *, actor, payment: SalaryPayment, approve: bool, review_note: str = ""
) -> SalaryPayment:
    """Admin approves or rejects a pending request. Rejecting requires a reason — the manager needs to know why before trying again."""
    if payment.status != SalaryPayment.Status.PENDING_APPROVAL:
        raise SalaryPaymentError(
            f"This request is already {payment.get_status_display().lower()}.", code="no_change"
        )
    if not approve and not review_note.strip():
        raise SalaryPaymentError(
            "A reason is required when rejecting a salary payment.", code="note_required"
        )

    payment.status = SalaryPayment.Status.APPROVED if approve else SalaryPayment.Status.REJECTED
    payment.review_note = review_note
    payment.reviewed_by = actor
    payment.reviewed_at = timezone.now()
    payment.save(update_fields=["status", "review_note", "reviewed_by", "reviewed_at"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.APPROVE if approve else AuditLog.Action.REJECT,
        target=payment,
        branch=payment.branch,
        reason=review_note,
        changes={"status": {"from": SalaryPayment.Status.PENDING_APPROVAL, "to": payment.status}},
    )

    if payment.requested_by_id:
        if approve:
            title = "Salary payment approved"
            message = f"{payment.staff.name}'s salary for {payment.month} was approved — you can pay it out now."
        else:
            title = "Salary payment rejected"
            message = f"{payment.staff.name}'s salary for {payment.month} was rejected: {review_note}"
        notify(recipient=payment.requested_by, title=title, message=message, link="/manager/staff")
    return payment


@transaction.atomic
def disburse_salary_payment(*, actor, payment: SalaryPayment, payment_method: str) -> SalaryPayment:
    """
    The Manager actually pays it, now that Admin has approved it.

    This is the one moment the money genuinely leaves the clinic, so this is
    where the Expense record is created — not at request time, and not at
    approval time. It's created already `approved` (not run through Expense's
    own auto-approve-threshold pending logic): Admin already authorized this
    exact amount via this request, so routing it through a second, unrelated
    approval gate would be redundant and would understate payroll while it
    waited.
    """
    from apps.expenses.models import Expense

    if payment.status != SalaryPayment.Status.APPROVED:
        raise SalaryPaymentError(
            "This salary payment must be approved before it can be paid.", code="not_approved"
        )

    year = timezone.localdate().year
    value = next_value("expense", year)

    expense = Expense.objects.create(
        expense_code=f"EXP-{year}-{str(value).zfill(5)}",
        category=Expense.Category.SALARIES,
        amount=payment.amount,
        description=f"Salary — {payment.staff.name} ({payment.month})",
        paid_to=payment.staff.name,
        payment_method=payment_method,
        branch=payment.branch,
        submitted_by=actor,
        status=Expense.Status.APPROVED,
        reviewed_by=payment.reviewed_by,
        reviewed_at=timezone.now(),
        review_note=f"Salary payment approved by {payment.reviewed_by.name if payment.reviewed_by else 'Admin'}.",
    )

    payment.status = SalaryPayment.Status.PAID
    payment.payment_method = payment_method
    payment.paid_at = timezone.now()
    payment.expense = expense
    payment.save(update_fields=["status", "payment_method", "paid_at", "expense"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=expense,
        branch=payment.branch,
        reason="Salary disbursement",
        changes={"amount": str(payment.amount), "staff": payment.staff.name, "month": payment.month},
    )
    return payment


def salary_payments_branch_summary(queryset, *, month: str | None = None) -> list[dict]:
    """
    Branch-wise totals of Admin-approved salary — one row per branch, split
    into what's approved-but-not-yet-paid and what's already been disbursed.

    A single conditional aggregate (`Sum(..., filter=Q(...))`) grouped by
    branch on `queryset` itself, not a join to a different related table, so
    this doesn't have the fan-out risk `monthly_report` avoids by hand
    (see its docstring) — every row summed here already belongs to the one
    table being grouped.
    """
    if month:
        queryset = queryset.filter(month=month)

    rows = (
        queryset.filter(status__in=[SalaryPayment.Status.APPROVED, SalaryPayment.Status.PAID])
        .values("branch_id", "branch__name")
        .annotate(
            approved_amount=Sum("amount", filter=Q(status=SalaryPayment.Status.APPROVED)),
            paid_amount=Sum("amount", filter=Q(status=SalaryPayment.Status.PAID)),
            payment_count=Count("id"),
        )
        .order_by("branch__name")
    )

    result = []
    for row in rows:
        approved_amount = row["approved_amount"] or Decimal("0.00")
        paid_amount = row["paid_amount"] or Decimal("0.00")
        result.append(
            {
                "branchId": str(row["branch_id"]),
                "branchName": row["branch__name"],
                "approvedAmount": approved_amount,
                "paidAmount": paid_amount,
                "totalApprovedAmount": approved_amount + paid_amount,
                "paymentCount": row["payment_count"],
            }
        )
    return result
