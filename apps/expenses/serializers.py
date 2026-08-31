"""Expense serializers."""

from decimal import Decimal

from rest_framework import serializers

from apps.expenses.models import Expense
from apps.payments.models import PaymentMethod

MIN_AMOUNT = Decimal("0.01")


class ExpenseSerializer(serializers.ModelSerializer):
    expenseCode = serializers.CharField(source="expense_code", read_only=True)
    paidTo = serializers.CharField(source="paid_to", read_only=True)
    paymentMethod = serializers.CharField(source="payment_method", read_only=True)
    isRecurring = serializers.BooleanField(source="is_recurring", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True)
    submittedBy = serializers.CharField(source="submitted_by.name", read_only=True, default="")
    reviewNote = serializers.CharField(source="review_note", read_only=True)
    reviewedBy = serializers.CharField(source="reviewed_by.name", read_only=True, default="")
    reviewedAt = serializers.DateTimeField(source="reviewed_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Expense
        fields = [
            "id", "expenseCode", "category", "amount", "description",
            "paidTo", "paymentMethod", "remarks", "isRecurring",
            "branchId", "branchName", "submittedBy", "status",
            "reviewNote", "reviewedBy", "reviewedAt", "createdAt",
        ]
        read_only_fields = fields


class ExpenseWriteSerializer(serializers.ModelSerializer):
    payment_method = serializers.ChoiceField(choices=PaymentMethod.choices)
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=MIN_AMOUNT
    )
    # Offline-queue replay support (docs/00) -- see the matching fields on
    # PatientWriteSerializer for why these aren't in Meta.fields.
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=64)
    client_created_at = serializers.DateTimeField(required=False, allow_null=True)

    class Meta:
        model = Expense
        fields = [
            "category", "amount", "description", "paid_to",
            "payment_method", "remarks", "is_recurring",
            "idempotency_key", "client_created_at",
        ]
        extra_kwargs = {
            "remarks": {"required": False, "allow_blank": True},
            "is_recurring": {"required": False},
        }


class ExpenseReviewSerializer(serializers.Serializer):
    """
    Approve or reject.

    `reviewNote` is validated in the service layer rather than here, because
    whether it's required depends on the action and on whether a prior
    decision is being reversed.
    """

    approve = serializers.BooleanField()
    reviewNote = serializers.CharField(required=False, allow_blank=True)


class ExpenseSummarySerializer(serializers.Serializer):
    """
    Totals count approved + pending, excluding rejected.

    `pendingAmount` is returned separately so the UI can show an honest total
    with its unfinalised portion visible — "৳97,200 (৳20,000 awaiting
    approval)" rather than a single number hiding the uncertainty.
    """

    total = serializers.DecimalField(max_digits=14, decimal_places=2)
    todayTotal = serializers.DecimalField(max_digits=14, decimal_places=2)
    monthTotal = serializers.DecimalField(max_digits=14, decimal_places=2)
    pendingAmount = serializers.DecimalField(max_digits=14, decimal_places=2)
    pendingCount = serializers.IntegerField()
    voucherCount = serializers.IntegerField()
