"""
Material stock operations and the POS sale.

⚠️ The security fix this module exists to make:

The frontend mock sends `unitPrice` from the browser and trusts it
(`sellMaterials` computes the total from the client's cart). Anyone able to
edit a request could buy a ৳2,500 assessment kit for ৳1. **Prices here are
always read from the database**; the client sends only material ids and
quantities, and the server computes the total.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.materials.models import Material, MaterialMovement, MaterialSaleItem
from apps.payments import services as payment_services
from apps.payments.models import PaymentCategory


class MaterialError(Exception):
    def __init__(self, message: str, *, code: str = "invalid", extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}


@transaction.atomic
def create_material(*, actor, branch, data: dict) -> Material:
    value = next_value("material", 0)
    material = Material.objects.create(
        code=f"MAT-{str(value).zfill(5)}", branch=branch, **data
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=material,
        branch=branch,
        changes={"code": material.code, "name": material.name},
    )
    return material


@transaction.atomic
def adjust_stock(
    *, actor, material: Material, movement_type: str, quantity: int, note: str = "",
    idempotency_key: str | None = None, client_created_at=None,
):
    """
    Move stock in or out, recording why.

    The row is locked for the update so two simultaneous adjustments can't both
    read the same starting quantity and lose one of the changes.

    A replayed idempotency key must not move stock a second time -- checked
    before the lock is even taken, same as every other offline-replayable
    action (docs/00).
    """
    if idempotency_key:
        existing = MaterialMovement.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing.material

    if quantity <= 0:
        raise MaterialError("Quantity must be at least 1.", code="invalid_quantity")

    # The branch comes from the material the caller already has (the view
    # loaded it with the material); the locking re-read below would otherwise
    # leave it to be fetched again for the movement and the audit entry.
    branch = material.branch
    material = Material.objects.select_for_update().get(pk=material.pk)
    material.branch = branch

    delta = quantity if movement_type == MaterialMovement.Type.IN else -quantity
    new_quantity = material.quantity + delta

    if new_quantity < 0:
        raise MaterialError(
            f"Only {material.quantity} in stock — cannot remove {quantity}.",
            code="insufficient_stock",
            extra={"available": material.quantity},
        )

    material.quantity = new_quantity
    material.save(update_fields=["quantity"])

    MaterialMovement.objects.create(
        material=material,
        type=movement_type,
        quantity=quantity,
        note=note,
        branch=material.branch,
        created_by=actor,
        idempotency_key=idempotency_key or None,
        client_created_at=client_created_at,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.UPDATE,
        target=material,
        branch=material.branch,
        reason=note or f"Stock {movement_type}",
        changes={"quantity": {"from": material.quantity - delta, "to": new_quantity}},
    )
    return material


@transaction.atomic
def sell_materials(
    *, actor, branch, patient, items: list[dict], method: str,
    idempotency_key: str | None = None,
):
    """
    POS checkout: deduct stock, record the lines, and take payment — all or
    nothing.

    `items` carries only `{material_id, quantity}`. Prices come from the
    database; a client-supplied price is never used.

    Everything is one transaction so a failure partway can't leave stock
    deducted with no payment, or a payment with no stock movement.
    """
    if not items:
        raise MaterialError("The cart is empty.", code="empty_cart")

    # Replay of an already-processed sale: return the original payment without
    # deducting stock a second time.
    if idempotency_key:
        from apps.payments.models import Payment

        existing = Payment.all_objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing, list(existing.sale_items.all()), False

    # Every material in the cart, locked in ONE statement and in id order --
    # not a locking query per line, and the same order for every concurrent
    # sale, so two carts sharing items queue instead of deadlocking.
    wanted_ids = [item["material_id"] for item in items]
    materials = {
        material.pk: material
        for material in Material.objects.select_for_update()
        .filter(pk__in=wanted_ids, branch=branch)
        .order_by("pk")
    }

    total = Decimal("0.00")
    lines: dict[int, dict] = {}  # one line per material, in cart order

    for item in items:
        quantity = int(item["quantity"])
        if quantity <= 0:
            raise MaterialError("Quantity must be at least 1.", code="invalid_quantity")

        material = materials.get(item["material_id"])
        if material is None:
            raise MaterialError(
                "That item isn't available at this branch.", code="material_not_found"
            )

        # The same material twice in one cart is one demand on the same stock:
        # the check is against everything this cart asks of it so far.
        asked = lines[material.pk]["quantity"] + quantity if material.pk in lines else quantity
        if material.quantity < asked:
            raise MaterialError(
                f"Only {material.quantity} of {material.name} in stock.",
                code="insufficient_stock",
                extra={"material": material.name, "available": material.quantity},
            )

        # The price the clinic actually charges, from the database.
        unit_price = material.selling_price
        total += unit_price * quantity

        lines[material.pk] = {"material": material, "quantity": asked, "unit_price": unit_price}

    resolved = list(lines.values())

    payment, _created = payment_services.record_payment(
        actor=actor,
        branch=branch,
        patient=patient,
        amount=total,
        method=method,
        category=PaymentCategory.MATERIAL_SALE,
        description=", ".join(f"{r['material'].name} × {r['quantity']}" for r in resolved),
        idempotency_key=idempotency_key,
    )

    # Stock, the sale lines and the stock movements, each written in one
    # statement however many items are in the cart.
    for row in resolved:
        row["material"].quantity -= row["quantity"]
    Material.objects.bulk_update([row["material"] for row in resolved], ["quantity"])

    sale_items = [
        MaterialSaleItem(
            payment=payment,
            material=row["material"],
            quantity=row["quantity"],
            unit_price=row["unit_price"],
        )
        for row in resolved
    ]
    MaterialSaleItem.objects.bulk_create(sale_items)

    MaterialMovement.objects.bulk_create(
        [
            MaterialMovement(
                material=row["material"],
                type=MaterialMovement.Type.OUT,
                quantity=row["quantity"],
                note=f"Sold — {payment.receipt_number}",
                branch=branch,
                created_by=actor,
            )
            for row in resolved
        ]
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=payment,
        branch=branch,
        reason="Material sale",
        changes={"total": str(total), "lines": len(resolved)},
    )
    return payment, sale_items, True


# ---------------------------------------------------------------------------
# Sell History
# ---------------------------------------------------------------------------


def sale_history_queryset(*, branch_id=None, params=None):
    """
    Past material sales, newest first, ready to serialize.

    Voided sales are left out. A manager who removes a mistaken sale from this
    list expects it gone from it; the money record itself is never destroyed
    and the voided payment stays visible on Transactions, which is the screen
    that exists to show everything that happened (docs/10).

    The joins are explicit because this table shows the buyer, the branch and
    every product line on each row -- without them the page would issue a
    query per row and get slower as the clinic's history grows.
    """
    from apps.payments.models import Payment, PaymentCategory, PaymentStatus

    params = params or {}

    queryset = (
        Payment.objects.filter(category=PaymentCategory.MATERIAL_SALE)
        .exclude(status=PaymentStatus.VOID)
        .select_related("patient", "branch", "collected_by")
        .prefetch_related("sale_items__material")
        .order_by("-created_at")
    )

    if branch_id:
        queryset = queryset.filter(branch_id=branch_id)

    queryset = _filter_sales_by_date(queryset, params)
    queryset = _filter_sales_by_search(queryset, params)
    return queryset


def _filter_sales_by_date(queryset, params):
    """
    One day, or a range. `date` wins when both are sent, the same precedence
    the reporting endpoints already use so the two screens can't disagree.
    """
    from datetime import date as date_cls

    def parse(value):
        try:
            return date_cls.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    exact = parse(params.get("date"))
    if exact:
        return queryset.filter(created_at__date=exact)

    date_from = parse(params.get("dateFrom"))
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)

    date_to = parse(params.get("dateTo"))
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)

    return queryset


def _filter_sales_by_search(queryset, params):
    """
    One box, searching what a person would actually type: the buyer's name or
    phone, a product on the sale, or the receipt number.

    Phone matching normalises the term first, so a number typed `+8801712...`
    still finds a row stored as `01712...` -- the same accommodation the
    patient search makes.
    """
    from django.db.models import Q

    from apps.common.validators import normalize_phone

    term = (params.get("search") or "").strip()
    if not term:
        return queryset

    lookup = (
        Q(patient__name__icontains=term)
        | Q(patient__phone__icontains=term)
        | Q(receipt_number__icontains=term)
        | Q(sale_items__material__name__icontains=term)
        | Q(sale_items__material__code__icontains=term)
    )

    normalized = normalize_phone(term)
    if normalized and normalized != term:
        lookup |= Q(patient__phone__icontains=normalized)

    # A sale with two matching product lines would otherwise come back twice.
    return queryset.filter(lookup).distinct()


@transaction.atomic
def update_sale_customer(*, actor, payment, name: str, phone: str):
    """
    Correct the name or phone a counter sale was billed to.

    Walk-ins only. A registered patient's name and phone are their own
    record's, edited from their profile where the change is visible for what
    it is -- not silently rewritten from a list of sales, which would also
    rename them on every other receipt they appear on.

    Nothing financial moves: the payment, its lines, the prices and the stock
    are all untouched.
    """
    patient = payment.patient

    if not patient.is_walk_in:
        raise MaterialError(
            "This sale was billed to a registered patient. "
            "Edit their details from their patient profile.",
            code="not_a_walk_in",
        )

    before = {"name": patient.name, "phone": patient.phone}
    patient.name = name
    patient.phone = phone
    patient.save(update_fields=["name", "phone", "updated_at"])

    changes = {
        field: {"from": before[field], "to": getattr(patient, field)}
        for field in ("name", "phone")
        if before[field] != getattr(patient, field)
    }
    if changes:
        audit.record(
            actor=actor,
            action=AuditLog.Action.UPDATE,
            target=patient,
            branch=payment.branch,
            reason=f"Buyer corrected on {payment.receipt_number}",
            changes=changes,
        )

    return payment
