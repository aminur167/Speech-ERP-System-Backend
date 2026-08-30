"""
Materials inventory and the POS sale trail.

**Branch-scoped, unlike the service catalog** — physical stock doesn't move
between locations, so each branch owns its own items and quantities.

`MaterialSaleItem` records what was actually sold on each payment, at the
price charged at the time. That record is what makes refunds trustworthy: a
refund is priced from the stored line, never from the request body and never
from the material's current price.
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.common.models import SoftDeleteModel, TimeStampedModel


class Material(TimeStampedModel, SoftDeleteModel):
    class Unit(models.TextChoices):
        PIECE = "piece", "Piece"
        BOX = "box", "Box"
        PACKET = "packet", "Packet"
        SET = "set", "Set"
        BOTTLE = "bottle", "Bottle"
        OTHER = "other", "Other"

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=32, unique=True, db_index=True)

    # Cloudinary delivery URL plus its public_id. The id is stored rather than
    # parsed back out of the URL because without it there's no reliable way to
    # replace or delete the asset later, and the account fills with orphans.
    image_url = models.URLField(blank=True)
    image_public_id = models.CharField(max_length=255, blank=True)

    unit = models.CharField(max_length=16, choices=Unit.choices, default=Unit.PIECE)
    quantity = models.IntegerField(default=0)

    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    # Kept separate from unit_cost so the markup is visible and reportable.
    selling_price = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    reorder_level = models.PositiveIntegerField(default=0)

    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="materials"
    )

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["branch", "name"]),
            models.Index(fields=["branch", "quantity"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def is_low_stock(self) -> bool:
        return self.quantity <= self.reorder_level

    @property
    def stock_value(self) -> Decimal:
        return self.unit_cost * self.quantity


class MaterialMovement(TimeStampedModel):
    """
    Append-only stock ledger.

    Every quantity change writes one of these — receipts, sales, adjustments,
    refund returns. Without it, a stock discrepancy has no history to explain
    it and staff are left guessing.
    """

    class Type(models.TextChoices):
        IN = "in", "Stock in"
        OUT = "out", "Stock out"

    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name="movements"
    )
    type = models.CharField(max_length=8, choices=Type.choices, db_index=True)
    quantity = models.PositiveIntegerField()
    note = models.CharField(max_length=255, blank=True)

    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="material_movements"
    )
    created_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL,
        related_name="material_movements",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["material", "-created_at"])]

    def __str__(self):
        return f"{self.type} {self.quantity} × {self.material_id}"


class MaterialSaleItem(models.Model):
    """
    One line of a POS sale.

    `unit_price` is the price charged at the time, copied here rather than
    referenced live — a later price change must not alter what a past sale or
    its refund was worth.
    """

    payment = models.ForeignKey(
        "payments.Payment", on_delete=models.PROTECT, related_name="sale_items"
    )
    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name="sale_items"
    )
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        indexes = [models.Index(fields=["payment"])]

    def __str__(self):
        return f"{self.quantity} × {self.material_id} @ {self.unit_price}"

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity
