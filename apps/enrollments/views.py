"""
Enrollment endpoints.

Enrolling and collecting are branch-desk actions, so they're Manager-only:
Admin can see everything but shouldn't transact on a branch's behalf.
"""

from drf_spectacular.utils import OpenApiParameter, extend_schema
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsManager
from apps.enrollments import services
from apps.enrollments.models import (
    Booking,
    EnrollmentStatus,
    Installment,
    InstallmentPlan,
    MonthlyBill,
    MonthlyEnrollment,
)
from apps.enrollments.serializers import (
    AdvanceOptionsSerializer,
    AdvancePreviewSerializer,
    BookingCreateSerializer,
    CollectAdvanceSerializer,
    BookingSerializer,
    CancelBookingSerializer,
    CollectPaymentSerializer,
    InstallmentPlanCreateSerializer,
    InstallmentPlanSerializer,
    MonthlyEnrollmentCreateSerializer,
    MonthlyEnrollmentSerializer,
    StopMonthlyServiceSerializer,
    StopPreviewSerializer,
    TerminatedMonthlyServiceSerializer,
)
from apps.patients.models import Patient
from apps.payments.serializers import PaymentSerializer
from apps.services.models import Service


class _CalendarPagination(PageNumberPagination):
    page_size = 200


def _error(exc: services.EnrollmentError, http_status=status.HTTP_400_BAD_REQUEST):
    body = {"detail": exc.message, "code": exc.code}
    body.update(exc.extra)
    return Response(body, status=http_status)


class _EnrollmentBase(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    http_method_names = ["get", "post", "head", "options"]

    # Reads, as opposed to the branch-desk actions below them. Admin can see
    # everything; only a Manager transacts on a branch's behalf.
    READ_ACTIONS = {
        "list", "retrieve", "terminated", "stop_preview", "advance_preview",
        "advance_options",
    }

    def get_permissions(self):
        if self.action in self.READ_ACTIONS:
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

        try:
            enrollment = services.create_monthly_enrollment(
                actor=request.user, branch=branch, patient=patient, service=service
            )
        except services.EnrollmentError as exc:
            # Chiefly the outstanding-due gate. Enforced in the service layer,
            # so calling this endpoint directly with the screen bypassed is
            # refused on exactly the same terms as the button.
            return _error(exc)

        return Response(
            MonthlyEnrollmentSerializer(enrollment).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(parameters=[OpenApiParameter("bill_id", int, location="path")])
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

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "search",
                str,
                description=(
                    "Patient name, patient code, phone, or service code/name — "
                    "the four things a manager has to hand when looking one up."
                ),
            ),
            OpenApiParameter("month", str, description='Terminated cycle, "YYYY-MM".'),
            OpenApiParameter(
                "kind",
                str,
                description='"unpaid_due" (the nightly job) or "manual" (a manager).',
            ),
            OpenApiParameter("pageSize", int),
        ],
        responses=TerminatedMonthlyServiceSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def terminated(self, request):
        """
        Every stopped monthly service — the job's, for an unpaid due, and the
        manager's own — and the one place either can be restarted.

        Both belong here because both describe the same thing to whoever is
        looking: this patient's monthly service is not running. `kind` tells
        them apart, and narrows the list when only one is wanted. What
        differs is only what resuming costs: a manager's termination already
        wrote the debt off, so there is nothing left to collect.
        """
        queryset = (
            self.get_queryset()
            .filter(status=EnrollmentStatus.TERMINATED)
            .order_by("-terminated_at")
        )

        kind = request.query_params.get("kind")
        if kind:
            queryset = queryset.filter(terminated_kind=kind)

        month = request.query_params.get("month")
        if month:
            queryset = queryset.filter(terminated_month=month)

        search = request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(patient__name__icontains=search)
                | Q(patient__patient_code__icontains=search)
                | Q(patient__phone__icontains=search)
                | Q(service__code__icontains=search)
                | Q(service__name__icontains=search)
            )

        paginator = PageNumberPagination()
        paginator.page_size = int(request.query_params.get("pageSize", 10) or 10)
        page = paginator.paginate_queryset(queryset, request)

        return paginator.get_paginated_response(
            TerminatedMonthlyServiceSerializer(page, many=True).data
        )

    @extend_schema(
        parameters=[OpenApiParameter("through", str, description='Month as "YYYY-MM".')],
        responses=AdvancePreviewSerializer,
    )
    @extend_schema(responses=AdvanceOptionsSerializer)
    @action(detail=True, methods=["get"], url_path="advance-options")
    def advance_options(self, request, pk=None):
        """The future months that may be ticked, and what blocks ticking them."""
        return Response(services.advance_month_options(self.get_object()))

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "months", str,
                description='Comma-separated months as "YYYY-MM,YYYY-MM".',
            )
        ],
        responses=AdvancePreviewSerializer,
    )
    @action(detail=True, methods=["get"], url_path="advance-preview")
    def advance_preview(self, request, pk=None):
        """What the ticked months would cost, before anyone commits to it."""
        raw = request.query_params.get("months", "")
        months = [part for part in (m.strip() for m in raw.split(",")) if part]
        try:
            return Response(
                services.preview_monthly_advance(
                    enrollment=self.get_object(), months=months
                )
            )
        except services.EnrollmentError as exc:
            return _error(exc)

    @extend_schema(request=CollectAdvanceSerializer)
    @action(detail=True, methods=["post"], url_path="pay-advance")
    def pay_advance(self, request, pk=None):
        """
        Take payment for the months that were ticked.

        Returns one payment per month rather than a single lump: each month
        keeps its own receipt, which is what makes an advance auditable month
        by month.
        """
        enrollment = self.get_object()
        serializer = CollectAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            payments, enrollment = services.collect_monthly_advance(
                actor=request.user,
                branch=enrollment.branch,
                enrollment=enrollment,
                months=data["months"],
                method=data["method"],
                idempotency_key=data.get("idempotencyKey") or None,
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(
            {
                "payments": PaymentSerializer(payments, many=True).data,
                "enrollment": MonthlyEnrollmentSerializer(
                    MonthlyEnrollment.objects.prefetch_related("bills").get(pk=enrollment.pk)
                ).data,
            }
        )

    @extend_schema(responses=StopPreviewSerializer)
    @action(detail=True, methods=["get"], url_path="stop-preview")
    def stop_preview(self, request, pk=None):
        """What stopping this service would mean, before anyone commits to it."""
        return Response(services.stoppable_months(self.get_object()))

    @extend_schema(request=StopMonthlyServiceSerializer)
    @action(detail=True, methods=["post"])
    def stop(self, request, pk=None):
        """
        Stop one service, deciding each unpaid month separately.

        Distinct from `terminate`, which waives everything in one go and is
        what the Due Payments screen still uses. Both call the same service
        function with different arguments, so there is one implementation of
        what stopping means.
        """
        serializer = StopMonthlyServiceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        decisions = {
            entry["billId"]: {
                "action": entry["action"],
                "reason": entry.get("reason", ""),
            }
            for entry in data["decisions"]
        }

        try:
            enrollment = services.stop_monthly_service(
                actor=request.user,
                enrollment=self.get_object(),
                decisions=decisions,
                reason=data.get("reason", ""),
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(
            MonthlyEnrollmentSerializer(
                MonthlyEnrollment.objects.prefetch_related("bills").get(pk=enrollment.pk)
            ).data
        )

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        """
        Reactivate an inactive service.

        Refused while the patient owes anything — the manager clears it on the
        Due Payments screen first. The refusal carries the months and the
        total so the screen can say what has to be paid rather than only that
        something does.
        """
        try:
            enrollment = services.resume_monthly_service(
                actor=request.user, enrollment=self.get_object()
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(
            MonthlyEnrollmentSerializer(
                MonthlyEnrollment.objects.prefetch_related("bills").get(pk=enrollment.pk)
            ).data
        )


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
                starts_on=data.get("startsOn"),
                ends_on=data.get("endsOn"),
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(InstallmentPlanSerializer(plan).data, status=status.HTTP_201_CREATED)

    @extend_schema(parameters=[OpenApiParameter("installment_id", int, location="path")])
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
                # Omitted collects the scheduled figure; a smaller amount is
                # carried into the later installments by the service.
                amount=serializer.validated_data.get("amount"),
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
        """
        Close a plan wholesale, waiving whatever is left.

        Kept beside `stop` rather than replaced by it: this is the blunt
        write-off, and `stop` is the manager's per-item Inactive decision.
        Only `stop` is on a screen.
        """
        plan = self.get_object()
        try:
            services.terminate(actor=request.user, container=plan)
        except services.EnrollmentError as exc:
            return _error(exc)

        plan.refresh_from_db()
        return Response(InstallmentPlanSerializer(plan).data)

    @extend_schema(responses=StopPreviewSerializer)
    @action(detail=True, methods=["get"], url_path="stop-preview")
    def stop_preview(self, request, pk=None):
        """What making this plan inactive would mean, before anyone commits."""
        return Response(services.stoppable_installments(self.get_object()))

    @extend_schema(request=StopMonthlyServiceSerializer)
    @action(detail=True, methods=["post"])
    def stop(self, request, pk=None):
        """
        Make one installment service inactive, deciding each unpaid part.

        The same dialog and the same rules as a monthly service: every unpaid
        item decided explicitly, every cancellation carrying a reason.
        """
        serializer = StopMonthlyServiceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        decisions = {
            entry["billId"]: {
                "action": entry["action"],
                "reason": entry.get("reason", ""),
            }
            for entry in data["decisions"]
        }

        try:
            plan = services.stop_installment_plan(
                actor=request.user,
                plan=self.get_object(),
                decisions=decisions,
                reason=data.get("reason", ""),
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(
            InstallmentPlanSerializer(
                InstallmentPlan.objects.prefetch_related("installments").get(pk=plan.pk)
            ).data
        )

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        """Reactivate an inactive plan — refused while anything is owed."""
        try:
            plan = services.resume_installment_plan(
                actor=request.user, plan=self.get_object()
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(
            InstallmentPlanSerializer(
                InstallmentPlan.objects.prefetch_related("installments").get(pk=plan.pk)
            ).data
        )


class BookingViewSet(_EnrollmentBase):
    """/api/enrollments/bookings/"""

    queryset = Booking.objects.select_related("patient", "service", "branch")
    serializer_class = BookingSerializer
    filterset_fields = ["status", "date"]
    # A calendar view wants every booking in the requested date range at
    # once, not the site-wide default of 10 per page -- the range itself
    # already bounds the result to a sane size (a branch's bookings for a
    # month, not the whole table). Keeps the standard paginated envelope
    # (count/results) rather than disabling pagination outright.
    pagination_class = _CalendarPagination

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "dateFrom", str, description="Inclusive. Calendar month/week views."
            ),
            OpenApiParameter("dateTo", str, description="Inclusive."),
        ],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        # A separate pair of query params rather than `filterset_fields`'s
        # exact-match `date` -- a calendar view needs "everything in this
        # month", not one day at a time.
        queryset = super().get_queryset()
        date_from = self.request.query_params.get("dateFrom")
        date_to = self.request.query_params.get("dateTo")
        if date_from:
            queryset = queryset.filter(date__gte=date_from)
        if date_to:
            queryset = queryset.filter(date__lte=date_to)
        return queryset

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

        try:
            booking, payment = services.create_booking(
                actor=request.user,
                branch=branch,
                patient=patient,
                service=service,
                booking_date=data["date"],
                booking_time=data["time"],
                method=data["method"],
                idempotency_key=data.get("idempotencyKey") or None,
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(
            {
                "booking": BookingSerializer(booking).data,
                "payment": PaymentSerializer(payment).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        serializer = CancelBookingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            booking = services.cancel_booking(
                actor=request.user, booking=booking, reason=serializer.validated_data["reason"]
            )
        except services.EnrollmentError as exc:
            return _error(exc)

        return Response(BookingSerializer(booking).data)
