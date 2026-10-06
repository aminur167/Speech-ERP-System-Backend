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

    material = Material.objects.select_for_update().get(pk=material.pk)

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
