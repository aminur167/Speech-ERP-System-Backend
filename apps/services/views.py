"""
Service catalog endpoints.

Branch-scoped (apps/services/models.py). A Manager only ever sees and
creates for their own branch. Admin sees every branch and may narrow with
`?branch=<id>`; edit/delete/(de)activate/review stay Admin-only, enforced
here and not by the frontend's `canManage` flag alone.
"""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common import audit
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.models import AuditLog
from apps.common.permissions import IsAdmin
from apps.notifications.inapp import notify_admins, notify_many
from apps.services import services
from apps.services.models import Service
from apps.services.serializers import (
    ServiceReviewSerializer,
    ServiceSerializer,
    ServiceWriteSerializer,
)


class ServiceViewSet(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    """
    /api/services/ — branch-scoped, like Materials.

    A Manager only ever sees and creates for their own branch. Admin sees
    every branch by default and may narrow with `?branch=<id>`; creating as
    Admin requires `branch` in the payload -- there's no "current branch" to
    fall back to (BranchScopedQuerySetMixin.get_effective_branch_id).
    """

    queryset = Service.objects.select_related("branch").all()
    serializer_class = ServiceSerializer
    filterset_fields = ["category", "is_online"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "fee", "created_at"]

    def get_permissions(self):
        if self.action in {"list", "retrieve", "enrollment_counts"}:
            return [IsAuthenticated()]
        # Anyone authenticated may propose a new package (create()) -- a
        # Manager's proposal lands as `pending`, never live, until Admin
        # reviews it via `review`. Every other write (edit, delete,
        # (de)activate) stays Admin-only, unchanged.
        if self.action == "create":
            return [IsAuthenticated()]
        return [IsAdmin()]

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return ServiceWriteSerializer
        return ServiceSerializer

    def get_queryset(self):
        """
        Inactive services are hidden from the listing by default.

        The enrollment wizards call GET /services/ to populate their pickers,
        so a retired package must not appear there. The Admin catalog opts
        back in with ?includeInactive=true — and only Admin, since a manager
        has no reason to see retired packages.

        This filter applies to `list` only. A deactivated service must stay
        directly reachable by id -- retrieve, update, destroy, and above all
        `activate` all call get_object(), which builds on this same queryset;
        filtering it everywhere would make reactivating a service permanently
        impossible; the moment `deactivate` succeeded, the object would drop
        out of its own lookup queryset and `activate` could never find it
        again. docs/03 is explicit that a deactivated service "is still
        visible in the Admin catalog... so it can be reactivated."
        """
        # BranchScopedQuerySetMixin.get_queryset() first: a Manager only ever
        # sees their own branch; Admin sees every branch, optionally narrowed
        # with ?branch=<id>. Everything below layers on top of that.
        queryset = super().get_queryset()

        if self.action != "list":
            return queryset

        include_inactive = (
            self.request.query_params.get("includeInactive", "").lower() == "true"
            and self.request.user.is_admin
        )
        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        # A pending or rejected proposal must never appear as a selectable
        # option in an enrollment wizard -- those call this same endpoint
        # without ?includePending, so they get approved-only by default
        # (true for both roles: even the Manager who proposed a package can't
        # enroll a patient in it before Admin approves it). The catalog pages
        # (Admin's and the Manager's) pass includePending=true to also show
        # what's awaiting a decision: Admin sees every pending/rejected
        # proposal (their queue to review); a Manager sees only their own,
        # so they can track it without seeing other branches' proposals.
        include_pending = (
            self.request.query_params.get("includePending", "").lower() == "true"
        )
        if not include_pending:
            queryset = queryset.filter(review_status=Service.ReviewStatus.APPROVED)
        elif not self.request.user.is_admin:
            queryset = queryset.filter(
                Q(review_status=Service.ReviewStatus.APPROVED)
                | Q(proposed_by=self.request.user)
            )

        return queryset

    def create(self, request, *args, **kwargs):
        # Resolved before validation, not after, so validate_code can check
        # the (branch, code) uniqueness constraint against the right branch.
        if request.user.is_admin:
            branch_id = self.get_effective_branch_id()
            branch = get_object_or_404(Branch, pk=branch_id)
        else:
            branch = request.user.branch

        serializer = ServiceWriteSerializer(data=request.data, context={"branch": branch})
        serializer.is_valid(raise_exception=True)

        # A Manager's package is filed under their own branch and lands as a
        # proposal, not a live catalog entry -- it never reaches enrollment
        # pickers (get_queryset above) until Admin reviews it. Admin creates
        # directly for a branch they name explicitly (there's no "their own
        # branch" to default to) and it's live immediately, no self-approval.
        if request.user.is_admin:
            service = serializer.save(branch=branch)
        else:
            service = serializer.save(
                branch=branch, review_status=Service.ReviewStatus.PENDING, proposed_by=request.user
            )
            branch_name = branch.name if branch else "A branch"
            notify_admins(
                title="Package needs approval",
                message=f'{branch_name} proposed "{service.name}" ({service.code}) for review.',
                link="/admin/services",
                exclude=request.user,
            )

        audit.record(
            actor=request.user,
            action=AuditLog.Action.CREATE,
            target=service,
            reason="Proposed for admin review" if not request.user.is_admin else "",
            changes={"code": service.code, "name": service.name, "fee": str(service.fee)},
        )
        return Response(ServiceSerializer(service).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        service = self.get_object()
        before = {"name": service.name, "fee": str(service.fee), "code": service.code}

        serializer = ServiceWriteSerializer(
            instance=service,
            data=request.data,
            partial=kwargs.pop("partial", False),
            context={"branch": service.branch},
        )
        serializer.is_valid(raise_exception=True)
        service = serializer.save()

        after = {"name": service.name, "fee": str(service.fee), "code": service.code}
        changes = audit.diff(before, after)
        if changes:
            audit.record(
                actor=request.user,
                action=AuditLog.Action.UPDATE,
                target=service,
                changes=changes,
            )
            self._notify_branch_of_edit(actor=request.user, service=service, changes=changes)
        return Response(ServiceSerializer(service).data)

    @staticmethod
    def _notify_branch_of_edit(*, actor, service, changes: dict) -> None:
        """
        Tell the branch its package was edited.

        Only Admin can reach update(), so this is always "someone above you
        changed your branch's package" -- the manager otherwise finds out by
        noticing a different price on the enrollment screen. Skips the actor
        so nobody is notified about their own edit, and says what moved
        rather than just that something did.
        """
        recipients = [
            manager
            for manager in service.branch.managers.filter(is_active=True)
            if manager.pk != actor.pk
        ]
        if not recipients:
            return

        summary = ", ".join(
            f"{field.replace('_', ' ')} {value['from']} → {value['to']}"
            for field, value in changes.items()
        )
        notify_many(
            recipients=recipients,
            title="Package updated by Admin",
            message=f'"{service.name}" was changed — {summary}.',
            link="/manager/packages",
        )

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, partial=True, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        Soft delete, refused while patients are enrolled.

        "Cannot delete" on its own leaves the Admin stuck, so the error carries
        the affected count and points at deactivation — which the UI can turn
        into a "Deactivate instead" button.
        """
        service = self.get_object()
        enrolled = service.active_enrollment_count()

        if enrolled > 0:
            return Response(
                {
                    "detail": (
                        f"{enrolled} patient{'s are' if enrolled != 1 else ' is'} currently "
                        f"enrolled in this package. Deactivate it instead to stop new "
                        f"enrollments while existing patients keep their plans."
                    ),
                    "activeEnrollmentCount": enrolled,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        service.delete()  # soft
        audit.record(
            actor=request.user,
            action=AuditLog.Action.SOFT_DELETE,
            target=service,
            changes={"code": service.code},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """Admin approves or rejects a Manager's proposed package."""
        service = self.get_object()
        serializer = ServiceReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            service = services.review_service(
                actor=request.user,
                service=service,
                approve=serializer.validated_data["approve"],
                review_note=serializer.validated_data.get("reviewNote", ""),
            )
        except services.ServiceError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(ServiceSerializer(service).data)

    @action(detail=False, methods=["get"], url_path="pending-count")
    def pending_count(self, request):
        """Admin-only — powers the sidebar's Services badge, independent of whatever page is open."""
        count = Service.objects.filter(review_status=Service.ReviewStatus.PENDING).count()
        return Response({"count": count})

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """
        Retire from sale. Always allowed, even with patients enrolled — their
        plans keep billing, which is the entire point of the distinction.
        """
        service = self.get_object()
        service.is_active = False
        service.save(update_fields=["is_active"])

        audit.record(
            actor=request.user,
            action=AuditLog.Action.UPDATE,
            target=service,
            reason="Deactivated (retired from sale)",
            changes={"is_active": {"from": True, "to": False}},
        )
        return Response(ServiceSerializer(service).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        service = self.get_object()
        service.is_active = True
        service.save(update_fields=["is_active"])

        audit.record(
            actor=request.user,
            action=AuditLog.Action.UPDATE,
            target=service,
            reason="Reactivated",
            changes={"is_active": {"from": False, "to": True}},
        )
        return Response(ServiceSerializer(service).data)

    @action(detail=False, methods=["get"], url_path="enrollment-counts")
    def enrollment_counts(self, request):
        """
        GET /api/services/enrollment-counts/ -> { serviceId: count }

        Two bulk aggregates rather than a per-service loop. With a handful of
        packages the difference is invisible; the discipline is what keeps it
        invisible after a decade of enrollments.

        Services with no enrollments are omitted — the frontend renders nothing
        for an undefined value, so an explicit 0 would add noise.
        """
        counts: dict[str, int] = {}

        monthly = (
            Service.objects.filter(monthly_enrollments__status="active")
            .annotate(n=Count("monthly_enrollments"))
            .values_list("id", "n")
        )
        installment = (
            Service.objects.filter(installment_plans__status="active")
            .annotate(n=Count("installment_plans"))
            .values_list("id", "n")
        )

        for service_id, n in list(monthly) + list(installment):
            counts[str(service_id)] = counts.get(str(service_id), 0) + n

        return Response(counts)
