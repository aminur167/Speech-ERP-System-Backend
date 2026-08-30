"""
Branch endpoints.

Branch management is Admin-only. Managers can read their own branch (the
sidebar and receipts show its name) but never list or modify branches.
"""

from decimal import Decimal

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches import services
from apps.branches.models import Branch
from apps.branches.serializers import (
    BranchOverviewSerializer,
    BranchSerializer,
    BranchWriteSerializer,
)
from apps.common.permissions import IsAdmin


class BranchViewSet(viewsets.ModelViewSet):
    """
    /api/branches/

    Not using BranchScopedQuerySetMixin: this is the branch registry itself,
    so the scoping rule is different — a manager sees exactly their own branch
    row, an admin sees all of them.
    """

    serializer_class = BranchSerializer
    queryset = Branch.objects.select_related("manager").all()
    filterset_fields = ["status"]
    search_fields = ["name", "code", "address"]
    ordering_fields = ["name", "opened_at", "created_at"]

    def get_permissions(self):
        # Reads are open to any authenticated user; writes are Admin-only.
        if self.action in {"list", "retrieve", "overview", "overview_detail"}:
            return [IsAuthenticated()]
        return [IsAdmin()]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_manager:
            if user.branch_id is None:
                return queryset.none()
            return queryset.filter(pk=user.branch_id)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = BranchWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        branch = services.create_branch(actor=request.user, data=dict(serializer.validated_data))
        return Response(BranchSerializer(branch).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        branch = self.get_object()
        serializer = BranchWriteSerializer(
            data=request.data, instance=branch, partial=kwargs.pop("partial", False)
        )
        serializer.is_valid(raise_exception=True)
        branch = services.update_branch(
            actor=request.user, branch=branch, data=dict(serializer.validated_data)
        )
        return Response(BranchSerializer(branch).data)

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, partial=True, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        Soft delete only.

        A branch is the parent of every patient, payment, and closing beneath
        it; removing the row would orphan or destroy all of that history.
        """
        branch = self.get_object()
        branch.delete()  # SoftDeleteModel.delete
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"], url_path="overview")
    def overview(self, request):
        """
        GET /api/branches/overview/ — every branch with its headline figures.

        Aggregates are placeholders until the payments and patients modules
        exist (Phases 2-3); the shape is settled now so the frontend's
        branches grid can bind against it.
        """
        branches = self.get_queryset()
        data = [self._build_overview(branch) for branch in branches]
        return Response(BranchOverviewSerializer(data, many=True).data)

    @action(detail=True, methods=["get"], url_path="overview")
    def overview_detail(self, request, pk=None):
        """GET /api/branches/{id}/overview/ — one branch's figures."""
        branch = self.get_object()
        return Response(BranchOverviewSerializer(self._build_overview(branch)).data)

    def _build_overview(self, branch: Branch) -> dict:
        # TODO(Phase 2-3): replace with real counts/sums once Patient and
        # Payment exist. Deliberately zero rather than fabricated, so a wrong
        # number never reaches a screen.
        return {
            "branch": branch,
            "patientCount": 0,
            "totalCollected": Decimal("0.00"),
            "monthlyRevenue": Decimal("0.00"),
        }
