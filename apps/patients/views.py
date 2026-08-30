"""
Patient endpoints.

Branch scoping comes from BranchScopedQuerySetMixin, which also covers the
detail route — so a manager guessing another branch's patient id gets a 404
rather than a leaked record.

The Patient Directory endpoint is specified in docs/02-patients.md but depends
on enrollments and payments, so it lands in Phase 5 (see ROADMAP). Building it
against models that don't exist yet would mean guessing at the joins.
"""

from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsManager
from apps.common.validators import normalize_phone
from apps.patients import services
from apps.patients.models import Patient
from apps.patients.serializers import (
    PatientListSerializer,
    PatientSerializer,
    PatientWriteSerializer,
)


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

    def create(self, request, *args, **kwargs):
        serializer = PatientWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Always the manager's own branch, whatever the body claims.
        branch = Branch.objects.get(pk=request.user.branch_id)

        patient = services.create_patient(
            actor=request.user, branch=branch, data=dict(serializer.validated_data)
        )
        return Response(PatientSerializer(patient).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        patient = self.get_object()
        serializer = PatientWriteSerializer(
            instance=patient, data=request.data, partial=kwargs.pop("partial", False)
        )
        serializer.is_valid(raise_exception=True)

        patient = services.update_patient(
            actor=request.user, patient=patient, data=dict(serializer.validated_data)
        )
        return Response(PatientSerializer(patient).data)

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, partial=True, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Soft delete — patient history is never removed."""
        patient = self.get_object()
        patient.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
