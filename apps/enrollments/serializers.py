"""Enrollment, plan, bill and booking serializers."""

from rest_framework import serializers

from apps.enrollments.models import (
    Booking,
    Installment,
    InstallmentPlan,
    MonthlyBill,
    MonthlyEnrollment,
)
from apps.payments.models import PaymentMethod


class MonthlyBillSerializer(serializers.ModelSerializer):
    """
    `status` is the *effective* status — overdue is derived from the due date,
    never stored, so it can't go stale when a date passes with no write.
    """

    status = serializers.SerializerMethodField()
    amountPaid = serializers.DecimalField(
        source="amount_paid", max_digits=12, decimal_places=2, read_only=True
    )
    outstanding = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    dueDate = serializers.DateField(source="due_date", read_only=True)
    paidAt = serializers.DateTimeField(source="paid_at", read_only=True)

    class Meta:
        model = MonthlyBill
        fields = [
            "id", "month", "label", "amount", "amountPaid", "outstanding",
            "status", "dueDate", "paidAt",
        ]
        read_only_fields = fields

    def get_status(self, obj) -> str:
        return obj.effective_status()


class InstallmentSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    amountPaid = serializers.DecimalField(
        source="amount_paid", max_digits=12, decimal_places=2, read_only=True
    )
    outstanding = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    dueDate = serializers.DateField(source="due_date", read_only=True)
    paidAt = serializers.DateTimeField(source="paid_at", read_only=True)

    class Meta:
        model = Installment
        fields = [
            "id", "index", "label", "amount", "amountPaid", "outstanding",
            "status", "dueDate", "paidAt",
        ]
        read_only_fields = fields

    def get_status(self, obj) -> str:
        return obj.effective_status()


class MonthlyEnrollmentSerializer(serializers.ModelSerializer):
    bills = MonthlyBillSerializer(many=True, read_only=True)
    patientId = serializers.CharField(source="patient_id", read_only=True)
    patientName = serializers.CharField(source="patient.name", read_only=True)
    serviceId = serializers.CharField(source="service_id", read_only=True)
    serviceName = serializers.CharField(source="service.name", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = MonthlyEnrollment
        fields = [
            "id", "patientId", "patientName", "serviceId", "serviceName",
            "branchId", "status", "bills", "createdAt",
        ]
        read_only_fields = fields


class InstallmentPlanSerializer(serializers.ModelSerializer):
    installments = InstallmentSerializer(many=True, read_only=True)
    patientId = serializers.CharField(source="patient_id", read_only=True)
    patientName = serializers.CharField(source="patient.name", read_only=True)
    serviceId = serializers.CharField(source="service_id", read_only=True)
    serviceName = serializers.CharField(source="service.name", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    totalAmount = serializers.DecimalField(
        source="total_amount", max_digits=12, decimal_places=2, read_only=True
    )
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = InstallmentPlan
        fields = [
            "id", "patientId", "patientName", "serviceId", "serviceName",
            "branchId", "status", "totalAmount", "installments", "createdAt",
        ]
        read_only_fields = fields


class MonthlyEnrollmentCreateSerializer(serializers.Serializer):
    patient = serializers.UUIDField()
    service = serializers.UUIDField()


class InstallmentPlanCreateSerializer(serializers.Serializer):
    patient = serializers.UUIDField()
    service = serializers.UUIDField()
    numberOfInstallments = serializers.IntegerField(min_value=2, max_value=12)


class CollectPaymentSerializer(serializers.Serializer):
    """Which bill/installment, and how the money came in."""

    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    idempotencyKey = serializers.CharField(required=False, allow_blank=True, max_length=64)


class BookingSerializer(serializers.ModelSerializer):
    bookingCode = serializers.CharField(source="booking_code", read_only=True)
    patientId = serializers.CharField(source="patient_id", read_only=True)
    patientName = serializers.CharField(source="patient.name", read_only=True)
    serviceId = serializers.CharField(source="service_id", read_only=True)
    serviceName = serializers.CharField(source="service.name", read_only=True)
    advanceAmount = serializers.DecimalField(
        source="advance_amount", max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = Booking
        fields = [
            "id", "bookingCode", "patientId", "patientName", "serviceId", "serviceName",
            "date", "time", "advanceAmount", "status",
        ]
        read_only_fields = fields


class BookingCreateSerializer(serializers.Serializer):
    patient = serializers.UUIDField()
    service = serializers.UUIDField()
    date = serializers.DateField()
    time = serializers.CharField(max_length=16)
    advanceAmount = serializers.DecimalField(max_digits=12, decimal_places=2)
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    idempotencyKey = serializers.CharField(required=False, allow_blank=True, max_length=64)
