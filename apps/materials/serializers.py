"""
Material serializers.

Note what the sale payload does **not** accept: a price. The client sends
material ids and quantities only; the server prices the sale from the database.
"""

from rest_framework import serializers

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


class SaleItemSerializer(serializers.Serializer):
    """Deliberately no price field — see the module docstring."""

    material = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class SellMaterialsSerializer(serializers.Serializer):
    patient = serializers.UUIDField()
    items = SaleItemSerializer(many=True)
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    idempotencyKey = serializers.CharField(required=False, allow_blank=True, max_length=64)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("The cart is empty.")
        return value


class MaterialsSummarySerializer(serializers.Serializer):
    totalItems = serializers.IntegerField()
    totalStockValue = serializers.DecimalField(max_digits=14, decimal_places=2)
    lowStockCount = serializers.IntegerField()
