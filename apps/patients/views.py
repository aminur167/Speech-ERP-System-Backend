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
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
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
from apps.enrollments import services as enrollment_services
from apps.enrollments.serializers import (
    InstallmentPlanSerializer,
    OutstandingDuesSerializer,
    MonthlyEnrollmentSerializer,
)
from apps.patients import attendance as attendance_services
from apps.patients import directory, services
from apps.patients.models import Patient, PatientAttendance
from apps.patients.serializers import (
    AttendanceRosterRowSerializer,
    MarkAttendanceSerializer,
    PatientAttendanceSerializer,
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
        # Registration and marking attendance are branch desk actions — the
        # frontend hides them from Admin, and that has to hold server-side too.
        if self.action in {"create", "mark_attendance"}:
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
        Every service the patient holds, active and inactive, newest first.

        A list rather than one-of-each: a patient can be in monthly therapy
        and paying off an installment package at the same time, and the
        profile screen has to show both.

        Inactive ones are included so the profile can offer Reactivate and
        still show what was kept or cancelled. `isActive` says which is which
        — the screen groups them, rather than this endpoint deciding for it.
        Making a service inactive is a decision about that service alone, so
        the patient's other services keep appearing here untouched.
        """
        patient = self.get_object()

        items = [
            {
                "type": "monthly",
                "id": str(enrollment.id),
                "serviceName": enrollment.service.name,
                "createdAt": enrollment.created_at,
                "isActive": enrollment.is_active,
                "enrollment": MonthlyEnrollmentSerializer(enrollment).data,
            }
            for enrollment in patient.monthly_enrollments.select_related(
                "service"
            ).prefetch_related("bills")
        ] + [
            {
                "type": "installment",
                "id": str(plan.id),
                "serviceName": plan.service.name,
                "createdAt": plan.created_at,
                "isActive": plan.is_active,
                "plan": InstallmentPlanSerializer(plan).data,
            }
            for plan in patient.installment_plans.select_related(
                "service"
            ).prefetch_related("installments")
        ]

        items.sort(key=lambda item: item["createdAt"], reverse=True)
        return Response(items)

    @extend_schema(responses=OutstandingDuesSerializer)
    @action(detail=True, methods=["get"], url_path="outstanding-dues")
    def outstanding_dues(self, request, pk=None):
        """
        Everything the patient still owes, across every service.

        The screen calls this before offering a new enrollment or a
        reactivation, so it can say which months and how much rather than
        waiting to be refused. The refusal itself lives in the service layer —
        this endpoint is the explanation, never the enforcement.
        """
        return Response(
            enrollment_services.patient_outstanding_dues(self.get_object())
        )

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

    # -- attendance --------------------------------------------------------

    @extend_schema(
        parameters=[
            OpenApiParameter("kind", str, description='"monthly" or "installment".'),
            OpenApiParameter("date", str, description="ISO date. Defaults to today."),
            OpenApiParameter("search", str),
            OpenApiParameter("unmarked", bool, description="Only patients not yet marked."),
            OpenApiParameter("alerts", bool, description="Only patients who have stopped coming."),
        ],
        responses=AttendanceRosterRowSerializer(many=True),
    )
    @action(detail=False, methods=["get"], url_path="attendance/roster")
    def attendance_roster(self, request):
        """
        The day's sheet for one kind of service.

        Monthly and installment are separate sheets because the manager takes
        them separately. A patient holding two *monthly* services appears once
        with both names on the row — they either came in or they didn't.

        Paginated, unlike the staff roster which deliberately loads whole: a
        branch has a handful of staff but can have hundreds of patients.
        """
        kind = request.query_params.get("kind") or PatientAttendance.ServiceKind.MONTHLY
        if kind not in PatientAttendance.ServiceKind.values:
            raise ValidationError({"kind": ['Expected "monthly" or "installment".']})

        on = _parse_date(request.query_params.get("date")) or timezone.localdate()
        branch_id = self._attendance_branch_id()

        roster_ids = attendance_services.roster_patient_ids(kind=kind, branch_id=branch_id)
        queryset = self.get_queryset().filter(id__in=roster_ids).order_by("name")

        search = request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(self._search_filter(search))

        # Assembled whole, then filtered, then paged — not paged first.
        # Both flags are derived rather than stored (one from the day's marks,
        # one from the gap clock), so filtering after a database page would
        # return a short page while matching patients sat on later pages, and
        # report a count for the unfiltered set. Same reasoning and same shape
        # as `collect_due_items`. Assembly is a fixed number of queries
        # whatever the roster size, so this costs rows in memory, not queries.
        rows = attendance_services.build_roster(
            patients=list(queryset), kind=kind, on=on, branch_id=branch_id
        )

        if request.query_params.get("unmarked") in {"1", "true", "True"}:
            rows = [row for row in rows if row["record"] is None]
        if request.query_params.get("alerts") in {"1", "true", "True"}:
            rows = [row for row in rows if row["alert"]]

        try:
            page_number = max(1, int(request.query_params.get("page", 1)))
            page_size = min(200, max(1, int(request.query_params.get("pageSize", 25))))
        except ValueError:
            page_number, page_size = 1, 25

        start = (page_number - 1) * page_size
        window = rows[start : start + page_size]

        return Response(
            {
                "count": len(rows),
                "next": str(page_number + 1) if start + page_size < len(rows) else None,
                "previous": str(page_number - 1) if page_number > 1 else None,
                "results": AttendanceRosterRowSerializer(window, many=True).data,
            }
        )

    @extend_schema(request=MarkAttendanceSerializer, responses=PatientAttendanceSerializer)
    @action(detail=True, methods=["post"], url_path="attendance")
    def mark_attendance(self, request, pk=None):
        """Mark one patient for one day — an upsert, so pressing twice is safe."""
        patient = self.get_object()
        serializer = MarkAttendanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        record = attendance_services.mark(
            actor=request.user,
            patient=patient,
            kind=data["serviceKind"],
            status=data["status"],
            on=data.get("date"),
            note=data.get("note", ""),
            expected_return_on=data.get("expectedReturnOn"),
        )
        return Response(PatientAttendanceSerializer(record).data)

    @extend_schema(
        parameters=[OpenApiParameter("days", int, description="How far back. Default 60.")],
        responses=PatientAttendanceSerializer(many=True),
    )
    @action(detail=True, methods=["get"], url_path="attendance-history")
    def attendance_history(self, request, pk=None):
        try:
            days = max(1, min(365, int(request.query_params.get("days", 60))))
        except ValueError:
            days = 60

        records = self.get_object().attendance_records.order_by("-date")[:days]
        return Response(PatientAttendanceSerializer(records, many=True).data)

    def _attendance_branch_id(self):
        """Manager: their own branch. Admin: optionally narrowed, else all."""
        user = self.request.user
        if user.is_manager:
            return user.branch_id
        return self.request.query_params.get("branch") or None


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
