"""
Enrollment endpoints.

Enrolling and collecting are branch-desk actions, so they're Manager-only:
Admin can see everything but shouldn't transact on a branch's behalf.
"""

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsManager
from apps.enrollments import services
from apps.enrollments.models import (
    Booking,
    Installment,
    InstallmentPlan,
    MonthlyBill,
    MonthlyEnrollment,
)
from apps.enrollments.serializers import (
    BookingCreateSerializer,
    BookingSerializer,
    CollectPaymentSerializer,
    InstallmentPlanCreateSerializer,
    InstallmentPlanSerializer,
    MonthlyEnrollmentCreateSerializer,
    MonthlyEnrollmentSerializer,
)
from apps.patients.models import Patient
from apps.payments.serializers import PaymentSerializer
from apps.services.models import Service


def _error(exc: services.EnrollmentError, http_status=status.HTTP_400_BAD_REQUEST):
    body = {"detail": exc.message, "code": exc.code}
    body.update(exc.extra)
    return Response(body, status=http_status)


class _EnrollmentBase(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        if self.action in {"list", "retrieve"}:
            return [IsAuthenticated()]
        return [IsManager()]

    def _resolve(self, request, model, field):
        """Look up a related object within the acting branch, or 404."""
        branch_id = request.user.branch_id
        try:
            return model.objects.get(pk=request.data[field]), branch_id
        except (model.DoesNotExist, KeyError):
            return None, branch_id


class MonthlyEnrollmentViewSet(_EnrollmentBase):
    """/api/enrollments/monthly/"""

    queryset = MonthlyEnrollment.objects.select_related(
        "patient", "service", "branch"
    ).prefetch_related("bills")
    serializer_class = MonthlyEnrollmentSerializer
    filterset_fields = ["status", "patient"]

    def create(self, request, *args, **kwargs):
        serializer = MonthlyEnrollmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch = Branch.objects.get(pk=request.user.branch_id)

        try:
            patient = Patient.objects.get(pk=data["patient"], branch=branch)
        except Patient.DoesNotExist:
            return Response(
                {"detail": "Patient not found in this branch."}, status=status.HTTP_404_NOT_FOUND
            )

        try:
            # Inactive services are excluded — a retired package must not take
            # new enrollments, though existing ones keep billing.
            service = Service.objects.get(pk=data["service"], is_active=True)
        except Service.DoesNotExist:
            return Response(
                {"detail": "Service not found or no longer available."},
                status=status.HTTP_404_NOT_FOUND,
            )

        enrollment = services.create_monthly_enrollment(
            actor=request.user, branch=branch, patient=patient, service=service
        )
        return Response(
            MonthlyEnrollmentSerializer(enrollment).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="bills/(?P<bill_id>[^/.]+)/pay")
    def pay_bill(self, request, pk=None, bill_id=None):
        """
        Collect a bill. One call, one transaction — the mock's two-step
        "create payment then mark paid" could take money without settling.
        """
        enrollment = self.get_object()
        serializer = CollectPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            bill = MonthlyBill.objects.get(pk=bill_id, enrollment=enrollment)
        except MonthlyBill.DoesNotExist:
            return Response({"detail": "Bill not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            payment, bill = services.collect_bill_payment(
                actor=request.user,
                branch=enrollment.branch,
                bill=bill,
                method=serializer.validated_data["method"],
                idempotency_key=serializer.validated_data.get("idempotencyKey") or None,
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(
            {
                "payment": PaymentSerializer(payment).data,
                "enrollment": MonthlyEnrollmentSerializer(
                    MonthlyEnrollment.objects.prefetch_related("bills").get(pk=enrollment.pk)
                ).data,
            }
        )

    @action(detail=True, methods=["post"])
    def terminate(self, request, pk=None):
        enrollment = self.get_object()
        try:
            services.terminate(actor=request.user, container=enrollment)
        except services.EnrollmentError as exc:
            return _error(exc)

        enrollment.refresh_from_db()
        return Response(MonthlyEnrollmentSerializer(enrollment).data)


class InstallmentPlanViewSet(_EnrollmentBase):
    """/api/enrollments/installments/"""

    queryset = InstallmentPlan.objects.select_related(
        "patient", "service", "branch"
    ).prefetch_related("installments")
    serializer_class = InstallmentPlanSerializer
    filterset_fields = ["status", "patient"]

    def create(self, request, *args, **kwargs):
        serializer = InstallmentPlanCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch = Branch.objects.get(pk=request.user.branch_id)

        try:
            patient = Patient.objects.get(pk=data["patient"], branch=branch)
        except Patient.DoesNotExist:
            return Response(
                {"detail": "Patient not found in this branch."}, status=status.HTTP_404_NOT_FOUND
            )

        try:
            service = Service.objects.get(pk=data["service"], is_active=True)
        except Service.DoesNotExist:
            return Response(
                {"detail": "Service not found or no longer available."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            plan = services.create_installment_plan(
                actor=request.user,
                branch=branch,
                patient=patient,
                service=service,
                number_of_installments=data["numberOfInstallments"],
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(InstallmentPlanSerializer(plan).data, status=status.HTTP_201_CREATED)

    @action(
        detail=True, methods=["post"],
        url_path="installments/(?P<installment_id>[^/.]+)/pay",
    )
    def pay_installment(self, request, pk=None, installment_id=None):
        plan = self.get_object()
        serializer = CollectPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            installment = Installment.objects.get(pk=installment_id, plan=plan)
        except Installment.DoesNotExist:
            return Response(
                {"detail": "Installment not found."}, status=status.HTTP_404_NOT_FOUND
            )

        try:
            payment, installment = services.collect_installment_payment(
                actor=request.user,
                branch=plan.branch,
                installment=installment,
                method=serializer.validated_data["method"],
                idempotency_key=serializer.validated_data.get("idempotencyKey") or None,
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(
            {
                "payment": PaymentSerializer(payment).data,
                "plan": InstallmentPlanSerializer(
                    InstallmentPlan.objects.prefetch_related("installments").get(pk=plan.pk)
                ).data,
            }
        )

    @action(detail=True, methods=["post"])
    def terminate(self, request, pk=None):
        plan = self.get_object()
        try:
            services.terminate(actor=request.user, container=plan)
        except services.EnrollmentError as exc:
            return _error(exc)

        plan.refresh_from_db()
        return Response(InstallmentPlanSerializer(plan).data)


class BookingViewSet(_EnrollmentBase):
    """/api/enrollments/bookings/"""

    queryset = Booking.objects.select_related("patient", "service", "branch")
    serializer_class = BookingSerializer
    filterset_fields = ["status", "date"]

    def create(self, request, *args, **kwargs):
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch = Branch.objects.get(pk=request.user.branch_id)

        try:
            patient = Patient.objects.get(pk=data["patient"], branch=branch)
            service = Service.objects.get(pk=data["service"], is_active=True)
        except (Patient.DoesNotExist, Service.DoesNotExist):
            return Response(
                {"detail": "Patient or service not found."}, status=status.HTTP_404_NOT_FOUND
            )

        booking, payment = services.create_booking(
            actor=request.user,
            branch=branch,
            patient=patient,
            service=service,
            booking_date=data["date"],
            booking_time=data["time"],
            advance_amount=data["advanceAmount"],
            method=data["method"],
            idempotency_key=data.get("idempotencyKey") or None,
        )

        return Response(
            {
                "booking": BookingSerializer(booking).data,
                "payment": PaymentSerializer(payment).data,
            },
            status=status.HTTP_201_CREATED,
        )
