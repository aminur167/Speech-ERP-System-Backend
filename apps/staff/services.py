"""
Staff HR business logic: roster codes, attendance check-in/out, and bonuses.

Attendance and bonus writes go through here rather than a plain serializer
`.save()` so that status is always derived from the actual clock server-side
(a client claiming "present" at 2pm would otherwise be trusted) and so a
bonus is always attributed to the manager who is actually authenticated,
never to a name the client sends.

Day-of attendance is deliberately only ever one of three derived states:
Present, Absent (no show by close — see `mark_no_show_absentees`), or Early
Leave (checked out before closing). "On Leave" remains a manual override via
`mark_attendance` for planned absences. There is no "Late" status — arrival
time doesn't change the outcome, only departure time does.

Check-in and check-out are both refused before the office opens (see
`_ensure_office_is_open`) — a clinic that isn't open yet can't have anyone
genuinely clocking in, so a punch at 2am is bad data, not an early arrival.
Check-in additionally closes once the office does (see
`_ensure_check_in_window_is_open`) — there is no late-arrival override, so an
auto-marked absent is final for the day. Check-out has no such upper bound:
an end-of-day check-out at or after closing is exactly what "on time" means.
"""

from collections import defaultdict
from datetime import date, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.notifications.inapp import notify, notify_many
from apps.staff.models import (
    SalaryPayment,
    StaffAdvance,
    StaffAttendance,
    StaffBonus,
    StaffMember,
)

# Office hours: 9am-4pm. Anyone still unmarked once the end hour passes is
# auto-marked absent (see `mark_no_show_absentees`); checking out before the
# end hour is an early leave rather than a plain present.
OFFICE_START_HOUR = 9
OFFICE_END_HOUR = 16

# The clinic's weekly holiday. `date.weekday()` is Monday=0 .. Sunday=6, so
# Friday is 4. No-show auto-marking skips this day entirely (see
# `mark_no_show_absentees`) -- nobody is penalised for not showing up on a
# day nobody is expected to. A staff member who does come in on a Friday
# still gets ordinary present/early-leave treatment from check-in/check-out.
WEEKLY_HOLIDAY_WEEKDAY = 4


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


class AttendanceError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


def _status_for_check_out(check_out_at) -> str:
    # `check_out_at` is a `timezone.now()`-style UTC-aware datetime -- calling
    # `.time()` on it directly would compare a UTC wall-clock hour against an
    # Asia/Dhaka office-hours constant. `localtime()` converts first.
    return (
        StaffAttendance.Status.EARLY_LEAVE
        if timezone.localtime(check_out_at).time() < time(OFFICE_END_HOUR, 0)
        else StaffAttendance.Status.PRESENT
    )


def _ensure_office_is_open(now, *, action: str) -> None:
    """
    Check-in and check-out only make sense once the office has actually
    opened -- a punch at 2am isn't a real attendance event, it's bad data
    (exactly the kind that used to render as a "Present ... 12:45 AM" row).

    No upper bound here: an end-of-day check-out at or after 4pm is exactly
    what "on time" looks like (see `_status_for_check_out`), and stragglers
    are auto-checked-out well past closing too. Check-in has its own,
    stricter upper bound -- see `_ensure_check_in_window_is_open`.
    """
    if timezone.localtime(now).time() < time(OFFICE_START_HOUR, 0):
        raise AttendanceError(
            f"Can't {action} before office hours start at {OFFICE_START_HOUR}:00 AM.",
            code="before_office_hours",
        )


def _ensure_check_in_window_is_open(now) -> None:
    """
    Check-in closes at both ends of the day: before the office opens (shared
    with check-out, see `_ensure_office_is_open`), and once it closes at 4pm.

    There is no late-arrival override -- once `mark_no_show_absentees` has
    (or would have) auto-marked someone absent for the day, that's final. A
    punch at 8pm was never a real attendance event; it was a manager trying
    to paper over a no-show after the fact.
    """
    _ensure_office_is_open(now, action="check in")
    if timezone.localtime(now).time() >= time(OFFICE_END_HOUR, 0):
        raise AttendanceError(
            f"Can't check in after office hours end at {OFFICE_END_HOUR}:00.",
            code="after_office_hours",
        )


def _upsert_attendance(record: StaffAttendance, *, update_fields: list[str]) -> StaffAttendance:
    """
    Write today's row for this staff member, creating it or updating it.

    One `INSERT ... ON CONFLICT DO UPDATE` on the (staff, date) unique key:
    the database does the lock-and-decide that a locking read, a
    create-if-missing and a separate update used to do in several statements
    (and a savepoint), and a single statement needs no transaction around it.
    `record` carries the values a NEW row starts with; `update_fields` names
    the ones an EXISTING row takes from it -- the rest of an existing row is
    left exactly as it was.
    """
    StaffAttendance.objects.bulk_create(
        [record],
        update_conflicts=True,
        unique_fields=["staff", "date"],
        update_fields=update_fields,
    )
    return record


def check_in(*, staff: StaffMember) -> StaffAttendance:
    now = timezone.now()
    _ensure_check_in_window_is_open(now)

    return _upsert_attendance(
        StaffAttendance(
            staff=staff, branch_id=staff.branch_id, date=timezone.localdate(),
            check_in_at=now, check_out_at=None, status=StaffAttendance.Status.PRESENT,
        ),
        update_fields=["check_in_at", "check_out_at", "status"],
    )


def check_out(*, staff: StaffMember) -> StaffAttendance:
    now = timezone.now()
    _ensure_office_is_open(now, action="check out")

    # A day with no check-in yet starts as present-since-now; the check-out
    # itself then closes it. An existing row keeps its own check-in time.
    return _upsert_attendance(
        StaffAttendance(
            staff=staff, branch_id=staff.branch_id, date=timezone.localdate(),
            check_in_at=now, check_out_at=now, status=_status_for_check_out(now),
        ),
        update_fields=["check_out_at", "status"],
    )


def mark_attendance(*, staff: StaffMember, status: str) -> StaffAttendance:
    """Manager override for a day the person didn't check in themselves — on leave or absent."""
    return _upsert_attendance(
        StaffAttendance(
            staff=staff, branch_id=staff.branch_id, date=timezone.localdate(),
            check_in_at=None, check_out_at=None, status=status,
        ),
        update_fields=["status", "check_in_at", "check_out_at"],
    )


def mark_no_show_absentees(staff_queryset) -> int:
    """
    Once office hours (4pm) are over, anyone in scope with no attendance row
    for today gets auto-marked absent — a Manager shouldn't have to remember
    to close out no-shows by hand.

    A no-op before 4pm, and a no-op entirely on the weekly holiday (Friday):
    a company that isn't open that day can't have a no-show. Safe to call
    repeatedly -- from a request handler (`today-attendance`/`summary`) and
    from the daily `close_out_daily_attendance` cron job alike, which is what
    makes this "real-time" rather than dependent on someone opening the
    Staff page after 4pm: `ignore_conflicts` makes it idempotent against the
    (staff, date) unique constraint. This is final -- `check_in` refuses to
    run once the office has closed (see `_ensure_check_in_window_is_open`),
    so there is no way to overwrite this placeholder row after the fact.

    Returns how many staff members were newly marked absent.
    """
    now = timezone.localtime()
    if now.hour < OFFICE_END_HOUR or now.weekday() == WEEKLY_HOLIDAY_WEEKDAY:
        return 0

    today = now.date()
    staff_list = list(staff_queryset)
    already_marked = set(
        StaffAttendance.objects.filter(
            staff_id__in=[member.id for member in staff_list], date=today
        ).values_list("staff_id", flat=True)
    )
    to_create = [
        StaffAttendance(
            staff=member, branch=member.branch, date=today, status=StaffAttendance.Status.ABSENT
        )
        for member in staff_list
        if member.id not in already_marked
    ]
    if to_create:
        StaffAttendance.objects.bulk_create(to_create, ignore_conflicts=True)
    return len(to_create)


def auto_check_out_stragglers(staff_queryset) -> int:
    """
    Once office hours (4pm) are over, anyone still checked in without a
    check-out gets auto-checked-out — a Manager shouldn't have to remember to
    check someone out by hand just because they forgot to tap the button on
    their way out.

    A no-op before 4pm. Unlike `mark_no_show_absentees`, this doesn't skip the
    weekly holiday: someone who did check in on a Friday still needs closing
    out. Status stays PRESENT (not EARLY_LEAVE) because `_status_for_check_out`
    at or after 4pm always resolves to PRESENT anyway — same rule a real
    check-out at this hour would get.

    Returns how many attendance rows were auto-checked-out.
    """
    now = timezone.localtime()
    if now.hour < OFFICE_END_HOUR:
        return 0

    today = now.date()
    return StaffAttendance.objects.filter(
        staff__in=staff_queryset,
        date=today,
        check_in_at__isnull=False,
        check_out_at__isnull=True,
    ).update(check_out_at=timezone.now(), status=StaffAttendance.Status.PRESENT)


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


def month_range(year: int, month: int) -> tuple[date, date]:
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
    start, end = month_range(year, month)

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
                "earlyLeaveCount": counts.get(StaffAttendance.Status.EARLY_LEAVE, 0),
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

    Outstanding advances come off it here, oldest first, and the subtraction
    is frozen alongside the figures it was made from. The advances are *not*
    marked recovered yet — that happens when the salary is actually paid
    (`disburse_salary_payment`). A rejected request hands over no money, so
    recording a repayment against it would write off a debt the clinic never
    collected.
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
    gross = rows[0]["netPayable"] if rows else staff.monthly_salary

    deduction, _rows = _recover_advances(staff, min(gross, _recoverable_now(staff)))
    amount = gross - deduction

    payment = SalaryPayment.objects.create(
        staff=staff, branch=staff.branch, month=month,
        amount=amount, gross_amount=gross, advance_deduction=deduction,
        requested_by=actor,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=payment,
        branch=staff.branch,
        changes={
            "amount": str(amount),
            "gross": str(gross),
            "advanceDeduction": str(deduction),
            "month": month,
            "staff": staff.name,
        },
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
    if payment.status != SalaryPayment.Status.APPROVED:
        raise SalaryPaymentError(
            "This salary payment must be approved before it can be paid.", code="not_approved"
        )

    # The advances this month's salary pays back. Applied here, where the
    # money actually changed hands, not at request time -- a request that is
    # rejected or left pending must not have written off anybody's debt.
    _apply_advance_recovery(payment)

    # Nothing left the till when advances took the whole month, so there is
    # no expense to record -- the advance's own Expense already accounted for
    # that money when it was handed over. Writing a zero-value expense here
    # would put a meaningless line in the branch's books.
    expense = None
    if payment.amount > 0:
        expense = _write_salary_expense(
            actor=actor, payment=payment, payment_method=payment_method
        )

    payment.status = SalaryPayment.Status.PAID
    payment.payment_method = payment_method
    payment.paid_at = timezone.now()
    payment.expense = expense
    payment.save(update_fields=["status", "payment_method", "paid_at", "expense"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        # The payment itself when no expense was written -- an audit entry
        # pointing at nothing would lose the event entirely.
        target=expense or payment,
        branch=payment.branch,
        reason="Salary disbursement",
        changes={
            "amount": str(payment.amount),
            "advanceRecovered": str(payment.advance_deduction),
            "staff": payment.staff.name,
            "month": payment.month,
        },
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


# ---------------------------------------------------------------------------
# Advances
# ---------------------------------------------------------------------------


class AdvanceError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


#: An advance at one of these has been promised or handed over, so it is
#: real money the branch is committed to. REJECTED ones are not.
_LIVE_ADVANCE_STATUSES = [
    StaffAdvance.Status.PENDING_APPROVAL,
    StaffAdvance.Status.APPROVED,
    StaffAdvance.Status.PAID,
]


def outstanding_advances(staff: StaffMember):
    """
    Paid advances this person has not finished repaying, oldest first.

    Only PAID ones: an advance still waiting on Admin, or approved but not
    yet handed over, is not money the person has, so taking it out of their
    salary would be collecting a debt that does not exist yet.

    Oldest first is the recovery order and the model's own ordering, so this
    is a filter, not a re-sort.
    """
    return staff.advances.filter(
        status=StaffAdvance.Status.PAID, recovered_amount__lt=F("amount")
    )


def outstanding_advance_total(staff: StaffMember) -> Decimal:
    return sum(
        (advance.outstanding for advance in outstanding_advances(staff)),
        Decimal("0.00"),
    )


@transaction.atomic
def request_advance(*, actor, staff: StaffMember, amount, reason: str) -> StaffAdvance:
    """
    A Manager's ask to pay one staff member early. Nothing moves until Admin
    approves it, exactly as a salary request.

    Several advances may be open at once -- a second emergency does not wait
    on the first being repaid -- so there is no duplicate guard here. What
    there is instead is a ceiling: see `_assert_advance_is_recoverable`.
    """
    value = Decimal(amount or 0)
    if value <= 0:
        raise AdvanceError("An advance must be more than zero.", code="invalid_amount")
    if not (reason or "").strip():
        raise AdvanceError(
            "Say why the advance is being given.", code="reason_required"
        )

    _assert_advance_is_recoverable(staff, value)

    advance = StaffAdvance.objects.create(
        staff=staff, branch=staff.branch, amount=value,
        reason=reason.strip(), requested_by=actor,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=advance,
        branch=staff.branch,
        reason=advance.reason,
        changes={"amount": str(value), "staff": staff.name},
    )

    from apps.accounts.models import User

    notify_many(
        recipients=User.objects.filter(role=User.Role.ADMIN),
        title="New salary advance request",
        message=(
            f"{staff.branch.name} requested BDT {value} as an advance for "
            f"{staff.name}."
        ),
        link="/admin/salary-approvals",
    )
    return advance


def _assert_advance_is_recoverable(staff: StaffMember, amount: Decimal) -> None:
    """
    Refuse an advance the salary cannot take back.

    Everything already owed, plus this, has to stay inside one month's pay.
    Recovery comes out of a single month, and whatever will not fit is
    carried -- so without a ceiling an advance could be granted that quietly
    takes several months of someone's entire salary to clear, leaving them
    nothing to live on in the meantime. Which is a decision for a person to
    make deliberately, not something a form should wave through.
    """
    already = outstanding_advance_total(staff)
    committed = StaffAdvance.objects.filter(
        staff=staff, status__in=[
            StaffAdvance.Status.PENDING_APPROVAL, StaffAdvance.Status.APPROVED,
        ],
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    ceiling = staff.monthly_salary
    if already + committed + amount > ceiling:
        remaining = max(ceiling - already - committed, Decimal("0.00"))
        raise AdvanceError(
            f"That is more than {staff.name}'s salary can recover. "
            f"At most {remaining} can be advanced right now.",
            code="exceeds_recoverable",
        )


@transaction.atomic
def review_advance(
    *, actor, advance: StaffAdvance, approve: bool, review_note: str = ""
) -> StaffAdvance:
    """Admin approves or rejects. Rejecting requires a reason, as a salary rejection does."""
    if advance.status != StaffAdvance.Status.PENDING_APPROVAL:
        raise AdvanceError(
            f"This advance is already {advance.get_status_display().lower()}.",
            code="no_change",
        )
    if not approve and not review_note.strip():
        raise AdvanceError(
            "A reason is required when rejecting an advance.", code="note_required"
        )

    advance.status = (
        StaffAdvance.Status.APPROVED if approve else StaffAdvance.Status.REJECTED
    )
    advance.review_note = review_note
    advance.reviewed_by = actor
    advance.reviewed_at = timezone.now()
    advance.save(update_fields=["status", "review_note", "reviewed_by", "reviewed_at"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.APPROVE if approve else AuditLog.Action.REJECT,
        target=advance,
        branch=advance.branch,
        reason=review_note,
        changes={
            "status": {
                "from": StaffAdvance.Status.PENDING_APPROVAL, "to": advance.status
            }
        },
    )

    if advance.requested_by_id:
        if approve:
            title = "Advance approved"
            message = (
                f"The advance for {advance.staff.name} was approved — "
                "you can pay it out now."
            )
        else:
            title = "Advance rejected"
            message = (
                f"The advance for {advance.staff.name} was rejected: {review_note}"
            )
        notify(
            recipient=advance.requested_by, title=title, message=message,
            link="/manager/staff/payroll",
        )
    return advance


@transaction.atomic
def disburse_advance(*, actor, advance: StaffAdvance, payment_method: str) -> StaffAdvance:
    """
    The Manager hands the money over. The Expense is written here, for the
    same reason `disburse_salary_payment` writes it here: this is the moment
    the money genuinely leaves.

    Filed under SALARIES, because that is what it is -- salary, early. A
    category of its own would split one month's payroll across two lines of
    the expense report and make the total answer to nothing.
    """
    from apps.expenses.models import Expense

    if advance.status != StaffAdvance.Status.APPROVED:
        raise AdvanceError(
            "This advance must be approved before it can be paid.",
            code="not_approved",
        )

    year = timezone.localdate().year
    value = next_value("expense", year)

    expense = Expense.objects.create(
        expense_code=f"EXP-{year}-{str(value).zfill(5)}",
        category=Expense.Category.SALARIES,
        amount=advance.amount,
        description=f"Salary advance — {advance.staff.name}",
        paid_to=advance.staff.name,
        payment_method=payment_method,
        branch=advance.branch,
        submitted_by=actor,
        status=Expense.Status.APPROVED,
        reviewed_by=advance.reviewed_by,
        reviewed_at=timezone.now(),
        review_note=(
            f"Advance approved by "
            f"{advance.reviewed_by.name if advance.reviewed_by else 'Admin'}."
        ),
    )

    advance.status = StaffAdvance.Status.PAID
    advance.payment_method = payment_method
    advance.paid_at = timezone.now()
    advance.expense = expense
    advance.save(update_fields=["status", "payment_method", "paid_at", "expense"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=expense,
        branch=advance.branch,
        reason="Salary advance disbursement",
        changes={"amount": str(advance.amount), "staff": advance.staff.name},
    )
    return advance


def _recover_advances(staff: StaffMember, payable: Decimal) -> tuple[Decimal, list]:
    """
    Take what this month's salary can off the outstanding advances.

    Returns `(deducted, rows)` without writing anything — the caller decides
    whether this is a preview or a real salary request.

    Oldest first, and never past what is payable: a month can only give back
    what it holds, and the remainder stays outstanding for the next one.
    `Payment`/`Expense` cannot be negative and a staff member cannot be asked
    to hand money back, so clamping here is the only correct shape.
    """
    budget = payable
    deducted = Decimal("0.00")
    rows = []

    for advance in outstanding_advances(staff):
        if budget <= 0:
            break
        take = min(advance.outstanding, budget)
        budget -= take
        deducted += take
        rows.append({"advance": advance, "amount": take})

    return deducted, rows


def _recoverable_now(staff: StaffMember) -> Decimal:
    """
    Outstanding advances, less whatever other open salary requests have
    already promised to take back.

    Without this, requesting two months at once before paying either would
    deduct the same advance twice: both requests would look at the same
    untouched `recovered_amount`, because recovery is only written at
    disbursement.
    """
    committed = SalaryPayment.objects.filter(
        staff=staff,
        status__in=[
            SalaryPayment.Status.PENDING_APPROVAL, SalaryPayment.Status.APPROVED,
        ],
    ).aggregate(total=Sum("advance_deduction"))["total"] or Decimal("0.00")

    return max(outstanding_advance_total(staff) - committed, Decimal("0.00"))


def _apply_advance_recovery(payment: SalaryPayment) -> None:
    """
    Write the repayment this salary makes, oldest advance first.

    Recomputed from the live advances rather than replayed from a stored
    allocation: `advance_deduction` is the total that was agreed, and this
    spreads exactly that much over whatever is still outstanding now. If an
    advance was settled some other way in between, the rest simply lands on
    the next one instead of failing.
    """
    budget = payment.advance_deduction
    if budget <= 0:
        return

    touched = []
    for advance in outstanding_advances(payment.staff).select_for_update():
        if budget <= 0:
            break
        take = min(advance.outstanding, budget)
        advance.recovered_amount += take
        budget -= take
        touched.append(advance)

    if touched:
        StaffAdvance.objects.bulk_update(touched, ["recovered_amount"])


def _write_salary_expense(*, actor, payment: SalaryPayment, payment_method: str):
    """The Expense a disbursed salary leaves behind. Split out so the zero case can skip it."""
    from apps.expenses.models import Expense

    year = timezone.localdate().year
    value = next_value("expense", year)

    return Expense.objects.create(
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
        review_note=(
            f"Salary payment approved by "
            f"{payment.reviewed_by.name if payment.reviewed_by else 'Admin'}."
        ),
    )
