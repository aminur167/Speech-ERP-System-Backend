"""Daily closing serializers."""

from decimal import Decimal

from rest_framework import serializers

from apps.dailyclosing.models import DailyClosing, DailyClosingAmendment

MIN_AMOUNT = Decimal("0.00")


class AmendmentSerializer(serializers.ModelSerializer):
    previousActualTotal = serializers.DecimalField(
        source="previous_actual_total", max_digits=14, decimal_places=2, read_only=True
    )
    correctedActualTotal = serializers.DecimalField(
        source="corrected_actual_total", max_digits=14, decimal_places=2, read_only=True
    )
    amendedBy = serializers.CharField(source="amended_by.name", read_only=True, default="")
    amendedAt = serializers.DateTimeField(source="amended_at", read_only=True)

    class Meta:
        model = DailyClosingAmendment
        fields = [
            "id", "previousActualTotal", "correctedActualTotal",
            "reason", "amendedBy", "amendedAt",
        ]
        read_only_fields = fields


class DailyClosingSerializer(serializers.ModelSerializer):
    systemTotal = serializers.DecimalField(
        source="system_total", max_digits=14, decimal_places=2, read_only=True
    )
    actualTotal = serializers.DecimalField(
        source="actual_total", max_digits=14, decimal_places=2, read_only=True
    )
    submittedBy = serializers.CharField(source="submitted_by.name", read_only=True, default="")
    submittedAt = serializers.DateTimeField(source="submitted_at", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    isAmended = serializers.BooleanField(source="is_amended", read_only=True)
    amendments = AmendmentSerializer(many=True, read_only=True)

    class Meta:
        model = DailyClosing
        fields = [
            "id", "branchId", "date", "systemTotal", "actualTotal", "difference",
            "status", "submittedBy", "submittedAt", "isAmended", "amendments",
        ]
        read_only_fields = fields


class SubmitClosingSerializer(serializers.Serializer):
    """
    Only the counted cash is accepted.

    `systemTotal`, `difference` and `status` are all derived server-side —
    they're the reconciliation result, not client input.
    """

    actualTotal = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=MIN_AMOUNT
    )
    date = serializers.DateField(required=False)


class AmendClosingSerializer(serializers.Serializer):
    correctedActualTotal = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=MIN_AMOUNT
    )
    reason = serializers.CharField()

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "A reason is required when correcting a closing."
            )
        return value
