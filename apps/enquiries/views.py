"""Enquiry endpoints — /api/enquiries/."""

from django.db.models import prefetch_related_objects
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.mixins import BranchScopedQuerySetMixin, manager_branch
from apps.common.permissions import IsManager
from apps.enquiries import services
from apps.enquiries.models import Enquiry
from apps.enquiries.serializers import (
    ConvertEnquirySerializer,
    EnquirySerializer,
    EnquirySummarySerializer,
    EnquiryWriteSerializer,
)
from apps.patients.serializers import PatientSerializer, PatientWriteSerializer


def _error(exc: services.EnquiryError, http_status=status.HTTP_400_BAD_REQUEST):
    body = {"detail": exc.message, "code": exc.code}
    body.update(exc.extra)
    return Response(body, status=http_status)


class EnquiryViewSet(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    """
    Calls that came in off an ad.

    Branch-scoped by the shared mixin: the branch that took the call owns it,
    a Manager sees only their own, and Admin sees every branch and may narrow
    with `?branch=`.
    """

    queryset = Enquiry.objects.select_related(
        "branch", "taken_by", "converted_patient"
    ).all()
    permission_classes = [IsAuthenticated]
    ordering = ["-called_at"]

    def get_permissions(self):
        # Recording a call is a branch-desk action, like registering a
        # patient: Admin has no branch to file it under. Admin can still
        # read, edit and delete for oversight.
        if self.action in {"create", "convert"}:
            return [IsManager()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return EnquiryWriteSerializer
        return EnquirySerializer

    def get_queryset(self):
        return services.filter_enquiries(super().get_queryset(), self.request.query_params)

    @extend_schema(
        parameters=[
            OpenApiParameter("search", str, description="Caller or patient name, phone, concern, code, or source detail."),
            OpenApiParameter("status", str),
            OpenApiParameter("source", str),
            OpenApiParameter("followUp", str, description='"today", "overdue", "upcoming" or "none".'),
            OpenApiParameter("dateFrom", str),
            OpenApiParameter("dateTo", str),
            OpenApiParameter("branch", str, description="Admin only."),
        ],
        responses=EnquirySerializer(many=True),
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = EnquiryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        enquiry = services.create_enquiry(
            actor=request.user,
            branch=manager_branch(request),
            data=dict(serializer.validated_data),
        )
        return Response(
            EnquirySerializer(enquiry).data, status=status.HTTP_201_CREATED
        )

    def update(self, request, *args, **kwargs):
        enquiry = self.get_object()
        serializer = EnquiryWriteSerializer(
            instance=enquiry, data=request.data, partial=kwargs.pop("partial", False)
        )
        serializer.is_valid(raise_exception=True)

        enquiry = services.update_enquiry(
            actor=request.user, enquiry=enquiry, data=dict(serializer.validated_data)
        )
        return Response(EnquirySerializer(enquiry).data)

    @extend_schema(responses=EnquirySummarySerializer)
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        The page header's counts, over whatever the filters left — so the
        numbers always describe the list beneath them.
        """
        return Response(services.enquiry_summary(self.get_queryset()))

    @extend_schema(request=ConvertEnquirySerializer)
    @action(detail=True, methods=["post"])
    def convert(self, request, pk=None):
        """
        POST /api/enquiries/{id}/convert/ — register this caller as a patient.

        The registration goes through `PatientWriteSerializer`, so it obeys
        exactly the rules the registration form does — including the
        guardian requirement for minors. A caller is not a shortcut past
        them.
        """
        enquiry = self.get_object()

        patient_serializer = PatientWriteSerializer(data=request.data)
        patient_serializer.is_valid(raise_exception=True)

        try:
            enquiry, patient = services.convert_to_patient(
                actor=request.user,
                enquiry=enquiry,
                patient_data=dict(patient_serializer.validated_data),
            )
        except services.EnquiryError as exc:
            return _error(exc)

        prefetch_related_objects([enquiry], "converted_patient")
        return Response(
            {
                "enquiry": EnquirySerializer(enquiry).data,
                "patient": PatientSerializer(
                    patient, context={"just_registered": True}
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )
