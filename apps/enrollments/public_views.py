"""
Public (unauthenticated) endpoints for the clinic's own website — a visitor
booking their own appointment, not a Manager acting on a branch's behalf.

Mounted separately from the rest of the API (`/api/public/...`, see
config/urls.py) precisely so this boundary — everything reachable with no
token at all — stays easy to audit in one place, rather than scattered
`AllowAny` overrides mixed in among branch-scoped staff endpoints.
"""

from datetime import datetime

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.branches.models import Branch
from apps.enrollments import services
from apps.enrollments.serializers import (
    PublicAvailabilitySlotSerializer,
    PublicBookingCreateSerializer,
    PublicBookingSerializer,
    PublicBranchSerializer,
    PublicServiceSerializer,
)
from apps.services.models import Service


class _PublicView(APIView):
    permission_classes = [AllowAny]


class PublicBranchListView(_PublicView):
    """GET /api/public/branches/ — the branch picker on the booking page."""

    @extend_schema(tags=["public"], responses=PublicBranchSerializer(many=True))
    def get(self, request):
        branches = Branch.objects.filter(status=Branch.Status.ACTIVE).order_by("name")
        return Response(
            PublicBranchSerializer(
                [{"id": b.id, "name": b.name, "address": b.address, "phone": b.phone} for b in branches],
                many=True,
            ).data
        )


class PublicServiceListView(_PublicView):
    """
    GET /api/public/services/?branch=<id> — the service picker, once a
    branch is chosen.

    Scoped to `category=ONLINE`: that's the category the fee/advance model
    here is built for (see `create_public_booking`), not every service the
    branch happens to run. `review_status=APPROVED` keeps a Manager's own
    still-pending proposal off a page the public can already see.
    """

    @extend_schema(
        tags=["public"],
        parameters=[OpenApiParameter("branch", int, required=True)],
        responses=PublicServiceSerializer(many=True),
    )
    def get(self, request):
        branch_id = request.query_params.get("branch")
        if not branch_id:
            return Response(
                {"detail": "branch is required.", "code": "branch_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        services_qs = Service.objects.filter(
            branch_id=branch_id,
            category=Service.Category.ONLINE,
            is_active=True,
            review_status=Service.ReviewStatus.APPROVED,
        ).order_by("name")
        return Response(PublicServiceSerializer(services_qs, many=True).data)


class PublicBookingAvailabilityView(_PublicView):
    """
    GET /api/public/bookings/availability/?branch=<id>&date=<YYYY-MM-DD>

    Every bookable time that day, each flagged open or already taken — what
    the picker reads before the visitor ever submits, so they don't fill out
    the whole form only to have `PublicBookingCreateView` reject the slot.
    """

    @extend_schema(
        tags=["public"],
        parameters=[
            OpenApiParameter("branch", int, required=True),
            OpenApiParameter("date", str, required=True, description="ISO date (YYYY-MM-DD)."),
        ],
        responses=PublicAvailabilitySlotSerializer(many=True),
    )
    def get(self, request):
        branch_id = request.query_params.get("branch")
        date_str = request.query_params.get("date")
        if not branch_id or not date_str:
            return Response(
                {"detail": "branch and date are required.", "code": "missing_params"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            branch = Branch.objects.get(pk=branch_id, status=Branch.Status.ACTIVE)
        except Branch.DoesNotExist:
            return Response(
                {"detail": "Branch not found.", "code": "not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"detail": "date must be an ISO date (YYYY-MM-DD).", "code": "invalid_date"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        slots = services.public_booking_availability(branch=branch, target_date=target_date)
        return Response(PublicAvailabilitySlotSerializer(slots, many=True).data)


class PublicBookingCreateView(_PublicView):
    """
    POST /api/public/bookings/ — the website's booking form submission.

    Unauthenticated and writes real data (a Patient, a Booking), so it's the
    one public endpoint worth rate-limiting against spam/abuse; the
    read-only views above carry no such risk.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_booking"

    @extend_schema(
        tags=["public"], request=PublicBookingCreateSerializer, responses=PublicBookingSerializer
    )
    def post(self, request):
        serializer = PublicBookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            branch = Branch.objects.get(pk=data["branch"], status=Branch.Status.ACTIVE)
            service = Service.objects.get(
                pk=data["service"],
                branch=branch,
                category=Service.Category.ONLINE,
                is_active=True,
                review_status=Service.ReviewStatus.APPROVED,
            )
        except (Branch.DoesNotExist, Service.DoesNotExist):
            return Response(
                {"detail": "Branch or service not found.", "code": "not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            booking, patient = services.create_public_booking(
                branch=branch,
                service=service,
                patient_data=data["patient"],
                booking_date=data["date"],
                booking_time=data["time"],
            )
        except services.EnrollmentError as exc:
            body = {"detail": exc.message, "code": exc.code}
            body.update(exc.extra)
            return Response(body, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            PublicBookingSerializer(
                {
                    "bookingCode": booking.booking_code,
                    "patientName": patient.name,
                    "patientCode": patient.patient_code,
                    "serviceName": service.name,
                    "branchName": branch.name,
                    "date": booking.date,
                    "time": booking.time,
                    "advanceAmount": booking.advance_amount,
                    "status": booking.status,
                }
            ).data,
            status=status.HTTP_201_CREATED,
        )
