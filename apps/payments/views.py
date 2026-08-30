"""Payment, void, and refund endpoints."""

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsAdmin, IsManager
from apps.patients.models import Patient
from apps.payments import services
from apps.payments.models import Payment, RefundRequest
from apps.payments.serializers import (
    PaymentCreateSerializer,
    PaymentSerializer,
    RefundApproveSerializer,
    RefundRejectSerializer,
    RefundRequestCreateSerializer,
    RefundRequestSerializer,
    VoidSerializer,
)


def _error_response(exc: services.PaymentError, http_status=status.HTTP_400_BAD_REQUEST):
    body = {"detail": exc.message, "code": exc.code}
    body.update(exc.extra)
    return Response(body, status=http_status)


class PaymentViewSet(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    """/api/payments/"""

    queryset = Payment.objects.select_related("patient", "branch", "collected_by").all()
    serializer_class = PaymentSerializer
    http_method_names = ["get", "post", "head", "options"]  # never PUT/DELETE money

    def get_permissions(self):
        # Collecting a payment and opening a refund request are branch-desk
        # actions -- Admin can see every payment but doesn't transact on a
        # branch's behalf (docs/04, docs/07). Void is different: Manager may
        # void same-day only, Admin may void any day (docs/04) -- both roles
        # reach the endpoint and void_payment() enforces the day cutoff
        # itself. Approve/reject a refund carries its own IsAdmin via @action.
        if self.action in {"create", "request_refund"}:
            return [IsManager()]
        return [IsAuthenticated()]

    def create(self, request, *args, **kwargs):
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch_id = self.get_effective_branch_id()
        branch = Branch.objects.get(pk=branch_id)

        # Scope the patient lookup to the same branch, so a manager can't take
        # a payment against another branch's patient by posting their id.
        try:
            patient = Patient.objects.get(pk=data["patient"], branch_id=branch_id)
        except Patient.DoesNotExist:
            return Response(
                {"detail": "Patient not found in this branch."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            payment, created = services.create_payment(
                actor=request.user,
                branch=branch,
                patient=patient,
                amount=data["amount"],
                method=data["method"],
                category=data.get("category", ""),
                description=data.get("description", ""),
                idempotency_key=data.get("idempotencyKey") or None,
                client_created_at=data.get("clientCreatedAt"),
            )
        except services.PaymentError as exc:
            return _error_response(exc)

        # A replayed idempotency key returns the original payment with 200, so
        # the client can tell "already recorded" from "newly recorded".
        return Response(
            PaymentSerializer(payment).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        payment = self.get_object()
        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            payment = services.void_payment(
                actor=request.user, payment=payment, reason=serializer.validated_data["reason"]
            )
        except services.PaymentError as exc:
            # Same-day and closing-cutoff refusals are authorisation failures,
            # not malformed input.
            http_status = (
                status.HTTP_403_FORBIDDEN
                if exc.code in {"not_same_day", "closing_submitted"}
                else status.HTTP_400_BAD_REQUEST
            )
            return _error_response(exc, http_status)

        return Response(PaymentSerializer(payment).data)

    @action(detail=True, methods=["post"], url_path="refund-requests",
            permission_classes=[IsManager])
    def request_refund(self, request, pk=None):
        """
        Manager opens a refund request. Manager-only by design: an admin
        approving their own request would defeat the separation of duties.
        """
        payment = self.get_object()
        serializer = RefundRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        items = [
            {"material_id": item["material"], "quantity": item["quantity"]}
            for item in data.get("items", [])
        ]

        try:
            refund = services.request_refund(
                actor=request.user,
                payment=payment,
                amount=data.get("amount"),
                reason=data["reason"],
                items=items or None,
            )
        except services.PaymentError as exc:
            return _error_response(exc)

        return Response(
            RefundRequestSerializer(refund).data, status=status.HTTP_201_CREATED
        )


class RefundRequestViewSet(BranchScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """
    /api/refund-requests/ — the Admin approval queue.

    Managers can see their branch's requests (to track what they submitted);
    only Admin can act on them.
    """

    queryset = RefundRequest.objects.select_related(
        "payment", "payment__patient", "payment__branch", "requested_by", "reviewed_by"
    ).prefetch_related("items__material")
    serializer_class = RefundRequestSerializer
    filterset_fields = ["status"]

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def approve(self, request, pk=None):
        refund = self.get_object()
        serializer = RefundApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            refund = services.approve_refund(
                actor=request.user,
                request=refund,
                bill_action=data["billAction"],
                refund_method=data.get("refundMethod", ""),
                review_note=data.get("reviewNote", ""),
            )
        except services.PaymentError as exc:
            return _error_response(exc)

        return Response(RefundRequestSerializer(refund).data)

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def reject(self, request, pk=None):
        refund = self.get_object()
        serializer = RefundRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            refund = services.reject_refund(
                actor=request.user,
                request=refund,
                review_note=serializer.validated_data["reviewNote"],
            )
        except services.PaymentError as exc:
            return _error_response(exc)

        return Response(RefundRequestSerializer(refund).data)
