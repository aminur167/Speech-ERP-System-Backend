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

from datetime import date
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import transaction
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


@transaction.atomic
def create_installment_plan(*, actor, branch, patient, service, number_of_installments: int):
    """
    Split a service fee into equal installments.

    Any rounding remainder lands on the **final** installment, so the parts
    always sum to exactly the total — splitting 18,500 three ways must not
    quietly lose or invent a taka.
    """
    if number_of_installments < 2:
        raise EnrollmentError(
            "An installment plan needs at least 2 installments.", code="too_few_installments"
        )

    total = Decimal(service.fee)

    # ROUND_DOWN, not default rounding: the remainder must be non-negative so
    # it lands on the *final* installment. With banker's rounding the base can
    # round up (18,500/3 → 6,166.67 each = 18,500.01), making the remainder
    # negative and the last installment smaller than the rest — the opposite
    # of the documented behaviour, and it charges the patient more up front.
    base = (total / number_of_installments).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    remainder = total - (base * number_of_installments)

    plan = InstallmentPlan.objects.create(
        patient=patient, service=service, branch=branch, total_amount=total
    )

    today = timezone.localdate()
    first_of_month = today.replace(day=1)

    rows = []
    for index in range(1, number_of_installments + 1):
        amount = base + remainder if index == number_of_installments else base
        month_date = add_months(first_of_month, index - 1)
        rows.append(
            Installment(
                plan=plan,
                index=index,
                label=f"{ORDINALS[index - 1] if index <= len(ORDINALS) else f'{index}th'} Installment",
                amount=amount,
                due_date=due_date_for_month(month_key(month_date)),
                status=BillStatus.DUE if index == 1 else BillStatus.UPCOMING,
            )
        )
    Installment.objects.bulk_create(rows)

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=plan,
        branch=branch,
        changes={"service": service.name, "total": str(total), "parts": number_of_installments},
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
def collect_installment_payment(
    *, actor, branch, installment, method: str, idempotency_key: str | None = None
):
    """Same contract as `collect_bill_payment`, for installment plans."""
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

    payment, created = payment_services.create_payment(
        actor=actor,
        branch=branch,
        patient=plan.patient,
        amount=installment.outstanding,
        method=method,
        category=PaymentCategory.INSTALLMENT,
        description=f"{plan.service.name} — {installment.label}",
        idempotency_key=idempotency_key,
    )

    if created:
        installment.amount_paid = installment.amount
        installment.status = BillStatus.PAID
        installment.paid_at = timezone.now()
        installment.payment = payment
        installment.save(update_fields=["amount_paid", "status", "paid_at", "payment"])

        nxt = plan.installments.filter(status=BillStatus.UPCOMING).order_by("index").first()
        if nxt is not None:
            nxt.status = BillStatus.DUE
            nxt.save(update_fields=["status"])

    return payment, installment


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


@transaction.atomic
def terminate(*, actor, container) -> None:
    """
    Stop a service — but only once the patient has settled what they owe.

    Without this block, a patient could walk away from an unpaid bill simply
    by asking to stop, and the debt would vanish from Outstanding Due. The
    sanctioned escape hatch for genuinely uncollectable money is an
    admin-approved refund write-off (docs/04), not silent termination.
    """
    outstanding = container.outstanding_total()

    if outstanding > 0:
        unpaid = (
            container.oldest_unpaid_bill()
            if isinstance(container, MonthlyEnrollment)
            else container.oldest_unpaid_installment()
        )
        label = unpaid.label if unpaid else "an outstanding balance"
        raise EnrollmentError(
            f"{outstanding} outstanding ({label}) must be paid before this service "
            f"can be stopped.",
            code="outstanding_dues",
            extra={"outstandingAmount": str(outstanding), "oldestLabel": label},
        )

    container.status = EnrollmentStatus.TERMINATED
    container.terminated_at = timezone.now()
    container.save(update_fields=["status", "terminated_at"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.TERMINATE,
        target=container,
        branch=container.branch,
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
