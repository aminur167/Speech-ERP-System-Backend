"""
Payment creation and the void/refund lifecycle.

Every money-moving flow in the system routes through `create_payment` here, so
receipt numbering, idempotency and audit behave identically whether the money
came from a daily service, a bill, a booking or the materials counter.
"""

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.branches.models import Branch
from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.notifications.inapp import notify_admins, notify_requester
from apps.payments.models import (
    Payment,
    PaymentCategory,
    PaymentStatus,
    RefundRequest,
)


class PaymentError(Exception):
    """Business-rule violation in a payment operation."""

    def __init__(self, message: str, *, code: str = "invalid", extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}


def _allocate_codes(branch: Branch, year: int) -> tuple[str, str]:
    """
    Reserve this branch's next receipt and transaction numbers.

    Per-branch series so an offline device never has to coordinate with
    another branch. Must run inside the caller's transaction — the numbers and
    the row they label have to commit together, or a later failure burns codes
    and leaves gaps in a sequence people read.
    """
    receipt_value = next_value(f"receipt:{branch.code}", year)
    txn_value = next_value(f"txn:{branch.code}", year)

    suffix = branch.short_code
    receipt = f"RCPT-{suffix}-{year}-{str(receipt_value).zfill(5)}"
    transaction_id = f"TXN-{suffix}-{year}-{str(txn_value).zfill(5)}"
    return receipt, transaction_id


@transaction.atomic
def create_payment(
    *,
    actor,
    branch: Branch,
    patient,
    amount: Decimal,
    method: str,
    category: str = "",
    description: str = "",
    idempotency_key: str | None = None,
    client_created_at=None,
    status: str = PaymentStatus.PAID,
) -> tuple[Payment, bool]:
    """
    Record a payment. Returns `(payment, created)`.

    `created` is False when an idempotency key replayed — the caller gets the
    original payment back rather than a second charge. That distinction
    matters for composite flows (a replayed material sale must not deduct
    stock twice).
    """
    if idempotency_key:
        existing = Payment.all_objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing, False

    if amount is None or Decimal(amount) <= Decimal("0"):
        raise PaymentError("Payment amount must be greater than zero.", code="invalid_amount")

    year = timezone.localdate().year
    receipt_number, transaction_id = _allocate_codes(branch, year)

    payment = Payment.objects.create(
        transaction_id=transaction_id,
        receipt_number=receipt_number,
        patient=patient,
        amount=amount,
        method=method,
        status=status,
        category=category,
        description=description,
        collected_by=actor if actor and actor.is_authenticated else None,
        branch=branch,
        idempotency_key=idempotency_key or None,
        client_created_at=client_created_at,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=payment,
        branch=branch,
        changes={
            "receipt_number": payment.receipt_number,
            "amount": str(payment.amount),
            "method": payment.method,
        },
    )
    return payment, True


# ---------------------------------------------------------------------------
# Void
# ---------------------------------------------------------------------------


def _closing_submitted_for(branch: Branch, day: date) -> bool:
    """
    Has this branch signed off that day's cash yet?

    Imported lazily so payments doesn't hard-depend on the daily-closing app
    at module load; the modules are built in separate phases.
    """
    try:
        from apps.dailyclosing.models import DailyClosing
    except (ImportError, LookupError):
        return False
    return DailyClosing.objects.filter(branch=branch, date=day).exists()


@transaction.atomic
def void_payment(*, actor, payment: Payment, reason: str) -> Payment:
    """
    Cancel a payment that never really happened.

    Managers may only void the current day's payments, and only before that
    day's closing is submitted. Before closing, the cash is still being
    counted and a correction is bookkeeping; after closing, the day has been
    reconciled and signed off, so changing it would invalidate the
    reconciliation. From that point the path is a refund.
    """
    if not reason or not reason.strip():
        raise PaymentError("A reason is required to void a payment.", code="reason_required")

    if payment.status in {PaymentStatus.VOID, PaymentStatus.REFUNDED}:
        raise PaymentError(
            f"This payment is already {payment.status}.", code="already_settled"
        )

    if actor.is_manager:
        today = timezone.localdate()
        payment_day = timezone.localtime(payment.created_at).date()

        if payment_day != today:
            raise PaymentError(
                "Only today's payments can be voided. Request a refund instead.",
                code="not_same_day",
            )

        if _closing_submitted_for(payment.branch, payment_day):
            raise PaymentError(
                "Today's closing has already been submitted, so this payment can no "
                "longer be voided. Request a refund instead.",
                code="closing_submitted",
            )

    payment.status = PaymentStatus.VOID
    payment.save(update_fields=["status", "updated_at"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.VOID,
        target=payment,
        branch=payment.branch,
        reason=reason,
        changes={"status": {"from": PaymentStatus.PAID, "to": PaymentStatus.VOID}},
    )
    return payment


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


@transaction.atomic
def request_refund(
    *, actor, payment: Payment, amount: Decimal, reason: str, items: list[dict] | None = None
) -> RefundRequest:
    """
    Open a refund request. Nothing on the payment changes until an admin
    approves — that gap is the whole point of the control.

    For material sales the amount is recomputed from the original sale's
    stored prices; a client-supplied total is never trusted.
    """
    if not reason or not reason.strip():
        raise PaymentError("A reason is required to request a refund.", code="reason_required")

    if payment.status in {PaymentStatus.VOID, PaymentStatus.REFUNDED}:
        raise PaymentError(
            f"This payment is already {payment.status} and cannot be refunded.",
            code="already_settled",
        )

    if payment.refund_requests.filter(status=RefundRequest.Status.PENDING).exists():
        raise PaymentError(
            "A refund request is already pending on this payment.", code="already_pending"
        )

    resolved_amount, resolved_items = _resolve_refund_amount(payment, amount, items)

    if resolved_amount > payment.refundable_amount:
        raise PaymentError(
            f"Only {payment.refundable_amount} remains refundable on this payment.",
            code="exceeds_refundable",
        )

    request = RefundRequest.objects.create(
        payment=payment,
        amount=resolved_amount,
        reason=reason,
        requested_by=actor,
        branch=payment.branch,
    )

    if resolved_items:
        from apps.payments.models import RefundRequestItem

        RefundRequestItem.objects.bulk_create(
            [
                RefundRequestItem(
                    refund_request=request,
                    material_id=item["material_id"],
                    quantity=item["quantity"],
                    unit_price=item["unit_price"],
                )
                for item in resolved_items
            ]
        )

    audit.record(
        actor=actor,
        action=AuditLog.Action.REFUND_REQUEST,
        target=request,
        branch=payment.branch,
        reason=reason,
        changes={"payment": payment.receipt_number, "amount": str(resolved_amount)},
    )

    notify_admins(
        title="Refund needs approval",
        message=(
            f"{payment.branch.name} requested a {resolved_amount} refund on "
            f"{payment.receipt_number} — {reason}"
        ),
        link="/admin/refund-approvals",
        exclude=actor,
    )
    return request


def _resolve_refund_amount(payment: Payment, amount, items) -> tuple[Decimal, list[dict]]:
    """
    Work out what this refund is actually worth.

    For material sales with line items, the total is derived from the recorded
    sale prices — never from the request body. Same rule as the sale itself:
    prices come from the server's records, so a tampered payload can't refund
    more than was charged.
    """
    if not items:
        return Decimal(amount), []

    from apps.materials.models import MaterialSaleItem

    sold = {
        line.material_id: line
        for line in MaterialSaleItem.objects.filter(payment=payment)
    }
    if not sold:
        raise PaymentError(
            "This payment has no material lines to refund.", code="no_sale_items"
        )

    already_refunded: dict[str, int] = {}
    from apps.payments.models import RefundRequestItem

    for prior in RefundRequestItem.objects.filter(
        refund_request__payment=payment,
        refund_request__status__in=[
            RefundRequest.Status.PENDING,
            RefundRequest.Status.APPROVED,
        ],
    ):
        already_refunded[prior.material_id] = (
            already_refunded.get(prior.material_id, 0) + prior.quantity
        )

    total = Decimal("0.00")
    resolved: list[dict] = []

    for item in items:
        material_id = item["material_id"]
        quantity = int(item["quantity"])

        if quantity <= 0:
            raise PaymentError("Refund quantity must be at least 1.", code="invalid_quantity")

        line = sold.get(material_id)
        if line is None:
            raise PaymentError(
                "That item was not part of this sale.", code="item_not_in_sale"
            )

        remaining = line.quantity - already_refunded.get(material_id, 0)
        if quantity > remaining:
            raise PaymentError(
                f"Only {remaining} of that item remain refundable on this receipt.",
                code="exceeds_sold_quantity",
            )

        total += line.unit_price * quantity
        resolved.append(
            {
                "material_id": material_id,
                "quantity": quantity,
                "unit_price": line.unit_price,
            }
        )

    return total, resolved


@transaction.atomic
def approve_refund(
    *, actor, request: RefundRequest, bill_action: str, refund_method: str = "",
    review_note: str = "",
) -> RefundRequest:
    """
    Approve a refund and apply its consequences atomically.

    Everything happens together: the request is marked approved, the payment's
    status moves, any bill reopens or is written off, and material stock
    returns. A partial failure that refunded the money without restoring stock
    (or vice versa) would leave the books wrong in a way nobody would notice.
    """
    if request.status != RefundRequest.Status.PENDING:
        raise PaymentError(
            f"This request has already been {request.status}.", code="already_reviewed"
        )

    request.status = RefundRequest.Status.APPROVED
    request.reviewed_by = actor
    request.reviewed_at = timezone.now()
    request.review_note = review_note or ""
    request.bill_action = bill_action
    request.refund_method = refund_method or request.payment.method
    request.save()

    payment = request.payment

    # Fully returned → refunded. Anything still outstanding → partial.
    payment.status = (
        PaymentStatus.REFUNDED
        if payment.refunded_amount >= payment.amount
        else PaymentStatus.PARTIAL
    )
    payment.save(update_fields=["status", "updated_at"])

    _apply_refund_side_effects(actor=actor, request=request)

    audit.record(
        actor=actor,
        action=AuditLog.Action.REFUND_APPROVE,
        target=request,
        branch=request.branch,
        reason=review_note or request.reason,
        changes={
            "payment": payment.receipt_number,
            "amount": str(request.amount),
            "bill_action": bill_action,
        },
    )

    notify_requester(
        actor=actor,
        recipient=request.requested_by,
        title="Refund approved",
        message=(
            f"The {request.amount} refund on {payment.receipt_number} was approved."
            + (f" — {review_note}" if review_note.strip() else "")
        ),
        link="/manager/transactions",
    )
    return request


def _apply_refund_side_effects(*, actor, request: RefundRequest) -> None:
    """Return stock for material sales; reopen or write off bills for the rest."""
    payment = request.payment

    if payment.category == PaymentCategory.MATERIAL_SALE:
        _restore_material_stock(actor=actor, request=request)
        return

    if payment.category in {PaymentCategory.MONTHLY, PaymentCategory.INSTALLMENT}:
        _apply_bill_action(actor=actor, request=request)


def _restore_material_stock(*, actor, request: RefundRequest) -> None:
    """
    Put returned quantities back into sellable stock.

    Note for the clinic: a *damaged* return also lands back in stock. Writing
    it off is a separate stock-out adjustment with a note — the system can't
    tell the difference and shouldn't guess.
    """
    from apps.materials.models import MaterialMovement, MaterialSaleItem

    lines = list(request.items.all())
    if not lines:
        # Full refund with no explicit lines — return everything on the sale.
        lines = [
            type("Line", (), {"material_id": s.material_id, "quantity": s.quantity})()
            for s in MaterialSaleItem.objects.filter(payment=request.payment)
        ]

    from apps.materials.models import Material

    for line in lines:
        material = Material.objects.select_for_update().get(pk=line.material_id)
        material.quantity += line.quantity
        material.save(update_fields=["quantity"])

        MaterialMovement.objects.create(
            material=material,
            type=MaterialMovement.Type.IN,
            quantity=line.quantity,
            note=f"Refund — {request.payment.receipt_number}",
            branch=request.branch,
            created_by=actor,
        )


def _apply_bill_action(*, actor, request: RefundRequest) -> None:
    """
    Reopen the bill this payment settled, or write it off.

    Reopen is the normal case: the patient has their money back, so the debt
    exists again and must reappear in Outstanding Due. Write-off deliberately
    forgives it, and is the only sanctioned way to close an enrollment whose
    dues will never be collected (see docs/05 — termination is otherwise
    blocked while anything is outstanding).
    """
    from apps.enrollments.models import Installment, MonthlyBill

    bill = MonthlyBill.objects.filter(payment=request.payment).first()
    installment = None
    if bill is None:
        installment = Installment.objects.filter(payment=request.payment).first()

    target = bill or installment
    if target is None:
        return

    if request.bill_action == RefundRequest.BillAction.WRITE_OFF:
        target.status = target.Status.WRITTEN_OFF
        target.save(update_fields=["status"])

        audit.record(
            actor=actor,
            action=AuditLog.Action.WRITE_OFF,
            target=target,
            branch=request.branch,
            reason=request.reason,
        )
        return

    # Reopen for the refunded portion only. A ৳2,000 refund against a ৳5,000
    # bill leaves ৳2,000 owing, not ৳5,000 — summing the full amount here is
    # the overstatement bug docs/04 warns about.
    target.amount_paid = max(Decimal("0.00"), target.amount_paid - request.amount)
    target.status = target.Status.PAID if target.is_settled else target.Status.DUE
    if not target.is_settled:
        target.paid_at = None
    target.save(update_fields=["amount_paid", "status", "paid_at"])


@transaction.atomic
def reject_refund(*, actor, request: RefundRequest, review_note: str) -> RefundRequest:
    """Decline a refund. The payment is untouched."""
    if not review_note or not review_note.strip():
        raise PaymentError(
            "A reason is required when rejecting a refund request.", code="note_required"
        )

    if request.status != RefundRequest.Status.PENDING:
        raise PaymentError(
            f"This request has already been {request.status}.", code="already_reviewed"
        )

    request.status = RefundRequest.Status.REJECTED
    request.reviewed_by = actor
    request.reviewed_at = timezone.now()
    request.review_note = review_note
    request.save()

    audit.record(
        actor=actor,
        action=AuditLog.Action.REFUND_REJECT,
        target=request,
        branch=request.branch,
        reason=review_note,
    )

    notify_requester(
        actor=actor,
        recipient=request.requested_by,
        title="Refund rejected",
        message=(
            f"The {request.amount} refund on {request.payment.receipt_number} "
            f"was rejected — {review_note}"
        ),
        link="/manager/transactions",
    )
    return request
