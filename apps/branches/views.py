"""
Branch endpoints.

Branch management is Admin-only. Managers can read their own branch (the
sidebar and receipts show its name) but never list or modify branches.
"""

from drf_spectacular.utils import extend_schema
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
from apps.reporting.services import patient_directory_summary, transactions_summary
from apps.common import audit
from apps.common.models import AuditLog
from apps.enrollments.models import EnrollmentStatus
from apps.staff.models import StaffMember


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
        # Managers may read branches (they need their own branch's name for the
        # sidebar and receipts), but the overview endpoints are Admin-only:
        # they exist for the Admin branches grid and expose cross-branch
        # revenue figures a manager has no business seeing.
        if self.action in {"list", "retrieve"}:
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
        Soft delete only — and only for a branch with nothing still running.

        A branch is the parent of every patient, payment and closing beneath
        it, so the row is never removed. But hiding a branch that still has
        active patient services or staff would leave billing and payroll
        running somewhere nobody can open. Deactivating is the answer for
        those, and the refusal says so.
        """
        branch = self.get_object()

        active_services = (
            branch.monthly_enrollments.filter(status=EnrollmentStatus.ACTIVE).count()
            + branch.installment_plans.filter(status=EnrollmentStatus.ACTIVE).count()
        )
        active_staff = branch.staff_members.filter(status=StaffMember.Status.ACTIVE).count()
        if active_services or active_staff:
            parts = []
            if active_services:
                parts.append(
                    f"{active_services} active patient service{'s' if active_services != 1 else ''}"
                )
            if active_staff:
                parts.append(
                    f"{active_staff} active staff member{'s' if active_staff != 1 else ''}"
                )
            return Response(
                {
                    "detail": (
                        f"This branch still has {' and '.join(parts)}. Deactivate the "
                        "branch instead — its history stays intact."
                    ),
                    "code": "branch_in_use",
                    "activeServiceCount": active_services,
                    "activeStaffCount": active_staff,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        branch.delete()  # SoftDeleteModel.delete
        audit.record(
            actor=request.user,
            action=AuditLog.Action.SOFT_DELETE,
            target=branch,
            changes={"code": branch.code},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """Mark the branch inactive. Its records and history are untouched."""
        return self._set_status(request, Branch.Status.INACTIVE, "Branch deactivated")

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        return self._set_status(request, Branch.Status.ACTIVE, "Branch reactivated")

    def _set_status(self, request, new_status, reason):
        branch = self.get_object()
        previous = branch.status
        if previous != new_status:
            branch.status = new_status
            branch.save(update_fields=["status", "updated_at"])
            audit.record(
                actor=request.user,
                action=AuditLog.Action.UPDATE,
                target=branch,
                reason=reason,
                changes={"status": {"from": previous, "to": new_status}},
            )
        return Response(BranchSerializer(branch).data)

    @extend_schema(operation_id="branches_overview_list")
    @action(detail=False, methods=["get"], url_path="overview")
    def overview(self, request):
        """
        GET /api/branches/overview/ — every branch with its headline figures.
        Admin only.

        Aggregates are placeholders until the payments and patients modules
        exist (Phases 2-3); the shape is settled now so the frontend's
        branches grid can bind against it.
        """
        branches = self.get_queryset()
        data = [self._build_overview(branch) for branch in branches]
        return Response(BranchOverviewSerializer(data, many=True).data)

    @extend_schema(operation_id="branches_overview_retrieve")
    @action(detail=True, methods=["get"], url_path="overview")
    def overview_detail(self, request, pk=None):
        """GET /api/branches/{id}/overview/ — one branch's figures."""
        branch = self.get_object()
        return Response(BranchOverviewSerializer(self._build_overview(branch)).data)

    def _build_overview(self, branch: Branch) -> dict:
        patients = patient_directory_summary(branch_id=branch.id)
        revenue = transactions_summary(branch_id=branch.id)
        return {
            "branch": branch,
            "patientCount": patients["total"],
            "totalCollected": revenue["totalCollected"],
            "monthlyRevenue": revenue["monthCollected"],
        }
