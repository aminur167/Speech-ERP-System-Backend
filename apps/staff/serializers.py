"""Staff HR serializers — camelCase on the wire, same convention as materials."""

from decimal import Decimal

from rest_framework import serializers

from apps.staff.models import StaffAttendance, StaffBonus, StaffMember


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
