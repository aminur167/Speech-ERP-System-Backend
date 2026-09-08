"""
Enrollment lifecycle: creating plans, generating bills, collecting payments,
and terminating.

The rules that matter most here, all confirmed in docs/05:

  * Bills generate monthly via a scheduled job, due on the 5th, overdue after.
  * Payment applies **oldest unpaid first** — September can't be paid while
    August is outstanding.
  * Collecting a payment and marking the bill paid happen in **one atomic
    operation**. The frontend mock did these as two separate API calls, so a
    crash between them took the money without settling the bill.
  * Termination is **blocked** while anything is outstanding.
"""

from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.enrollments.models import (
    BillStatus,
    Booking,
    EnrollmentStatus,
    Installment,
    InstallmentPlan,
    MonthlyBill,
    MonthlyEnrollment,
    due_date_for_month,
)
from apps.payments import services as payment_services
from apps.payments.models import Payment, PaymentCategory

ORDINALS = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th", "11th", "12th"]


class EnrollmentError(Exception):
    def __init__(self, message: str, *, code: str = "invalid", extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}


def month_key(value: date) -> str:
    return f"{value.year}-{value.month:02d}"


def month_label(value: date) -> str:
    return value.strftime("%B %Y")


def add_months(value: date, months: int) -> date:
    """Month arithmetic on the 1st, so month lengths never truncate a date."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


# ---------------------------------------------------------------------------
# Monthly enrollments
# ---------------------------------------------------------------------------


@transaction.atomic
def create_monthly_enrollment(*, actor, branch, patient, service, months_ahead: int = 3):
    """
    Enroll a patient and open the first few months of billing.

    Only the current month is `due`; the rest are `upcoming` so the due-payment
    list shows one payable bill at a time. The scheduled job extends the series
    from here (`generate_due_bills`).
    """
    enrollment = MonthlyEnrollment.objects.create(
        patient=patient, service=service, branch=branch
    )

    today = timezone.localdate()
    first_of_month = today.replace(day=1)

    bills = []
    for offset in range(months_ahead):
        month_date = add_months(first_of_month, offset)
        key = month_key(month_date)
        bills.append(
            MonthlyBill(
                enrollment=enrollment,
                month=key,
                label=month_label(month_date),
                amount=service.fee,
                due_date=due_date_for_month(key),
                status=BillStatus.DUE if offset == 0 else BillStatus.UPCOMING,
            )
        )
    MonthlyBill.objects.bulk_create(bills)

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=enrollment,
        branch=branch,
        changes={"service": service.name, "fee": str(service.fee)},
    )
    return enrollment


def split_equally(total: Decimal, parts: int) -> list[Decimal]:
    """
    `total` in `parts` equal amounts, remainder on the last.

    ROUND_DOWN, not default rounding: the remainder must be non-negative so
    it lands on the *final* part. With banker's rounding the base can round
    up (18,500/3 → 6,166.67 each = 18,500.01), making the remainder negative
    and the last part smaller than the rest — the opposite of the documented
    behaviour, and it charges the patient more up front.
    """
    base = (total / parts).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    remainder = total - (base * parts)
    return [base + remainder if i == parts - 1 else base for i in range(parts)]


def spread_due_dates(*, starts_on: date, ends_on: date, count: int) -> list[date]:
    """
    `count` due dates from `starts_on` to `ends_on` inclusive, evenly spaced.

    The first falls on `starts_on` and the last on `ends_on`, so the plan is
    cleared inside the window the manager agreed with the patient rather than
    running past it. Integer division means intermediate gaps can differ by a
    day; the two endpoints are what matter and they stay exact.
    """
    if count == 1:
        return [ends_on]
    span = (ends_on - starts_on).days
    return [starts_on + timedelta(days=(span * i) // (count - 1)) for i in range(count)]


@transaction.atomic
def create_installment_plan(
    *,
    actor,
    branch,
    patient,
    service,
    number_of_installments: int,
    starts_on: date | None = None,
    ends_on: date | None = None,
):
    """
    Split a service fee into equal installments across an agreed window.

    Given `starts_on`/`ends_on`, due dates spread evenly across that range —
    first on the start, last on the end — so the plan clears inside it.
    Without them the original behaviour stands (monthly, on the configured due
    day), which is what plans created before the range existed were built on.

    Amounts are equal by default; `collect_installment_payment` re-divides
    what's left whenever a collection differs from the scheduled amount.
    """
    if number_of_installments < 2:
        raise EnrollmentError(
            "An installment plan needs at least 2 installments.", code="too_few_installments"
        )

    if (starts_on is None) != (ends_on is None):
        raise EnrollmentError(
            "Give both a start and an end date, or neither.", code="incomplete_range"
        )

    if starts_on is not None and ends_on is not None:
        if ends_on < starts_on:
            raise EnrollmentError(
                "The end date cannot be before the start date.", code="invalid_range"
            )
        # One date per installment at minimum: two installments falling due on
        # the same day isn't a schedule, and it makes the even spread
        # meaningless.
        if (ends_on - starts_on).days + 1 < number_of_installments:
            raise EnrollmentError(
                f"A {number_of_installments}-installment plan needs a range of at least "
                f"{number_of_installments} days.",
                code="range_too_short",
            )

    total = Decimal(service.fee)
    amounts = split_equally(total, number_of_installments)

    plan = InstallmentPlan.objects.create(
        patient=patient,
        service=service,
        branch=branch,
        total_amount=total,
        starts_on=starts_on,
        ends_on=ends_on,
    )

    if starts_on is not None and ends_on is not None:
        due_dates = spread_due_dates(
            starts_on=starts_on, ends_on=ends_on, count=number_of_installments
        )
    else:
        first_of_month = timezone.localdate().replace(day=1)
        due_dates = [
            due_date_for_month(month_key(add_months(first_of_month, index)))
            for index in range(number_of_installments)
        ]

    rows = [
        Installment(
            plan=plan,
            index=index + 1,
            label=(
                f"{ORDINALS[index]} Installment"
                if index < len(ORDINALS)
                else f"{index + 1}th Installment"
            ),
            amount=amounts[index],
            due_date=due_dates[index],
            status=BillStatus.DUE if index == 0 else BillStatus.UPCOMING,
        )
        for index in range(number_of_installments)
    ]
    Installment.objects.bulk_create(rows)

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=plan,
        branch=branch,
        changes={
            "service": service.name,
            "total": str(total),
            "parts": number_of_installments,
            "window": f"{starts_on} → {ends_on}" if starts_on else "monthly",
        },
    )
    return plan


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _assert_is_oldest_unpaid(container, target) -> None:
    """
    Oldest-first enforcement.

    Without it, a patient can keep paying the current month while an old debt
    ages indefinitely, and Outstanding Due stops describing anything
    actionable. The error names what must be paid instead of just refusing —
    the manager is standing in front of the patient and needs to know.
    """
    oldest = (
        container.oldest_unpaid_bill()
        if isinstance(container, MonthlyEnrollment)
        else container.oldest_unpaid_installment()
    )

    if oldest is None:
        raise EnrollmentError("Nothing is currently outstanding.", code="nothing_due")

    if oldest.pk != target.pk:
        raise EnrollmentError(
            f"{oldest.label} ({oldest.outstanding}) must be paid first.",
            code="not_oldest_unpaid",
            extra={"oldestLabel": oldest.label, "oldestAmount": str(oldest.outstanding)},
        )


@transaction.atomic
def collect_bill_payment(
    *, actor, branch, bill, method: str, idempotency_key: str | None = None
):
    """
    Take payment for a monthly bill and settle it — atomically.

    The mock split this into "create payment" then "mark bill paid". A crash
    between the two took the patient's money and left the bill unpaid, with
    nothing to reconcile it against. One transaction makes that impossible.
    """
    enrollment = bill.enrollment

    # An idempotency-key replay must return the ORIGINAL result even though
    # the bill is already settled by now -- checked before every other guard,
    # or a legitimate retry (a network timeout, an offline-queue replaying a
    # queued mutation) gets told "already paid" instead of getting back the
    # receipt its first attempt actually produced.
    if idempotency_key:
        existing = Payment.all_objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            bill.refresh_from_db()
            return existing, bill

    if not enrollment.is_active:
        raise EnrollmentError("This enrollment has been terminated.", code="terminated")

    # Lock the row so two managers collecting the same bill can't both succeed.
    bill = MonthlyBill.objects.select_for_update().get(pk=bill.pk)

    if bill.is_settled or bill.status in {BillStatus.PAID, BillStatus.WRITTEN_OFF}:
        raise EnrollmentError("This bill has already been settled.", code="already_paid")

    _assert_is_oldest_unpaid(enrollment, bill)

    payment, created = payment_services.create_payment(
        actor=actor,
        branch=branch,
        patient=enrollment.patient,
        amount=bill.outstanding,
        method=method,
        category=PaymentCategory.MONTHLY,
        description=f"{enrollment.service.name} — {bill.label}",
        idempotency_key=idempotency_key,
    )

    # A replayed key means this bill was already settled by the original
    # request; don't apply the settlement twice.
    if created:
        bill.amount_paid = bill.amount
        bill.status = BillStatus.PAID
        bill.paid_at = timezone.now()
        bill.payment = payment
        bill.save(update_fields=["amount_paid", "status", "paid_at", "payment"])

        _promote_next_bill(enrollment)

    return payment, bill


def _promote_next_bill(enrollment: MonthlyEnrollment) -> None:
    """Make the next upcoming bill payable once the current one clears."""
    nxt = (
        enrollment.bills.filter(status=BillStatus.UPCOMING).order_by("month").first()
    )
    if nxt is not None:
        nxt.status = BillStatus.DUE
        nxt.save(update_fields=["status"])


@transaction.atomic
def _redistribute_remaining(plan, *, after_index: int) -> None:
    """
    Re-divide what's still owed across the installments after `after_index`.

    The plan's total is fixed; only how it's split moves. So when a
    collection differs from the scheduled amount, the difference is carried
    forward into the later installments equally rather than left stranded on
    a closed one — paying 3,000 against a 5,000 installment on a 15,000 plan
    leaves 12,000 over the remaining two, i.e. 6,000 each.

    Nothing left to owe means the later installments aren't needed at all;
    they're removed so the schedule matches reality (the plan was cleared in
    fewer payments than planned) rather than sitting there at zero.
    """
    later = list(
        plan.installments.filter(index__gt=after_index)
        .exclude(status__in=[BillStatus.PAID, BillStatus.WRITTEN_OFF])
        .order_by("index")
    )
    if not later:
        return

    collected = plan.installments.aggregate(s=Sum("amount_paid"))["s"] or Decimal("0.00")
    remaining = plan.total_amount - collected

    if remaining <= 0:
        plan.installments.filter(pk__in=[row.pk for row in later]).delete()
        return

    amounts = split_equally(remaining, len(later))
    for row, amount in zip(later, amounts):
        row.amount = amount
    Installment.objects.bulk_update(later, ["amount"])


@transaction.atomic
def collect_installment_payment(
    *,
    actor,
    branch,
    installment,
    method: str,
    amount: Decimal | None = None,
    idempotency_key: str | None = None,
):
    """
    Collect against an installment, for any amount the patient can pay today.

    `amount` defaults to the scheduled figure. Anything else closes this
    installment at what was actually collected and carries the difference
    into the later ones (`_redistribute_remaining`) — under- and overpayment
    both, since both change what's left to spread.

    The exception is the final installment: there's nothing after it to carry
    a shortfall into, so it stays open for the remainder instead of quietly
    writing the debt off.
    """
    plan = installment.plan

    # See the matching guard in collect_bill_payment for why this runs first.
    if idempotency_key:
        existing = Payment.all_objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            installment.refresh_from_db()
            return existing, installment

    if not plan.is_active:
        raise EnrollmentError("This plan has been terminated.", code="terminated")

    installment = Installment.objects.select_for_update().get(pk=installment.pk)

    if installment.is_settled or installment.status in {BillStatus.PAID, BillStatus.WRITTEN_OFF}:
        raise EnrollmentError("This installment has already been settled.", code="already_paid")

    _assert_is_oldest_unpaid(plan, installment)

    scheduled = installment.outstanding
    collecting = scheduled if amount is None else Decimal(amount)

    if collecting <= 0:
        raise EnrollmentError("Enter an amount greater than zero.", code="invalid_amount")

    # Never take more than the plan still owes -- the surplus would have
    # nowhere to go and would overstate collection.
    plan_outstanding = plan.total_amount - (
        plan.installments.aggregate(s=Sum("amount_paid"))["s"] or Decimal("0.00")
    )
    if collecting > plan_outstanding:
        raise EnrollmentError(
            f"This plan only has {plan_outstanding} outstanding.",
            code="exceeds_outstanding",
            extra={"outstanding": str(plan_outstanding)},
        )

    is_last = not plan.installments.filter(index__gt=installment.index).exists()
    short = collecting < scheduled

    payment, created = payment_services.create_payment(
        actor=actor,
        branch=branch,
        patient=plan.patient,
        amount=collecting,
        method=method,
        category=PaymentCategory.INSTALLMENT,
        description=f"{plan.service.name} — {installment.label}",
        idempotency_key=idempotency_key,
    )

    if created:
        installment.amount_paid = installment.amount_paid + collecting

        if short and is_last:
            # Stays open: the remainder has nowhere later to go.
            installment.status = (
                BillStatus.OVERDUE
                if installment.due_date < timezone.localdate()
                else BillStatus.DUE
            )
            installment.payment = payment
            installment.save(update_fields=["amount_paid", "status", "payment"])
        else:
            # Closes at what was collected; the schedule absorbs the rest.
            installment.amount = installment.amount_paid
            installment.status = BillStatus.PAID
            installment.paid_at = timezone.now()
            installment.payment = payment
            installment.save(
                update_fields=["amount", "amount_paid", "status", "paid_at", "payment"]
            )
            _redistribute_remaining(plan, after_index=installment.index)

        nxt = plan.installments.filter(status=BillStatus.UPCOMING).order_by("index").first()
        if nxt is not None:
            nxt.status = BillStatus.DUE
            nxt.save(update_fields=["status"])

    installment.refresh_from_db()
    return payment, installment


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


@transaction.atomic
def terminate(*, actor, container, reason: str = "") -> None:
    """
    Stop a service, whatever the patient still owes.

    An outstanding balance used to block this outright, on the reasoning that
    a patient could otherwise walk away from a debt by asking to stop. In
    practice that left a manager unable to close a plan for someone who had
    simply stopped coming, and the row sat in Due Payments forever.

    So termination always succeeds now — but the debt is never allowed to
    quietly evaporate. Anything still unpaid is written off explicitly: the
    same WRITTEN_OFF status an admin-approved refund write-off produces, which
    is excluded from Outstanding Due, plus an audit entry naming the exact
    amount forgiven. The money is accounted for either way; the difference is
    that closing the plan is now the manager's call rather than a dead end.
    """
    outstanding = container.outstanding_total()

    if outstanding > 0:
        unpaid = container.installments if not isinstance(
            container, MonthlyEnrollment
        ) else container.bills
        # `is_settled` rather than status alone: a partially-paid item still
        # has a balance to forgive, and leaving it DUE would keep the money in
        # Outstanding Due after the plan is closed.
        for item in unpaid.exclude(status=BillStatus.WRITTEN_OFF):
            if item.outstanding <= 0:
                continue
            item.status = BillStatus.WRITTEN_OFF
            item.save(update_fields=["status"])

        audit.record(
            actor=actor,
            action=AuditLog.Action.WRITE_OFF,
            target=container,
            branch=container.branch,
            reason=reason or "Written off when the service was stopped",
            changes={"writtenOff": str(outstanding)},
        )

    container.status = EnrollmentStatus.TERMINATED
    container.terminated_at = timezone.now()
    fields = ["status", "terminated_at"]
    if isinstance(container, MonthlyEnrollment):
        # Says this was a person's decision, not the unpaid-due job's — the
        # two are resumed on completely different terms.
        container.terminated_kind = MonthlyEnrollment.TerminationKind.MANUAL
        fields.append("terminated_kind")
    container.save(update_fields=fields)

    audit.record(
        actor=actor,
        action=AuditLog.Action.TERMINATE,
        target=container,
        branch=container.branch,
        reason=reason,
        changes={"writtenOff": str(outstanding)} if outstanding > 0 else None,
    )


# ---------------------------------------------------------------------------
# Scheduled bill generation
# ---------------------------------------------------------------------------


@transaction.atomic
def generate_due_bills(*, up_to: date | None = None) -> dict:
    """
    Extend every active enrollment's billing to the current month.

    Two properties this must have, both tested:

    **Idempotent** — running twice in a month creates no duplicate. Guaranteed
    by the unique constraint on (enrollment, month) rather than by trusting
    the scheduler to fire exactly once.

    **Catch-up capable** — if the server was down on the 1st, the next run
    backfills the missed months instead of skipping them. That's why this
    walks from each enrollment's last bill to the target month rather than
    just adding "this month".
    """
    target = (up_to or timezone.localdate()).replace(day=1)
    created_count = 0
    skipped = 0

    enrollments = MonthlyEnrollment.objects.filter(
        status=EnrollmentStatus.ACTIVE
    ).select_related("service")

    for enrollment in enrollments:
        last = enrollment.bills.order_by("-month").first()
        if last is None:
            skipped += 1
            continue

        last_year, last_month = (int(part) for part in last.month.split("-"))
        cursor = add_months(date(last_year, last_month, 1), 1)

        while cursor <= target:
            key = month_key(cursor)
            _, was_created = MonthlyBill.objects.get_or_create(
                enrollment=enrollment,
                month=key,
                defaults={
                    "label": month_label(cursor),
                    "amount": enrollment.service.fee,
                    "due_date": due_date_for_month(key),
                    "status": BillStatus.DUE,
                },
            )
            if was_created:
                created_count += 1
            cursor = add_months(cursor, 1)

    return {"created": created_count, "enrollments_without_bills": skipped}


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------


def _validate_booking_slot(booking_date: date, booking_time: str) -> None:
    """
    Re-validate the date and time server-side.

    The picker already constrains both in the UI, but that is a convenience,
    not a control -- a direct API call bypasses it entirely.
    """
    if booking_date < timezone.localdate():
        raise EnrollmentError("Bookings cannot be made for a past date.", code="past_date")

    try:
        hour_str, minute_str = booking_time.split(":")
        hour, minute = int(hour_str), int(minute_str)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except (ValueError, AttributeError):
        raise EnrollmentError(
            f'"{booking_time}" is not a valid time.', code="invalid_time"
        ) from None

    minutes_of_day = hour * 60 + minute
    window_start = settings.BOOKING_WINDOW_START_HOUR * 60
    window_end = settings.BOOKING_WINDOW_END_HOUR * 60
    if not (window_start <= minutes_of_day <= window_end):
        raise EnrollmentError(
            f"Bookings are only available between "
            f"{settings.BOOKING_WINDOW_START_HOUR:02d}:00 and "
            f"{settings.BOOKING_WINDOW_END_HOUR:02d}:00.",
            code="outside_booking_window",
        )


@transaction.atomic
def create_booking(
    *, actor, branch, patient, service, booking_date, booking_time,
    method: str, idempotency_key: str | None = None,
):
    """
    Book an online session and take the advance in one transaction.

    The advance is always `BOOKING_ADVANCE_RATIO` of the service fee,
    computed here and never accepted from the client -- the same reason
    material-sale pricing is server-side (docs/06). A tampered or stale
    client-supplied amount could otherwise undercharge, or overcharge, a
    patient at the moment of booking.
    """
    # A replay must return the ORIGINAL booking, not a second one. The old
    # code created the Booking unconditionally before ever consulting the
    # idempotency key -- only the Payment underneath it was replay-safe, so a
    # retried request minted a second booking_code and a second calendar
    # slot for the same appointment even though it charged only once.
    if idempotency_key:
        existing_payment = Payment.all_objects.filter(idempotency_key=idempotency_key).first()
        if existing_payment is not None:
            return existing_payment.bookings.first(), existing_payment

    _validate_booking_slot(booking_date, booking_time)

    year = timezone.localdate().year
    value = next_value(f"booking:{branch.code}", year)
    code = f"BKG-{branch.short_code}-{year}-{str(value).zfill(5)}"

    advance_amount = (
        service.fee * Decimal(str(settings.BOOKING_ADVANCE_RATIO))
    ).quantize(Decimal("0.01"))

    booking = Booking.objects.create(
        booking_code=code,
        patient=patient,
        service=service,
        branch=branch,
        date=booking_date,
        time=booking_time,
        advance_amount=advance_amount,
    )

    payment, _ = payment_services.create_payment(
        actor=actor,
        branch=branch,
        patient=patient,
        amount=advance_amount,
        method=method,
        category=PaymentCategory.ONLINE,
        description=f"{service.name} — advance for {booking_date}",
        idempotency_key=idempotency_key,
    )

    booking.payment = payment
    booking.save(update_fields=["payment"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=booking,
        branch=branch,
        changes={"booking_code": code, "advance": str(advance_amount)},
    )
    return booking, payment


def cancel_booking(*, actor, booking: Booking, reason: str = "") -> Booking:
    """
    Marks a booking cancelled. Does not touch its payment -- the advance
    already collected is real money that moved, and refunding it (or not) is
    a separate decision that goes through the existing Refund Request flow
    (Manager requests, Admin approves), the same as any other collected
    payment. Reinventing that here would create a second, untested path to
    the same outcome.
    """
    if booking.status == Booking.Status.CANCELLED:
        raise EnrollmentError("This booking is already cancelled.", code="already_cancelled")

    booking.status = Booking.Status.CANCELLED
    booking.save(update_fields=["status"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.TERMINATE,
        target=booking,
        branch=booking.branch,
        reason=reason,
    )
    return booking


# ---------------------------------------------------------------------------
# Automatic termination for an unpaid month, and resuming afterwards
# ---------------------------------------------------------------------------


@transaction.atomic
def terminate_unpaid_monthly_services(*, actor=None, on: date | None = None) -> dict:
    """
    End every monthly service whose due for a finished month is still unpaid.

    The confirmed rule: a patient has until the last day of the month to
    clear that month's due. October's due unpaid when October ends stops the
    service; cleared in time, nothing happens.

    Written as "any unpaid bill for a month earlier than the current one"
    rather than "last month's bill", so the job is **catch-up capable** the
    same way `generate_due_bills` is: if it doesn't run for a week, the next
    run still ends the services that should have ended, instead of skipping
    them forever. It is also idempotent — an already-terminated enrollment is
    not in the queryset at all, so a service can never be terminated twice.

    Crucially this does **not** write the debt off, which is the whole
    difference from `terminate()`. A manager stopping a service forgives what
    is owed; a service stopped for non-payment keeps every taka of it, because
    the patient may come back in December and settle up (see
    `resume_monthly_service`).
    """
    today = on or timezone.localdate()
    current_month = month_key(today.replace(day=1))

    ended = []
    enrollments = MonthlyEnrollment.objects.filter(
        status=EnrollmentStatus.ACTIVE
    ).select_related("patient", "service", "branch")

    for enrollment in enrollments:
        overdue = enrollment.unpaid_bills().filter(month__lt=current_month).first()
        if overdue is None:
            continue

        _end_for_unpaid_due(actor=actor, enrollment=enrollment, month=overdue.month)
        ended.append(enrollment)

    return {
        "terminated": len(ended),
        "enrollmentIds": [enrollment.id for enrollment in ended],
    }


def _end_for_unpaid_due(*, actor, enrollment: MonthlyEnrollment, month: str) -> None:
    """
    Stop one enrollment, keeping the debt and dropping the empty schedule.

    Bills after the month that ended it are placeholders and nothing more:
    created upfront as the billing lookahead, never payable, never paid, for
    service that will now not be delivered. Removing them is what keeps the
    arrears honest — left behind, they would be counted into "previous due"
    and charge the patient for months they were not enrolled, and on resume
    `oldest_unpaid_bill` would hand the manager a bill from the gap instead of
    the new cycle's. Nothing historical goes with them: no payment, no
    receipt, no audit entry ever pointed at one.
    """
    enrollment.bills.filter(
        month__gt=month, status=BillStatus.UPCOMING, amount_paid=Decimal("0.00")
    ).delete()

    outstanding = enrollment.outstanding_total()

    enrollment.status = EnrollmentStatus.TERMINATED
    enrollment.terminated_at = timezone.now()
    enrollment.terminated_kind = MonthlyEnrollment.TerminationKind.UNPAID_DUE
    enrollment.terminated_month = month
    enrollment.save(
        update_fields=["status", "terminated_at", "terminated_kind", "terminated_month"]
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.TERMINATE,
        target=enrollment,
        branch=enrollment.branch,
        reason=f"{month} due went unpaid to the end of the month",
        changes={"month": month, "outstanding": str(outstanding)},
    )


@transaction.atomic
def resume_monthly_service(
    *, actor, enrollment: MonthlyEnrollment, carry_due: bool,
    method: str = "", idempotency_key: str | None = None,
):
    """
    Restart a service that was stopped for an unpaid due.

    Two ways, and the difference is only what happens to the arrears:

    * `carry_due=True` — the patient pays what they owed. Every unpaid bill is
      collected oldest-first, each keeping its own payment and receipt, so the
      arrears stay attributable to the months they belong to rather than
      collapsing into one undated lump.
    * `carry_due=False` — the arrears are written off, the same explicit
      WRITTEN_OFF that closing a plan produces, with an audit entry naming the
      amount forgiven. Forgiven, never quietly dropped.

    Either way the new cycle starts at the **current** month. The gap months
    are not backfilled: the patient was not enrolled during them, and billing
    for service nobody delivered is the one thing this must not do.

    Both kinds of termination resume through here. A service a manager
    stopped by hand simply has nothing left to collect — stopping it already
    wrote the debt off — so both options lead to the same place for it, and
    the screen says as much rather than offering a choice that isn't one.
    """
    if enrollment.status != EnrollmentStatus.TERMINATED:
        raise EnrollmentError(
            "This service is not terminated.", code="not_terminated"
        )
    if carry_due and not method:
        raise EnrollmentError(
            "A payment method is required to settle the previous due.",
            code="method_required",
        )

    arrears = enrollment.outstanding_total()

    # Reopened before collecting: `collect_bill_payment` refuses to take money
    # for a terminated enrollment, and rightly so. If any part of the
    # collection fails, this whole transaction rolls back and the service
    # stays terminated — it never ends up active with the arrears unpaid.
    enrollment.status = EnrollmentStatus.ACTIVE
    enrollment.terminated_at = None
    enrollment.terminated_kind = ""
    enrollment.terminated_month = ""
    enrollment.save(
        update_fields=["status", "terminated_at", "terminated_kind", "terminated_month"]
    )

    payments = []
    if carry_due:
        # Re-read each time: settling one bill promotes the next, so the list
        # has to be walked from the database rather than from a snapshot.
        while (bill := enrollment.oldest_unpaid_bill()) is not None:
            payment, _ = collect_bill_payment(
                actor=actor, branch=enrollment.branch, bill=bill, method=method,
                idempotency_key=None,
            )
            payments.append(payment)
    elif arrears > 0:
        for bill in enrollment.unpaid_bills():
            bill.status = BillStatus.WRITTEN_OFF
            bill.save(update_fields=["status"])

        audit.record(
            actor=actor,
            action=AuditLog.Action.WRITE_OFF,
            target=enrollment,
            branch=enrollment.branch,
            reason="Previous due waived when the service was resumed",
            changes={"writtenOff": str(arrears)},
        )

    reinstated = _open_cycle_from(enrollment, timezone.localdate())

    changes = {
        "status": {"from": EnrollmentStatus.TERMINATED, "to": EnrollmentStatus.ACTIVE},
        "previousDue": str(arrears),
        "previousDueSettled": carry_due,
    }
    if reinstated:
        # A write-off being undone is never allowed to be silent, even when
        # it is the right thing to do.
        changes["reinstatedMonths"] = reinstated

    audit.record(
        actor=actor,
        action=AuditLog.Action.UPDATE,
        target=enrollment,
        branch=enrollment.branch,
        reason="Service resumed",
        changes=changes,
    )

    enrollment.refresh_from_db()
    return enrollment, payments


def _open_cycle_from(
    enrollment: MonthlyEnrollment, day: date, months_ahead: int = 3
) -> list[str]:
    """
    Start billing again at `day`'s month, with the same lookahead a fresh
    enrollment gets. Returns the months that had to be reinstated.

    `get_or_create` rather than `bulk_create`: the unique (enrollment, month)
    constraint is what actually guarantees no duplicate, and a resumed
    enrollment may already own a bill for one of these months.

    **Reinstating is the subtle part.** Stopping a service by hand writes off
    every unpaid bill it owns, and that includes the lookahead months for
    service never delivered. Resume it and `get_or_create` finds those rows
    already there, written off — so the enrollment would come back active
    with no payable bill at all, quietly never invoicing again. A written-off
    month from here on is therefore reopened, because the charge is no longer
    a forgiven old debt: it is this month's fee for service now being
    delivered. A month already PAID is left alone; nobody is billed twice.
    """
    first_of_month = day.replace(day=1)
    reinstated: list[str] = []

    for offset in range(months_ahead):
        month_date = add_months(first_of_month, offset)
        key = month_key(month_date)
        status = BillStatus.DUE if offset == 0 else BillStatus.UPCOMING

        bill, created = MonthlyBill.objects.get_or_create(
            enrollment=enrollment,
            month=key,
            defaults={
                "label": month_label(month_date),
                "amount": enrollment.service.fee,
                "due_date": due_date_for_month(key),
                "status": status,
            },
        )
        if created:
            continue

        if bill.status == BillStatus.WRITTEN_OFF and bill.amount_paid <= 0:
            bill.status = status
            bill.save(update_fields=["status"])
            reinstated.append(bill.label)

    return reinstated
