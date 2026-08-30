"""Expense endpoints."""

from datetime import datetime
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsAdmin, IsManager
from apps.expenses import services
from apps.expenses.models import Expense
from apps.expenses.serializers import (
    ExpenseReviewSerializer,
    ExpenseSerializer,
    ExpenseSummarySerializer,
    ExpenseWriteSerializer,
)

# Approved and pending are real spending; rejected is not the clinic's cost.
COUNTED_STATUSES = [Expense.Status.APPROVED, Expense.Status.PENDING]


class ExpenseViewSet(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    """/api/expenses/"""

    queryset = Expense.objects.select_related("branch", "submitted_by", "reviewed_by").all()
    serializer_class = ExpenseSerializer
    filterset_fields = ["status", "category"]
    search_fields = ["expense_code", "description", "paid_to"]
    ordering_fields = ["created_at", "amount"]

    def get_permissions(self):
        if self.action == "create":
            return [IsManager()]
        if self.action == "review":
            return [IsAdmin()]
        return [IsAuthenticated()]

    def get_queryset(self):
        queryset = super().get_queryset()

        # An exact date always wins over a relative period — the dashboard's
        # date picker sends both, and honouring the period would silently
        # ignore the user's explicit choice.
        date_param = self.request.query_params.get("date")
        period = self.request.query_params.get("period")

        if date_param:
            parsed = _parse_date(date_param)
            if parsed:
                queryset = queryset.filter(created_at__date=parsed)
        elif period == "today":
            queryset = queryset.filter(created_at__date=timezone.localdate())
        elif period == "month":
            today = timezone.localdate()
            queryset = queryset.filter(
                created_at__year=today.year, created_at__month=today.month
            )

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = ExpenseWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        branch = Branch.objects.get(pk=request.user.branch_id)
        expense = services.create_expense(
            actor=request.user, branch=branch, data=dict(serializer.validated_data)
        )
        return Response(ExpenseSerializer(expense).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        expense = self.get_object()
        expense.delete()  # soft
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """Admin approves or rejects — and may reverse an earlier decision."""
        expense = self.get_object()
        serializer = ExpenseReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            expense = services.review_expense(
                actor=request.user,
                expense=expense,
                approve=serializer.validated_data["approve"],
                review_note=serializer.validated_data.get("reviewNote", ""),
            )
        except services.ExpenseError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(ExpenseSerializer(expense).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Totals for the dashboard and reports.

        The status rule here must match Net Revenue in the reporting module —
        if the two diverge, the figures on two screens stop reconciling.
        """
        base = super().get_queryset()  # unfiltered by the date params above

        reference = _parse_date(request.query_params.get("date")) or timezone.localdate()
        counted = base.filter(status__in=COUNTED_STATUSES)

        total = counted.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        today_total = counted.filter(created_at__date=reference).aggregate(s=Sum("amount"))[
            "s"
        ] or Decimal("0.00")
        month_total = counted.filter(
            created_at__year=reference.year, created_at__month=reference.month
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

        pending = base.filter(status=Expense.Status.PENDING)
        pending_amount = pending.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

        return Response(
            ExpenseSummarySerializer(
                {
                    "total": total,
                    "todayTotal": today_total,
                    "monthTotal": month_total,
                    "pendingAmount": pending_amount,
                    "pendingCount": pending.count(),
                    "voucherCount": base.count(),
                }
            ).data
        )


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
