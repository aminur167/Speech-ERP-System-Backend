"""Staff HR serializers — camelCase on the wire, same convention as materials."""

from decimal import Decimal

from rest_framework import serializers

from apps.payments.models import PaymentMethod
from apps.staff.models import SalaryPayment, StaffAttendance, StaffBonus, StaffMember


class StaffMemberSerializer(serializers.ModelSerializer):
    staffCode = serializers.CharField(source="staff_code", read_only=True)
    joinedAt = serializers.DateField(source="joined_at")
    monthlySalary = serializers.DecimalField(
        source="monthly_salary", max_digits=12, decimal_places=2
    )
    branchId = serializers.CharField(source="branch_id", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = StaffMember
        fields = [
            "id", "staffCode", "name", "designation", "phone", "email",
            "joinedAt", "monthlySalary", "status", "branchId", "createdAt",
        ]
        read_only_fields = ["id", "staffCode", "branchId", "createdAt"]


class StaffMemberWriteSerializer(serializers.ModelSerializer):
    """Snake_case fields — the frontend runs its payload through `toSnakeCase` before posting, same as materials."""

    class Meta:
        model = StaffMember
        fields = [
            "name", "designation", "phone", "email", "joined_at",
            "monthly_salary", "status",
        ]
        extra_kwargs = {
            "email": {"required": False, "allow_blank": True},
            "status": {"required": False},
        }


class StaffAttendanceSerializer(serializers.ModelSerializer):
    staffId = serializers.CharField(source="staff_id", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    checkInAt = serializers.DateTimeField(source="check_in_at", read_only=True)
    checkOutAt = serializers.DateTimeField(source="check_out_at", read_only=True)

    class Meta:
        model = StaffAttendance
        fields = ["id", "staffId", "branchId", "date", "checkInAt", "checkOutAt", "status"]
        read_only_fields = fields


class MarkAttendanceSerializer(serializers.Serializer):
    """Manual status overrides only — "present"/"late" are always derived server-side from the check-in time."""

    status = serializers.ChoiceField(
        choices=[StaffAttendance.Status.ON_LEAVE, StaffAttendance.Status.ABSENT]
    )


class StaffBonusSerializer(serializers.ModelSerializer):
    staffId = serializers.CharField(source="staff_id", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    awardedBy = serializers.CharField(source="awarded_by.name", read_only=True, default="")
    awardedAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = StaffBonus
        fields = ["id", "staffId", "branchId", "amount", "reason", "awardedBy", "awardedAt"]
        read_only_fields = fields


class AddBonusSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    reason = serializers.CharField(max_length=255)


class StaffSummarySerializer(serializers.Serializer):
    totalStaff = serializers.IntegerField()
    presentToday = serializers.IntegerField()
    onLeaveToday = serializers.IntegerField()
    monthlySalaryPayout = serializers.DecimalField(max_digits=14, decimal_places=2)
    monthlyBonusPayout = serializers.DecimalField(max_digits=14, decimal_places=2)


class SalaryPaymentSerializer(serializers.ModelSerializer):
    staffId = serializers.CharField(source="staff_id", read_only=True)
    staffName = serializers.CharField(source="staff.name", read_only=True)
    staffCode = serializers.CharField(source="staff.staff_code", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True)
    requestedBy = serializers.CharField(source="requested_by.name", read_only=True, default="")
    reviewNote = serializers.CharField(source="review_note", read_only=True)
    reviewedBy = serializers.CharField(source="reviewed_by.name", read_only=True, default="")
    reviewedAt = serializers.DateTimeField(source="reviewed_at", read_only=True)
    paymentMethod = serializers.CharField(source="payment_method", read_only=True)
    paidAt = serializers.DateTimeField(source="paid_at", read_only=True)
    expenseId = serializers.CharField(source="expense_id", read_only=True, default=None)
    expenseCode = serializers.CharField(source="expense.expense_code", read_only=True, default="")
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = SalaryPayment
        fields = [
            "id", "staffId", "staffName", "staffCode", "branchId", "branchName",
            "month", "amount", "status", "requestedBy", "reviewNote", "reviewedBy",
            "reviewedAt", "paymentMethod", "paidAt", "expenseId", "expenseCode", "createdAt",
        ]
        read_only_fields = fields


class RequestSalaryPaymentSerializer(serializers.Serializer):
    """`month` is an ISO "YYYY-MM"; amount is never accepted from the client — it's computed server-side from the roster and that month's bonuses, same reasoning as materials pricing a sale from the database."""

    month = serializers.RegexField(r"^\d{4}-\d{2}$")


class ReviewSalaryPaymentSerializer(serializers.Serializer):
    approve = serializers.BooleanField()
    reviewNote = serializers.CharField(required=False, allow_blank=True)


class DisburseSalaryPaymentSerializer(serializers.Serializer):
    paymentMethod = serializers.ChoiceField(choices=PaymentMethod.choices)


class StaffMonthlyReportRowSerializer(serializers.Serializer):
    """One row of the monthly payroll + attendance report."""

    staffId = serializers.CharField()
    staffCode = serializers.CharField()
    name = serializers.CharField()
    designation = serializers.CharField()
    monthlySalary = serializers.DecimalField(max_digits=12, decimal_places=2)
    bonusTotal = serializers.DecimalField(max_digits=12, decimal_places=2)
    netPayable = serializers.DecimalField(max_digits=12, decimal_places=2)
    presentCount = serializers.IntegerField()
    lateCount = serializers.IntegerField()
    absentCount = serializers.IntegerField()
    leaveCount = serializers.IntegerField()
