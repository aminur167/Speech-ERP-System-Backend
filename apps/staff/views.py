"""Staff HR endpoints: roster, attendance, bonuses, and the monthly report."""

from datetime import date
from decimal import Decimal

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsManager
from apps.staff import services
from apps.staff.models import StaffAttendance, StaffMember
from apps.staff.serializers import (
    AddBonusSerializer,
    MarkAttendanceSerializer,
    StaffAttendanceSerializer,
    StaffBonusSerializer,
    StaffMemberSerializer,
    StaffMemberWriteSerializer,
    StaffMonthlyReportRowSerializer,
    StaffSummarySerializer,
)


class StaffMemberViewSet(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    """
    /api/staff/

    Branch-scoped, like Material — a Manager sees and manages only their own
    branch's roster; Admin sees every branch and may narrow with `?branch=`.
    """

    queryset = StaffMember.objects.select_related("branch").all()
    serializer_class = StaffMemberSerializer
    search_fields = ["name", "staff_code", "phone", "email"]
    ordering_fields = ["name", "joined_at", "monthly_salary", "created_at"]
    filterset_fields = ["status", "designation"]

    def get_permissions(self):
        read_only_actions = {
            "list", "retrieve", "summary", "today_attendance",
            "attendance_history", "bonuses", "monthly_report",
        }
        if self.action in read_only_actions:
            return [IsAuthenticated()]
        return [IsManager()]

    def create(self, request, *args, **kwargs):
        serializer = StaffMemberWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        branch = Branch.objects.get(pk=request.user.branch_id)
        member = services.create_staff_member(
            actor=request.user, branch=branch, data=dict(serializer.validated_data)
        )
        return Response(StaffMemberSerializer(member).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        member = self.get_object()
        serializer = StaffMemberWriteSerializer(
            instance=member, data=request.data, partial=kwargs.pop("partial", False)
        )
        serializer.is_valid(raise_exception=True)
        member = serializer.save()
        return Response(StaffMemberSerializer(member).data)

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, partial=True, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Soft delete — attendance and bonus history must stay resolvable."""
        member = self.get_object()
        member.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        queryset = self.get_queryset()
        today = timezone.localdate()
        today_records = StaffAttendance.objects.filter(staff__in=queryset, date=today)

        active = list(queryset.filter(status=StaffMember.Status.ACTIVE))
        salary_payout = sum((member.monthly_salary for member in active), Decimal("0.00"))

        report_rows = services.monthly_report(active, year=today.year, month=today.month)
        bonus_payout = sum((row["bonusTotal"] for row in report_rows), Decimal("0.00"))

        return Response(
            StaffSummarySerializer(
                {
                    "totalStaff": len(active),
                    "presentToday": today_records.filter(
                        status__in=[StaffAttendance.Status.PRESENT, StaffAttendance.Status.LATE]
                    ).count(),
                    "onLeaveToday": today_records.filter(
                        status__in=[StaffAttendance.Status.ON_LEAVE, StaffAttendance.Status.ABSENT]
                    ).count(),
                    "monthlySalaryPayout": salary_payout,
                    "monthlyBonusPayout": bonus_payout,
                }
            ).data
        )

    @action(detail=False, methods=["get"], url_path="today-attendance")
    def today_attendance(self, request):
        """`{staffId: attendanceRecord}` for the requesting scope's roster — missing keys mean not yet marked today."""
        today = timezone.localdate()
        records = StaffAttendance.objects.filter(staff__in=self.get_queryset(), date=today)
        return Response(
            {str(record.staff_id): StaffAttendanceSerializer(record).data for record in records}
        )

    @action(detail=True, methods=["get"], url_path="attendance-history")
    def attendance_history(self, request, pk=None):
        member = self.get_object()
        records = member.attendance_records.all()[:31]
        return Response(StaffAttendanceSerializer(records, many=True).data)

    @action(detail=True, methods=["post"], url_path="check-in", permission_classes=[IsManager])
    def check_in(self, request, pk=None):
        member = self.get_object()
        record = services.check_in(staff=member)
        return Response(StaffAttendanceSerializer(record).data)

    @action(detail=True, methods=["post"], url_path="check-out", permission_classes=[IsManager])
    def check_out(self, request, pk=None):
        member = self.get_object()
        record = services.check_out(staff=member)
        return Response(StaffAttendanceSerializer(record).data)

    @action(
        detail=True, methods=["post"], url_path="mark-attendance", permission_classes=[IsManager]
    )
    def mark_attendance(self, request, pk=None):
        member = self.get_object()
        serializer = MarkAttendanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = services.mark_attendance(staff=member, status=serializer.validated_data["status"])
        return Response(StaffAttendanceSerializer(record).data)

    @action(detail=True, methods=["get"])
    def bonuses(self, request, pk=None):
        member = self.get_object()
        records = member.bonuses.select_related("awarded_by").all()
        return Response(StaffBonusSerializer(records, many=True).data)

    @action(detail=True, methods=["post"], url_path="award-bonus", permission_classes=[IsManager])
    def award_bonus(self, request, pk=None):
        member = self.get_object()
        serializer = AddBonusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bonus = services.add_bonus(
            actor=request.user,
            staff=member,
            amount=serializer.validated_data["amount"],
            reason=serializer.validated_data["reason"],
        )
        return Response(
            StaffBonusSerializer(bonus).data, status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=["get"], url_path="monthly-report")
    def monthly_report(self, request):
        """
        `?month=YYYY-MM` (defaults to the current month) — one row per staff
        member with salary, bonuses, and present/late/absent/leave counts for
        that month. Powers the frontend's CSV export.
        """
        month_param = request.query_params.get("month")
        if month_param:
            try:
                year, month = (int(part) for part in month_param.split("-", 1))
                date(year, month, 1)
            except (ValueError, TypeError):
                raise ValidationError({"month": ["Expected YYYY-MM."]}) from None
        else:
            today = timezone.localdate()
            year, month = today.year, today.month

        rows = services.monthly_report(self.get_queryset(), year=year, month=month)
        return Response(StaffMonthlyReportRowSerializer(rows, many=True).data)
