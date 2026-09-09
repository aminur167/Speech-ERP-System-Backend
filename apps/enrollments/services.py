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

import hashlib
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
    CLOSED_STATUSES,
    FORGIVEN_STATUSES,
    NON_OUTSTANDING_STATUSES,
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
# Outstanding dues — the gate on enrolling and reactivating
# ---------------------------------------------------------------------------


def patient_outstanding_dues(patient) -> dict:
    """
    Everything this patient still owes, across every service they hold.

    **Inactive services are included on purpose.** Making a service inactive
    asks the manager to keep or cancel each unpaid month; the months they kept
    are still a debt, and they are exactly the debt a returning patient has to
    clear. Looking only at active services would let someone walk away from a
    kept due and re-enroll the next day as if nothing had happened.

    Cancelled and written-off months are absent, because `unpaid_bills` and
    `unpaid_installments` already exclude them — nobody owes a forgiven
    amount, which is the whole point of having forgiven it.
    """
    items: list[dict] = []

    enrollments = patient.monthly_enrollments.select_related("service").prefetch_related(
        "bills"
    )
    for enrollment in enrollments:
        for bill in enrollment.unpaid_bills():
            if bill.outstanding <= 0:
                continue
            items.append(
                {
                    "type": "monthly",
                    "refId": str(enrollment.pk),
                    "itemId": str(bill.pk),
                    "serviceName": enrollment.service.name,
                    "month": bill.month,
                    "label": bill.label,
                    "amount": bill.outstanding,
                    "serviceActive": enrollment.is_active,
                }
            )

    plans = patient.installment_plans.select_related("service").prefetch_related(
        "installments"
    )
    for plan in plans:
        for installment in plan.unpaid_installments():
            if installment.outstanding <= 0:
                continue
            items.append(
                {
                    "type": "installment",
                    "refId": str(plan.pk),
                    "itemId": str(installment.pk),
                    "serviceName": plan.service.name,
                    "month": "",
                    "label": installment.label,
                    "amount": installment.outstanding,
                    "serviceActive": plan.is_active,
                }
            )

    items.sort(key=lambda row: (row["month"], row["label"]))
    return {
        "items": items,
        "total": sum((row["amount"] for row in items), Decimal("0.00")),
    }


def _assert_no_outstanding_dues(patient, *, action: str) -> None:
    """
    Refuse the action while anything is owed.

    Enforced in the service layer rather than the view, because the rule has
    to hold for every caller — a request straight to the API with the screen
    bypassed must be refused on exactly the same terms as the button.
    """
    outstanding = patient_outstanding_dues(patient)
    if outstanding["total"] <= 0:
        return
    raise EnrollmentError(
        "This patient has outstanding due payments. Please clear the due "
        f"payments before they can {action}.",
        code="outstanding_dues",
        extra={
            "total": str(outstanding["total"]),
            "items": [
                {**row, "amount": str(row["amount"])} for row in outstanding["items"]
            ],
        },
    )


# ---------------------------------------------------------------------------
# Monthly enrollments
# ---------------------------------------------------------------------------


@transaction.atomic
def create_monthly_enrollment(*, actor, branch, patient, service, months_ahead: int = 1):
    """
    Enroll a patient and open the current month's bill.

    **Only the current month.** This used to open a three-month lookahead of
    `upcoming` rows, and that turned out to be the wrong shape: a month nobody
    has been asked to pay for yet is not a due, but it sat in the enrollment
    looking like one — it had to be excluded at every read site, dropped again
    whenever a service stopped, and it made "pay October but not November"
    impossible to express. Future months now come into existence one of two
    ways: the monthly job creates one when the month arrives, or an advance
    payment creates exactly the months that were paid for.

    A patient who still owes anything cannot be enrolled in a new service.
    Checked here rather than only in the view, so no other caller can slip
    past it.
    """
    _assert_no_outstanding_dues(patient, action="enroll in a new service")

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

    A patient who still owes anything cannot be enrolled in a new service —
    the same gate `create_monthly_enrollment` applies, checked in the service
    layer so the API cannot be used to step around the screen.
    """
    _assert_no_outstanding_dues(patient, action="enroll in a new service")

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

    # An inactive service is deliberately NOT refused here. Making a service
    # inactive asks the manager to keep or cancel each unpaid month, and the
    # months they kept are still owed — clearing them on the Due Payments
    # screen is the only route back to reactivating the service, so refusing
    # to take that money would make a kept due uncollectable forever. What
    # must not happen to an inactive service is *new* billing, and that is
    # prevented where bills are created, not where they are paid.

    # Lock the row so two managers collecting the same bill can't both succeed.
    bill = MonthlyBill.objects.select_for_update().get(pk=bill.pk)

    if bill.is_settled or bill.status in CLOSED_STATUSES:
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
        # Not a literal PAID: a month settled before it arrives is an advance.
        bill.status = bill.settled_status()
        bill.paid_at = timezone.now()
        bill.payment = payment
        bill.save(update_fields=["amount_paid", "status", "paid_at", "payment"])

        _promote_next_bill(enrollment)

    return payment, bill


def _promote_next_bill(enrollment: MonthlyEnrollment) -> None:
    """
    Make the next payable bill due once the current one clears.

    Reads the oldest *unpaid* bill rather than filtering for UPCOMING: with
    months payable in advance, the nearest upcoming row may already be
    prepaid, and filtering by status would step over it to promote a later
    month — leaving a further-out bill marked due while a nearer one sat
    settled ahead of it.
    """
    nxt = enrollment.oldest_unpaid_bill()
    if nxt is not None and nxt.status == BillStatus.UPCOMING:
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
        .exclude(status__in=CLOSED_STATUSES)
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

    # Inactive plans still accept payment for the instalments the manager
    # chose to keep — see the note in `collect_bill_payment`.

    installment = Installment.objects.select_for_update().get(pk=installment.pk)

    if installment.is_settled or installment.status in CLOSED_STATUSES:
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
def terminate(
    *, actor, container, reason: str = "", waivers: dict | None = None,
    waived_status: str = BillStatus.WRITTEN_OFF,
) -> None:
    """
    Stop a service, whatever the patient still owes.

    An outstanding balance used to block this outright, on the reasoning that
    a patient could otherwise walk away from a debt by asking to stop. In
    practice that left a manager unable to close a plan for someone who had
    simply stopped coming, and the row sat in Due Payments forever.

    So termination always succeeds now — but the debt is never allowed to
    quietly evaporate. Anything waived is written off explicitly: the same
    WRITTEN_OFF status an admin-approved refund write-off produces, which is
    excluded from Outstanding Due, plus an audit entry naming the exact amount
    forgiven. The money is accounted for either way; the difference is that
    closing the plan is now the manager's call rather than a dead end.

    `waivers` is `{bill_id: "why"}` — the per-month decision the Stop Service
    dialog collects. Each waived month carries **its own written reason**,
    because the point of recording one is that Admin can see why the amount
    owed went down; "written off when the service was stopped" answers that
    question with the fact it was asked about.

    Omitting `waivers` entirely keeps the original behaviour — waive
    everything, one shared reason — which is what the Due Payments screen's
    wholesale Terminate still does, and what installment plans always do.

    `waived_status` says which kind of forgiveness this is. A manager making a
    service inactive passes CANCELLED; the admin refund path's WRITTEN_OFF
    stays the default. Both are excluded from Outstanding Due — the difference
    is only that Admin can tell, later, which decision produced the drop.
    """
    unpaid_manager = (
        container.bills
        if isinstance(container, MonthlyEnrollment)
        else container.installments
    )
    # `outstanding > 0` rather than status alone: a partially-paid item still
    # has a balance to forgive, and leaving it DUE would keep the money in
    # Outstanding Due after the plan is closed.
    unpaid = [
        item
        for item in unpaid_manager.exclude(status__in=FORGIVEN_STATUSES)
        if item.outstanding > 0
    ]

    if waivers is None:
        waived = unpaid
        kept = []
    else:
        waived = [item for item in unpaid if item.pk in waivers]
        kept = [item for item in unpaid if item.pk not in waivers]

    outstanding = sum((item.outstanding for item in waived), Decimal("0.00"))
    kept_total = sum((item.outstanding for item in kept), Decimal("0.00"))

    if waived:
        # Month by month, with the manager's own words against each — a partial
        # write-off has to be at least as legible in the audit log as a
        # wholesale one.
        #
        # Built **before** the statuses change, because `outstanding` is zero
        # for a forgiven item by definition. Reading it afterwards recorded
        # every cancelled month as ৳0.00 — a breakdown that named the months
        # correctly and said each of them cost nothing, which is worse than no
        # breakdown at all.
        breakdown = [
            {
                "label": item.label,
                "amount": str(item.outstanding),
                "reason": (waivers or {}).get(item.pk, "") or reason,
            }
            for item in waived
        ]

        for item in waived:
            item.status = waived_status
            item.save(update_fields=["status"])

        audit.record(
            actor=actor,
            action=AuditLog.Action.WRITE_OFF,
            target=container,
            branch=container.branch,
            reason=reason or "Cancelled when the service was made inactive",
            changes={
                "writtenOff": str(outstanding),
                "kind": waived_status,
                "months": breakdown,
            },
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

    changes = {}
    if outstanding > 0:
        changes["writtenOff"] = str(outstanding)
    if kept_total > 0:
        # Still owed, and still collectable if the patient comes back — the
        # figure the Terminated Services screen reports.
        changes["keptDue"] = str(kept_total)
        changes["keptMonths"] = [item.label for item in kept]

    audit.record(
        actor=actor,
        action=AuditLog.Action.TERMINATE,
        target=container,
        branch=container.branch,
        reason=reason,
        changes=changes or None,
    )


@transaction.atomic
def stop_monthly_service(*, actor, enrollment, decisions: dict, reason: str = ""):
    """
    Make one monthly service inactive, deciding each unpaid month separately.

    Only this service stops. The patient's other services are untouched, and
    the patient record itself is not deactivated — someone can stop monthly
    therapy and keep paying off an installment package in the same week.

    `decisions` is `{bill_id: {"action": "keep"|"waive", "reason": "..."}}`,
    one entry for every month that has actually fallen due and is still
    unpaid. Three rules make this safe:

    **Every arrived unpaid month needs an explicit decision.** No silent
    default — defaulting to keep quietly leaves a debt the manager thought
    they had forgiven, and defaulting to waive is how thousands of taka get
    written off by accident.

    **Waiving needs a written reason.** The record exists so Admin can see why
    the amount owed went down; without a reason it records that it went down
    and nothing else, which is the part that mattered.

    **Months that never arrived are dropped, not decided.** An enrollment
    carries a lookahead of future bills nobody has been asked to pay yet;
    asking the manager to keep-or-waive November in September is asking about
    money that was never owed. They are deleted, exactly as the nightly
    unpaid-due job already does, and for the same reason: no payment, no
    receipt and no audit entry ever pointed at one.
    """
    if enrollment.status != EnrollmentStatus.ACTIVE:
        raise EnrollmentError("This service is not running.", code="not_active")

    current = month_key(timezone.localdate())

    # Never-payable lookahead first, so it is out of the way before anything
    # is validated against it.
    enrollment.bills.filter(
        month__gt=current, status=BillStatus.UPCOMING, amount_paid=Decimal("0.00")
    ).delete()

    arrived = [bill for bill in enrollment.unpaid_bills() if bill.month <= current]
    undecided = [bill for bill in arrived if bill.pk not in decisions]
    if undecided:
        raise EnrollmentError(
            "Decide what happens to every unpaid month before stopping.",
            code="decisions_required",
            extra={"months": [bill.label for bill in undecided]},
        )

    unknown = set(decisions) - {bill.pk for bill in arrived}
    if unknown:
        raise EnrollmentError(
            "Some decisions do not belong to this service.",
            code="unknown_bill",
            extra={"billIds": sorted(str(pk) for pk in unknown)},
        )

    waivers = {}
    for bill in arrived:
        decision = decisions[bill.pk]
        if decision.get("action") != "waive":
            continue
        why = (decision.get("reason") or "").strip()
        if not why:
            raise EnrollmentError(
                f"Say why {bill.label}'s due is being waived.",
                code="waive_reason_required",
                extra={"billId": str(bill.pk), "label": bill.label},
            )
        waivers[bill.pk] = why

    terminate(
        actor=actor, container=enrollment, reason=reason, waivers=waivers,
        waived_status=BillStatus.CANCELLED,
    )
    enrollment.refresh_from_db()
    return enrollment


def stoppable_installments(plan) -> dict:
    """
    The same three groups for an installment plan, in its own vocabulary.

    A plan is one agreed debt on a fixed schedule, so there is no lookahead to
    drop and nothing can be prepaid beyond it — every unpaid installment has
    been agreed to and needs a keep-or-cancel decision. Returning the same
    shape as the monthly version lets one dialog serve both.
    """
    unpaid = [i for i in plan.unpaid_installments() if i.outstanding > 0]
    return {
        "owed": [
            {
                "billId": str(item.pk),
                "month": "",
                "label": item.label,
                "amount": item.outstanding,
                "status": item.effective_status(),
            }
            for item in unpaid
        ],
        "owedTotal": sum((item.outstanding for item in unpaid), Decimal("0.00")),
        "prepaid": [],
        "prepaidTotal": Decimal("0.00"),
        "droppedMonths": [],
    }


@transaction.atomic
def stop_installment_plan(*, actor, plan, decisions: dict, reason: str = ""):
    """
    Make one installment service inactive, deciding each unpaid part.

    Deliberately the same rules as the monthly version — every unpaid item
    needs an explicit decision and every cancellation needs a written reason —
    because a manager should not find that forgiving ৳20,000 of an
    installment plan asks less of them than forgiving one ৳5,000 month.
    """
    if plan.status != EnrollmentStatus.ACTIVE:
        raise EnrollmentError("This service is not running.", code="not_active")

    unpaid = [i for i in plan.unpaid_installments() if i.outstanding > 0]

    undecided = [item for item in unpaid if item.pk not in decisions]
    if undecided:
        raise EnrollmentError(
            "Decide what happens to every unpaid instalment before making "
            "this service inactive.",
            code="decisions_required",
            extra={"months": [item.label for item in undecided]},
        )

    unknown = set(decisions) - {item.pk for item in unpaid}
    if unknown:
        raise EnrollmentError(
            "Some decisions do not belong to this service.",
            code="unknown_bill",
            extra={"billIds": sorted(str(pk) for pk in unknown)},
        )

    waivers = {}
    for item in unpaid:
        decision = decisions[item.pk]
        if decision.get("action") != "waive":
            continue
        why = (decision.get("reason") or "").strip()
        if not why:
            raise EnrollmentError(
                f"Say why {item.label} is being cancelled.",
                code="waive_reason_required",
                extra={"billId": str(item.pk), "label": item.label},
            )
        waivers[item.pk] = why

    terminate(
        actor=actor, container=plan, reason=reason, waivers=waivers,
        waived_status=BillStatus.CANCELLED,
    )
    plan.refresh_from_db()
    return plan


def stoppable_months(enrollment) -> dict:
    """
    What the Stop Service dialog has to show before it can ask anything.

    Three groups, because each is a different question: months that are owed
    and need a decision, months already paid in advance (money the clinic is
    holding for service it will now not deliver), and the lookahead that will
    simply be dropped.
    """
    current = month_key(timezone.localdate())
    unpaid = list(enrollment.unpaid_bills())

    owed = [bill for bill in unpaid if bill.month <= current]
    prepaid = list(
        enrollment.bills.filter(
            month__gt=current, amount_paid__gt=Decimal("0.00")
        ).order_by("month")
    )
    dropped = list(
        enrollment.bills.filter(
            month__gt=current, status=BillStatus.UPCOMING, amount_paid=Decimal("0.00")
        ).order_by("month")
    )

    def row(bill):
        return {
            "billId": str(bill.pk),
            "month": bill.month,
            "label": bill.label,
            "amount": bill.outstanding if bill in owed else bill.amount_paid,
            "status": bill.effective_status(),
        }

    return {
        "owed": [row(bill) for bill in owed],
        "owedTotal": sum((bill.outstanding for bill in owed), Decimal("0.00")),
        "prepaid": [row(bill) for bill in prepaid],
        "prepaidTotal": sum((bill.amount_paid for bill in prepaid), Decimal("0.00")),
        "droppedMonths": [bill.label for bill in dropped],
    }


def advance_month_options(enrollment, *, count: int | None = None) -> dict:
    """
    The future months a manager may tick on the Advance Payment screen.

    Every month is offered independently — October and December with November
    left alone is a real choice a patient makes, so the screen cannot present
    a single "pay through" slider. A month already settled comes back marked
    `covered` and unselectable rather than being hidden, because "December is
    already paid" is the answer the manager is looking for.

    Arrears are not in this list. They are collected on the Due Payments
    screen, and `collect_monthly_advance` refuses while any exist.
    """
    horizon = count or settings.MAX_ADVANCE_MONTHS
    today = timezone.localdate()
    covered = {
        bill.month: bill
        for bill in enrollment.bills.filter(month__gt=month_key(today))
    }

    options = []
    for offset in range(1, horizon + 1):
        month_date = add_months(today.replace(day=1), offset)
        key = month_key(month_date)
        bill = covered.get(key)
        options.append(
            {
                "month": key,
                "label": month_label(month_date),
                "amount": bill.amount if bill else enrollment.service.fee,
                "covered": bool(bill and bill.is_settled),
                "status": bill.effective_status() if bill else "",
            }
        )

    # Surfaced alongside the options so the screen can explain *why* the
    # Confirm button is disabled, instead of just refusing.
    outstanding = patient_outstanding_dues(enrollment.patient)
    return {
        "months": options,
        "fee": enrollment.service.fee,
        "outstandingTotal": outstanding["total"],
        "outstandingItems": outstanding["items"],
    }


def preview_monthly_advance(*, enrollment, months: list[str]) -> dict:
    """What the ticked months would cost, before anyone commits to it."""
    rows = _validate_advance_months(enrollment, months)
    return {
        "months": [
            {"month": key, "label": label, "amount": amount}
            for key, label, amount in rows
        ],
        "total": sum((amount for _, _, amount in rows), Decimal("0.00")),
    }


def _validate_advance_months(enrollment, months) -> list[tuple]:
    """
    Check the ticked months and price them, or say exactly what is wrong.

    Shared by the preview and the collection so the two can never disagree
    about which months are payable — a preview that accepts what the
    collection then refuses is worse than no preview.
    """
    if not months:
        raise EnrollmentError("Pick at least one month.", code="no_months")

    today = timezone.localdate()
    current = month_key(today)
    limit = month_key(add_months(today, settings.MAX_ADVANCE_MONTHS))

    existing = {bill.month: bill for bill in enrollment.bills.all()}
    rows = []
    for key in sorted(set(months)):
        try:
            year, month = (int(part) for part in str(key).split("-"))
            month_date = date(year, month, 1)
        except (ValueError, TypeError):
            raise EnrollmentError(
                "Expected a month as YYYY-MM.", code="invalid_month",
                extra={"month": str(key)},
            ) from None

        if key <= current:
            raise EnrollmentError(
                f"{month_label(month_date)} is not a future month.",
                code="not_future", extra={"month": key},
            )
        if key > limit:
            raise EnrollmentError(
                f"Payment can be taken at most {settings.MAX_ADVANCE_MONTHS} "
                "months ahead.",
                code="too_far_ahead", extra={"month": key},
            )

        bill = existing.get(key)
        if bill is not None and bill.is_settled:
            # The unique (enrollment, month) constraint stops a duplicate row;
            # this stops a duplicate *charge* against the row that exists.
            raise EnrollmentError(
                f"{month_label(month_date)} has already been paid.",
                code="already_paid", extra={"month": key},
            )

        rows.append(
            (
                key,
                month_label(month_date),
                bill.outstanding if bill is not None else enrollment.service.fee,
            )
        )
    return rows


@transaction.atomic
def collect_monthly_advance(
    *, actor, branch, enrollment, months: list[str], method: str,
    idempotency_key: str | None = None,
):
    """
    Take payment for specific future months, chosen one by one.

    **Arrears first, always.** Nothing may be paid ahead while the patient
    still owes anything — the manager clears that on the Due Payments screen.
    That is not tidiness: it is what stops a patient who has just handed over
    three months of cash from being caught by an unpaid current month, and it
    is why the oldest-first rule needs no exception here.

    With arrears settled, each ticked month is created and paid in ascending
    order, so the row being charged is the oldest unpaid one at that moment
    and `_assert_is_oldest_unpaid` is satisfied by construction rather than
    bypassed. Months that were *not* ticked are simply never created — they
    are not skipped dues, they do not exist yet, and the monthly job will
    raise them when they arrive.

    Each month keeps its own payment and receipt, so revenue stays
    attributable to the month it belongs to.
    """
    if not enrollment.is_active:
        raise EnrollmentError("This enrollment has been terminated.", code="terminated")

    # An idempotency-key replay must return the ORIGINAL payments, checked
    # before validation rather than after: by the time a retry arrives the
    # months it names are settled, so validation would reject a legitimate
    # replay with "already paid" instead of handing back the receipts the
    # first attempt produced. Same ordering, and the same reason, as
    # `collect_bill_payment`.
    if idempotency_key:
        keys = [_advance_leg_key(idempotency_key, m) for m in sorted(set(months))]
        replayed = list(
            Payment.all_objects.filter(idempotency_key__in=keys).order_by("id")
        )
        if len(replayed) == len(keys):
            return replayed, enrollment

    rows = _validate_advance_months(enrollment, months)

    outstanding = enrollment.outstanding_total()
    if outstanding > 0:
        raise EnrollmentError(
            "Clear what is already owed before taking payment for future "
            "months.",
            code="arrears_first",
            extra={"outstanding": str(outstanding)},
        )

    payments = []
    for key, label, _amount in rows:
        bill, _ = MonthlyBill.objects.get_or_create(
            enrollment=enrollment,
            month=key,
            defaults={
                "label": label,
                "amount": enrollment.service.fee,
                "due_date": due_date_for_month(key),
                "status": BillStatus.UPCOMING,
            },
        )
        payment, _ = collect_bill_payment(
            actor=actor, branch=branch, bill=bill, method=method,
            idempotency_key=_advance_leg_key(idempotency_key, key),
        )
        payments.append(payment)

    enrollment.refresh_from_db()
    return payments, enrollment




def _advance_leg_key(idempotency_key: str | None, month: str) -> str | None:
    """
    A deterministic key per month.

    The loop mints one payment per month, so a single client key cannot
    protect them all — but a key derived from it and the month can, and
    deterministically, so replaying the whole request returns the original
    payments instead of charging again. Hashed because `idempotency_key` is
    capped at 64 characters and the client's own key may already fill it.
    """
    if not idempotency_key:
        return None
    digest = hashlib.sha1(f"{idempotency_key}:{month}".encode()).hexdigest()
    return f"adv-{digest}"


# ---------------------------------------------------------------------------
# Scheduled bill generation
# ---------------------------------------------------------------------------


@transaction.atomic
def generate_due_bills(*, up_to: date | None = None) -> dict:
    """
    Extend every active enrollment's billing to the current month.

    Three properties this must have, all tested:

    **Idempotent** — running twice in a month creates no duplicate. Guaranteed
    by the unique constraint on (enrollment, month) rather than by trusting
    the scheduler to fire exactly once.

    **Catch-up capable** — if the server was down on the 1st, the next run
    backfills the missed months instead of skipping them. That's why this
    walks from each enrollment's last bill to the target month rather than
    just adding "this month".

    **Never bills a month that was paid in advance twice.** The prepaid row is
    already there, so `get_or_create` finds it — but the walk has to start
    from the last bill *at or before* the target, not the last bill outright.
    With December prepaid and November missing, starting from December would
    put the cursor in January, sail past the target, and November would never
    be billed at all. Advance payment is precisely what makes an enrollment's
    months sparse, so the walk cannot assume they are contiguous.
    """
    target = (up_to or timezone.localdate()).replace(day=1)
    created_count = 0
    skipped = 0

    # Months paid ahead that have now arrived become ordinary paid months.
    # One bulk update, idempotent, and catch-up capable like the rest of this
    # job. `effective_status` derives the same answer, so a missed run is
    # cosmetic rather than a correctness bug — this keeps the stored column
    # honest for anything that filters on it directly.
    settled_advances = MonthlyBill.objects.filter(
        status=BillStatus.ADVANCE, month__lte=month_key(target)
    ).update(status=BillStatus.PAID)

    enrollments = MonthlyEnrollment.objects.filter(
        status=EnrollmentStatus.ACTIVE
    ).select_related("service")

    target_key = month_key(target)
    for enrollment in enrollments:
        if not enrollment.bills.exists():
            skipped += 1
            continue

        # The last month that has actually arrived — months prepaid beyond
        # the target are deliberately stepped over, not treated as the end of
        # the series.
        last = (
            enrollment.bills.filter(month__lte=target_key).order_by("-month").first()
        )
        if last is None:
            # Everything this enrollment owns lies in the future: it was
            # created by an advance payment ahead of its own first cycle.
            # Nothing to bill yet.
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

    return {
        "created": created_count,
        "enrollments_without_bills": skipped,
        "advances_settled": settled_advances,
    }


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
def resume_monthly_service(*, actor, enrollment: MonthlyEnrollment):
    """
    Reactivate a service that had been made inactive.

    **The debt is cleared first, not folded in.** Reactivation is refused
    while the patient owes anything, anywhere — the manager collects it on the
    Due Payments screen and comes back. That replaces the old two-option
    resume, which let the arrears be waived at this moment instead: a due that
    had already survived an explicit keep decision could then be forgiven a
    second time with no fresh justification, quietly undoing the reason the
    keep-or-cancel choice exists.

    Months cancelled when the service stopped stay cancelled. They are not
    resurrected, and the gap months are not backfilled: the patient was not
    enrolled during them, and billing for service nobody delivered is the one
    thing this must not do. Billing restarts at the current month.
    """
    if enrollment.status != EnrollmentStatus.TERMINATED:
        raise EnrollmentError(
            "This service is already active.", code="not_terminated"
        )

    _assert_no_outstanding_dues(
        enrollment.patient, action="have a service reactivated"
    )

    enrollment.status = EnrollmentStatus.ACTIVE
    enrollment.terminated_at = None
    enrollment.terminated_kind = ""
    enrollment.terminated_month = ""
    enrollment.save(
        update_fields=["status", "terminated_at", "terminated_kind", "terminated_month"]
    )

    reinstated = _open_cycle_from(enrollment, timezone.localdate())

    changes = {
        "status": {"from": EnrollmentStatus.TERMINATED, "to": EnrollmentStatus.ACTIVE},
    }
    if reinstated:
        # Reopening a forgiven month is never allowed to be silent, even when
        # it is the right thing to do — this is only ever the current month,
        # for service now being delivered again.
        changes["reinstatedMonths"] = reinstated

    audit.record(
        actor=actor,
        action=AuditLog.Action.UPDATE,
        target=enrollment,
        branch=enrollment.branch,
        reason="Service reactivated",
        changes=changes,
    )

    enrollment.refresh_from_db()
    return enrollment


@transaction.atomic
def resume_installment_plan(*, actor, plan):
    """Reactivate an installment service, on the same terms as a monthly one."""
    if plan.status != EnrollmentStatus.TERMINATED:
        raise EnrollmentError("This service is already active.", code="not_terminated")

    _assert_no_outstanding_dues(plan.patient, action="have a service reactivated")

    plan.status = EnrollmentStatus.ACTIVE
    plan.terminated_at = None
    plan.save(update_fields=["status", "terminated_at"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.UPDATE,
        target=plan,
        branch=plan.branch,
        reason="Service reactivated",
        changes={
            "status": {"from": EnrollmentStatus.TERMINATED, "to": EnrollmentStatus.ACTIVE}
        },
    )
    plan.refresh_from_db()
    return plan


def _open_cycle_from(
    enrollment: MonthlyEnrollment, day: date, months_ahead: int = 1
) -> list[str]:
    """
    Start billing again at `day`'s month, on the same terms a fresh enrollment
    gets — the current month and nothing beyond it. Returns the months that
    had to be reinstated.

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

        if bill.status in FORGIVEN_STATUSES and bill.amount_paid <= 0:
            bill.status = status
            bill.save(update_fields=["status"])
            reinstated.append(bill.label)

    return reinstated
