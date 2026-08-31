"""
Patient endpoints.

Branch scoping comes from BranchScopedQuerySetMixin, which also covers the
detail route — so a manager guessing another branch's patient id gets a 404
rather than a leaked record.

The Patient Directory is the heavy read here: a denormalized listing joining
enrollments, plans and payments. Its assembly lives in `directory.py`, which
explains why it is built the way it is.
"""

from datetime import datetime

from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsManager
from apps.common.validators import normalize_phone
from apps.enrollments.models import EnrollmentStatus
from apps.enrollments.serializers import (
    InstallmentPlanSerializer,
    MonthlyEnrollmentSerializer,
)
from apps.patients import directory, services
from apps.patients.models import Patient
from apps.patients.serializers import (
    PatientListSerializer,
    PatientSerializer,
    PatientWriteSerializer,
)
from apps.reporting.services import patient_directory_summary


class PatientViewSet(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    """/api/patients/"""

    queryset = Patient.objects.select_related("branch", "created_by").all()
    filterset_fields = ["gender", "status"]
    ordering_fields = ["created_at", "name"]
    ordering = ["-created_at"]

    def get_permissions(self):
        # Registration is a branch desk action — the frontend hides it from
        # Admin, and that has to hold server-side too.
        if self.action == "create":
            return [IsManager()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.action == "list":
            return PatientListSerializer
        if self.action in {"create", "update", "partial_update"}:
            return PatientWriteSerializer
        return PatientSerializer

    def get_queryset(self):
        queryset = super().get_queryset()

        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(self._search_filter(search))

        return queryset

    @staticmethod
    def _search_filter(term: str) -> Q:
        """
        Name / phone / patient code / guardian name, case-insensitive.

        Two accommodations for how staff actually search: a phone number typed
        in any format matches (normalised first), and the numeric tail of a
        patient code matches on its own — nobody types `PT-DHK-2026-00001`
        when `00001` identifies the patient.
        """
        lookup = (
            Q(name__icontains=term)
            | Q(patient_code__icontains=term)
            | Q(guardian_name__icontains=term)
            | Q(phone__icontains=term)
        )

        normalized = normalize_phone(term)
        if normalized and normalized != term:
            lookup |= Q(phone__icontains=normalized) | Q(guardian_phone__icontains=normalized)

        return lookup

    @action(detail=False, methods=["get"])
    def directory(self, request):
        """
        GET /api/patients/directory/

        The page is sliced before the joins happen — denormalizing the whole
        table to render ten rows would defeat the point of paginating.
        """
        queryset = directory.filter_queryset(
            self.get_queryset(), request.query_params
        ).order_by("-created_at")

        paginator = PageNumberPagination()
        paginator.page_size = int(request.query_params.get("pageSize", 10) or 10)
        page = paginator.paginate_queryset(queryset, request)

        return paginator.get_paginated_response(directory.build_rows(list(page)))

    @action(detail=False, methods=["get"], url_path="directory/summary")
    def directory_summary(self, request):
        """
        Care-status counts for the dashboard cards.

        `date` scopes `intake` to that date's month rather than the current
        one — the dashboard's date picker needs a past month to report that
        month's intake, not today's.
        """
        branch_id = (
            request.user.branch_id
            if request.user.is_manager
            else request.query_params.get("branch") or None
        )
        return Response(
            patient_directory_summary(
                branch_id=branch_id,
                as_of=_parse_date(request.query_params.get("date")),
            )
        )

    @action(detail=True, methods=["get"], url_path="active-services")
    def active_services(self, request, pk=None):
        """
        Every active service the patient holds, newest first.

        A list rather than one-of-each: a patient can be in monthly therapy
        and paying off an installment package at the same time, and the
        profile screen has to show both.
        """
        patient = self.get_object()

        items = [
            {
                "type": "monthly",
                "id": str(enrollment.id),
                "serviceName": enrollment.service.name,
                "createdAt": enrollment.created_at,
                "enrollment": MonthlyEnrollmentSerializer(enrollment).data,
            }
            for enrollment in patient.monthly_enrollments.filter(
                status=EnrollmentStatus.ACTIVE
            ).select_related("service").prefetch_related("bills")
        ] + [
            {
                "type": "installment",
                "id": str(plan.id),
                "serviceName": plan.service.name,
                "createdAt": plan.created_at,
                "plan": InstallmentPlanSerializer(plan).data,
            }
            for plan in patient.installment_plans.filter(
                status=EnrollmentStatus.ACTIVE
            ).select_related("service").prefetch_related("installments")
        ]

        items.sort(key=lambda item: item["createdAt"], reverse=True)
        return Response(items)

    def create(self, request, *args, **kwargs):
        serializer = PatientWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        idempotency_key = data.pop("idempotency_key", None)
        client_created_at = data.pop("client_created_at", None)

        # Always the manager's own branch, whatever the body claims.
        branch = Branch.objects.get(pk=request.user.branch_id)

        patient = services.create_patient(
            actor=request.user, branch=branch, data=data,
            idempotency_key=idempotency_key, client_created_at=client_created_at,
        )
        return Response(PatientSerializer(patient).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        patient = self.get_object()
        serializer = PatientWriteSerializer(
            instance=patient, data=request.data, partial=kwargs.pop("partial", False)
        )
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        # idempotency_key/client_created_at describe the CREATE that's being
        # replayed, not an editable field of the patient -- an edit is never
        # itself offline-queued in a way that needs replay protection today.
        data.pop("idempotency_key", None)
        data.pop("client_created_at", None)

        patient = services.update_patient(actor=request.user, patient=patient, data=data)
        return Response(PatientSerializer(patient).data)

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, partial=True, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Soft delete — patient history is never removed."""
        patient = self.get_object()
        patient.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
