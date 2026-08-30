"""Daily closing endpoints."""

from datetime import datetime

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsAdmin, IsManager
from apps.dailyclosing import services
from apps.dailyclosing.models import DailyClosing
from apps.dailyclosing.serializers import (
    AmendClosingSerializer,
    DailyClosingSerializer,
    SubmitClosingSerializer,
)


class DailyClosingViewSet(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    """
    /api/daily-closing/

    PUT, PATCH and DELETE are absent by design — a submitted closing is
    append-only. Corrections go through /amend/, which preserves the original
    figure. Admin cannot edit in place either.
    """

    queryset = DailyClosing.objects.select_related("branch", "submitted_by").prefetch_related(
        "amendments__amended_by"
    )
    serializer_class = DailyClosingSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        if self.action == "create":
            # Submitting the day's count is a branch-desk action; admin
            # shouldn't sign off cash they didn't handle.
            return [IsManager()]
        if self.action == "amend":
            return [IsAdmin()]
        return [IsAuthenticated()]

    def create(self, request, *args, **kwargs):
        serializer = SubmitClosingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        branch = Branch.objects.get(pk=request.user.branch_id)

        try:
            closing = services.submit_closing(
                actor=request.user,
                branch=branch,
                actual_total=serializer.validated_data["actualTotal"],
                day=serializer.validated_data.get("date"),
            )
        except services.ClosingError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(DailyClosingSerializer(closing).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="today-summary")
    def today_summary(self, request):
        """
        The day's expected collection, plus the refunds and voids that explain
        a short drawer.
        """
        branch_id = (
            request.user.branch_id
            if request.user.is_manager
            else request.query_params.get("branch")
        )
        if not branch_id:
            return Response(
                {"detail": "Admin must specify a branch."}, status=status.HTTP_400_BAD_REQUEST
            )

        branch = Branch.objects.get(pk=branch_id)
        day = _parse_date(request.query_params.get("date"))

        return Response(services.day_summary(branch=branch, day=day))

    @action(detail=False, methods=["get"])
    def history(self, request):
        return Response(DailyClosingSerializer(self.get_queryset()[:90], many=True).data)

    @action(detail=True, methods=["post"])
    def amend(self, request, pk=None):
        closing = self.get_object()
        serializer = AmendClosingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            closing = services.amend_closing(
                actor=request.user,
                closing=closing,
                corrected_actual_total=serializer.validated_data["correctedActualTotal"],
                reason=serializer.validated_data["reason"],
            )
        except services.ClosingError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
            )

        closing.refresh_from_db()
        return Response(DailyClosingSerializer(closing).data)


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
