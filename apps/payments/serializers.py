"""Payment and refund serializers."""

from decimal import Decimal

from rest_framework import serializers

from apps.payments.models import Payment, PaymentMethod, RefundRequest, RefundRequestItem

# Decimal, not 0.01 — a float bound on a money field is exactly the class of
# bug the Decimal rule exists to prevent, and DRF warns about it.
MIN_AMOUNT = Decimal("0.01")


class PaymentSerializer(serializers.ModelSerializer):
    """
    Read shape — carries everything Receipt.tsx renders.

    `branchName` resolves from the FK. A hardcoded "Main Branch" string was a
    real bug in the frontend; the fix is to always derive it from the branch
    where the transaction actually happened.
    """

    transactionId = serializers.CharField(source="transaction_id", read_only=True)
    receiptNumber = serializers.CharField(source="receipt_number", read_only=True)
    patientId = serializers.CharField(source="patient_id", read_only=True)
    patientName = serializers.CharField(source="patient.name", read_only=True)
    patientCode = serializers.CharField(source="patient.patient_code", read_only=True)
    collectedBy = serializers.CharField(source="collected_by.name", read_only=True, default="")
    branchId = serializers.CharField(source="branch_id", read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id", "transactionId", "receiptNumber",
            "patientId", "patientName", "patientCode",
            "amount", "method", "status", "category", "description",
            "collectedBy", "branchId", "branchName", "createdAt",
        ]
        read_only_fields = fields


class PaymentCreateSerializer(serializers.Serializer):
    """
    Direct payment creation (daily service, booking advance).

    `branch` and `collected_by` are absent by design — both come from the
    authenticated user, never the request body.
    """

    patient = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_AMOUNT)
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    category = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)

    # Client-generated per user action, so a retry or a replayed offline
    # mutation returns the original payment instead of charging twice.
    idempotencyKey = serializers.CharField(required=False, allow_blank=True, max_length=64)
    clientCreatedAt = serializers.DateTimeField(required=False, allow_null=True)


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField()

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("A reason is required to void a payment.")
        return value


class RefundItemSerializer(serializers.Serializer):
    material = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class RefundRequestCreateSerializer(serializers.Serializer):
    """
    Manager's refund request.

    `amount` is ignored when `items` are supplied — the total is recomputed
    from the original sale's stored prices so a tampered payload can't refund
    more than was charged.
    """

    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=MIN_AMOUNT, required=False
    )
    reason = serializers.CharField()
    items = RefundItemSerializer(many=True, required=False)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("A reason is required.")
        return value

    def validate(self, attrs):
        if not attrs.get("items") and attrs.get("amount") is None:
            raise serializers.ValidationError(
                {"amount": ["Provide an amount, or the items being returned."]}
            )
        return attrs


class RefundRequestItemSerializer(serializers.ModelSerializer):
    materialName = serializers.CharField(source="material.name", read_only=True)
    unitPrice = serializers.DecimalField(
        source="unit_price", max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = RefundRequestItem
        fields = ["id", "material", "materialName", "quantity", "unitPrice"]
        read_only_fields = fields


class RefundRequestSerializer(serializers.ModelSerializer):
    payment = PaymentSerializer(read_only=True)
    items = RefundRequestItemSerializer(many=True, read_only=True)
    requestedBy = serializers.CharField(source="requested_by.name", read_only=True, default="")
    requestedAt = serializers.DateTimeField(source="requested_at", read_only=True)
    reviewedBy = serializers.CharField(source="reviewed_by.name", read_only=True, default="")
    reviewedAt = serializers.DateTimeField(source="reviewed_at", read_only=True)
    reviewNote = serializers.CharField(source="review_note", read_only=True)
    billAction = serializers.CharField(source="bill_action", read_only=True)
    refundMethod = serializers.CharField(source="refund_method", read_only=True)

    class Meta:
        model = RefundRequest
        fields = [
            "id", "payment", "amount", "reason", "status", "items",
            "requestedBy", "requestedAt", "reviewedBy", "reviewedAt", "reviewNote",
            "billAction", "refundMethod",
        ]
        read_only_fields = fields


class RefundApproveSerializer(serializers.Serializer):
    billAction = serializers.ChoiceField(
        choices=RefundRequest.BillAction.choices,
        default=RefundRequest.BillAction.REOPEN,
    )
    refundMethod = serializers.ChoiceField(
        choices=PaymentMethod.choices, required=False, allow_blank=True
    )
    reviewNote = serializers.CharField(required=False, allow_blank=True)


class RefundRejectSerializer(serializers.Serializer):
    reviewNote = serializers.CharField()

    def validate_reviewNote(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "A reason is required when rejecting a refund request."
            )
        return value
