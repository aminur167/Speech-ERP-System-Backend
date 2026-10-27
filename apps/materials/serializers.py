"""
Material serializers.

Note what the sale payload does **not** accept: a price. The client sends
material ids and quantities only; the server prices the sale from the database.
"""

from decimal import Decimal

from rest_framework import serializers

from apps.common.validators import validate_bd_phone
from apps.materials.models import Material, MaterialMovement
from apps.payments.models import PaymentMethod


class MaterialSerializer(serializers.ModelSerializer):
    imageUrl = serializers.URLField(source="image_url", read_only=True)
    unitCost = serializers.DecimalField(
        source="unit_cost", max_digits=12, decimal_places=2, read_only=True
    )
    sellingPrice = serializers.DecimalField(
        source="selling_price", max_digits=12, decimal_places=2, read_only=True
    )
    reorderLevel = serializers.IntegerField(source="reorder_level", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    isLowStock = serializers.BooleanField(source="is_low_stock", read_only=True)
    stockValue = serializers.DecimalField(
        source="stock_value", max_digits=14, decimal_places=2, read_only=True
    )
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Material
        fields = [
            "id", "name", "code", "imageUrl", "unit", "quantity",
            "unitCost", "sellingPrice", "reorderLevel",
            "branchId", "isLowStock", "stockValue", "createdAt",
        ]
        read_only_fields = fields


class MaterialWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Material
        fields = [
            "name", "unit", "quantity", "unit_cost", "selling_price",
            "reorder_level", "image_url", "image_public_id",
        ]
        extra_kwargs = {
            "image_url": {"required": False, "allow_blank": True},
            "image_public_id": {"required": False, "allow_blank": True},
            "quantity": {"required": False},
            "reorder_level": {"required": False},
        }

    def validate_image_url(self, value):
        """
        Only accept URLs from the clinic's own Cloudinary account.

        Without this the API would happily store any URL a client sends, and
        a material's image could be pointed at arbitrary external content.
        """
        if not value:
            return value

        from django.conf import settings

        cloud_name = getattr(settings, "CLOUDINARY_CLOUD_NAME", "") or ""
        if not cloud_name:
            # Not configured yet — accept, but only Cloudinary-shaped URLs, so
            # this can't become a permanent open door.
            if "res.cloudinary.com" not in value:
                raise serializers.ValidationError(
                    "Image must be hosted on Cloudinary."
                )
            return value

        expected = f"res.cloudinary.com/{cloud_name}/"
        if expected not in value:
            raise serializers.ValidationError(
                "Image must be hosted on this clinic's Cloudinary account."
            )
        return value


class MaterialMovementSerializer(serializers.ModelSerializer):
    createdBy = serializers.CharField(source="created_by.name", read_only=True, default="")
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = MaterialMovement
        fields = ["id", "type", "quantity", "note", "createdBy", "createdAt"]
        read_only_fields = fields


class AdjustStockSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=MaterialMovement.Type.choices)
    quantity = serializers.IntegerField(min_value=1)
    note = serializers.CharField(required=False, allow_blank=True)
    idempotencyKey = serializers.CharField(required=False, allow_blank=True, max_length=64)
    clientCreatedAt = serializers.DateTimeField(required=False, allow_null=True)


class SaleItemSerializer(serializers.Serializer):
    """Deliberately no price field — see the module docstring."""

    material = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class SellMaterialsSerializer(serializers.Serializer):
    patient = serializers.IntegerField()
    items = SaleItemSerializer(many=True)
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    # One discount off the whole cart; the server checks it against the total
    # it priced itself (see sell_materials).
    discount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0.00"),
        default=Decimal("0.00"),
    )
    discountReason = serializers.CharField(
        required=False, allow_blank=True, max_length=500, default=""
    )
    idempotencyKey = serializers.CharField(required=False, allow_blank=True, max_length=64)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("The cart is empty.")
        return value


class MaterialsSummarySerializer(serializers.Serializer):
    totalItems = serializers.IntegerField()
    totalStockValue = serializers.DecimalField(max_digits=14, decimal_places=2)
    lowStockCount = serializers.IntegerField()


class SaleHistoryItemSerializer(serializers.Serializer):
    """One product line of a past sale, priced as it was sold."""

    material = serializers.IntegerField(source="material_id")
    name = serializers.CharField(source="material.name")
    quantity = serializers.IntegerField()
    unitPrice = serializers.DecimalField(
        source="unit_price", max_digits=12, decimal_places=2
    )
    lineTotal = serializers.DecimalField(
        source="line_total", max_digits=12, decimal_places=2
    )


class SaleHistorySerializer(serializers.Serializer):
    """
    A row of Sell History.

    Built from the sale's Payment, with the buyer and the lines denormalised
    onto it — the table shows who bought what in one row, and asking the
    client to stitch three endpoints together to render it would be slower
    and easier to get wrong.
    """

    id = serializers.CharField(source="pk")
    receiptNumber = serializers.CharField(source="receipt_number")
    customerId = serializers.CharField(source="patient_id")
    customerName = serializers.CharField(source="patient.name")
    customerPhone = serializers.CharField(source="patient.phone")
    # Whether the buyer was a counter walk-in or a registered patient. The
    # row's Edit action is offered only for a walk-in: a registered patient's
    # name and phone belong to their own record, and quietly rewriting it
    # from a sales list is not something this screen should be able to do.
    isWalkIn = serializers.BooleanField(source="patient.is_walk_in")
    branchId = serializers.CharField(source="branch_id")
    branchName = serializers.CharField(source="branch.name")
    method = serializers.CharField()
    total = serializers.DecimalField(source="amount", max_digits=12, decimal_places=2)
    soldBy = serializers.CharField(source="collected_by.name", default="")
    soldAt = serializers.DateTimeField(source="created_at")
    items = SaleHistoryItemSerializer(source="sale_items", many=True)


class SaleCustomerSerializer(serializers.Serializer):
    """
    Correcting who a counter sale was billed to.

    Name and phone only — nothing here touches the money, the products or the
    stock, so a mistyped name at the counter can be fixed without going
    anywhere near a financial record (docs/04: a payment is never edited).
    """

    name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=20, validators=[validate_bd_phone])
