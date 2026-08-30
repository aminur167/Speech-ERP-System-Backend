"""
Service catalog endpoints.

Read access is intentionally open to both roles — the enrollment wizards need
the catalog. Writes are Admin-only, enforced here and not by the frontend's
`canManage` flag alone.
"""

from django.db.models import Count, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import audit
from apps.common.models import AuditLog
from apps.common.permissions import IsAdmin
from apps.services.models import Service
from apps.services.serializers import ServiceSerializer, ServiceWriteSerializer


class ServiceViewSet(viewsets.ModelViewSet):
    """/api/services/ — organisation-wide, not branch-scoped."""

    queryset = Service.objects.all()
    serializer_class = ServiceSerializer
    filterset_fields = ["category", "is_online"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "fee", "created_at"]

    def get_permissions(self):
        if self.action in {"list", "retrieve", "enrollment_counts"}:
            return [IsAuthenticated()]
        return [IsAdmin()]

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return ServiceWriteSerializer
        return ServiceSerializer

    def get_queryset(self):
        """
        Inactive services are hidden by default.

        The enrollment wizards call this endpoint to populate their pickers, so
        a retired package must not appear there. The Admin catalog opts back in
        with ?includeInactive=true — and only Admin, since a manager has no
        reason to see retired packages.
        """
        queryset = super().get_queryset()

        include_inactive = (
            self.request.query_params.get("includeInactive", "").lower() == "true"
            and self.request.user.is_admin
        )
        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = ServiceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service = serializer.save()

        audit.record(
            actor=request.user,
            action=AuditLog.Action.CREATE,
            target=service,
            changes={"code": service.code, "name": service.name, "fee": str(service.fee)},
        )
        return Response(ServiceSerializer(service).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        service = self.get_object()
        before = {"name": service.name, "fee": str(service.fee), "code": service.code}

        serializer = ServiceWriteSerializer(
            instance=service, data=request.data, partial=kwargs.pop("partial", False)
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
        return Response(ServiceSerializer(service).data)

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
