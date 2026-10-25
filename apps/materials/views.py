"""Material inventory and POS endpoints."""

from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.mixins import BranchScopedQuerySetMixin, manager_branch
from apps.common.permissions import IsManager
from apps.materials import services
from apps.materials.models import Material
from apps.materials.serializers import (
    AdjustStockSerializer,
    MaterialMovementSerializer,
    MaterialSerializer,
    MaterialsSummarySerializer,
    MaterialWriteSerializer,
    SaleCustomerSerializer,
    SaleHistorySerializer,
    SellMaterialsSerializer,
)
from apps.patients.models import Patient
from apps.payments.serializers import PaymentSerializer
from apps.common import audit
from apps.common.models import AuditLog


def _error(exc: services.MaterialError, http_status=status.HTTP_400_BAD_REQUEST):
    body = {"detail": exc.message, "code": exc.code}
    body.update(exc.extra)
    return Response(body, status=http_status)


class MaterialViewSet(BranchScopedQuerySetMixin, viewsets.ModelViewSet):
    """
    /api/materials/

    Branch-scoped, unlike the service catalog — physical stock doesn't move
    between locations, so each branch owns its own items.
    """

    queryset = Material.objects.select_related("branch").all()
    serializer_class = MaterialSerializer
    search_fields = ["name", "code"]
    ordering_fields = ["name", "quantity", "created_at"]

    def get_permissions(self):
        if self.action in {"list", "retrieve", "summary", "movements"}:
            return [IsAuthenticated()]
        return [IsManager()]

    def create(self, request, *args, **kwargs):
        serializer = MaterialWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        branch = manager_branch(request)
        material = services.create_material(
            actor=request.user, branch=branch, data=dict(serializer.validated_data)
        )
        return Response(MaterialSerializer(material).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        material = self.get_object()
        serializer = MaterialWriteSerializer(
            instance=material, data=request.data, partial=kwargs.pop("partial", False)
        )
        serializer.is_valid(raise_exception=True)
        material = serializer.save()
        return Response(MaterialSerializer(material).data)

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, partial=True, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        Soft delete — the movement and sale history must stay resolvable, so
        the row can't go away.
        """
        material = self.get_object()
        material.delete()
        audit.record(
            actor=request.user,
            action=AuditLog.Action.SOFT_DELETE,
            target=material,
            branch=material.branch,
            changes={"code": material.code},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Aggregated in the database rather than by iterating rows — the
        difference is invisible with five materials and matters with years of
        catalogue growth.
        """
        queryset = self.get_queryset()

        # Item count, stock value and low-stock count: one query.
        totals = queryset.aggregate(
            items=Count("pk"),
            stock_value=Sum(
                ExpressionWrapper(
                    F("quantity") * F("unit_cost"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                )
            ),
            low_stock=Count("pk", filter=Q(quantity__lte=F("reorder_level"))),
        )

        return Response(
            MaterialsSummarySerializer(
                {
                    "totalItems": totals["items"],
                    "totalStockValue": totals["stock_value"] or Decimal("0.00"),
                    "lowStockCount": totals["low_stock"],
                }
            ).data
        )

    @action(detail=True, methods=["get"])
    def movements(self, request, pk=None):
        material = self.get_object()
        return Response(
            MaterialMovementSerializer(
                material.movements.select_related("created_by")[:100], many=True
            ).data
        )

    @action(detail=True, methods=["post"], url_path="adjust-stock",
            permission_classes=[IsManager])
    def adjust_stock(self, request, pk=None):
        material = self.get_object()
        serializer = AdjustStockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            material = services.adjust_stock(
                actor=request.user,
                material=material,
                movement_type=data["type"],
                quantity=data["quantity"],
                note=data.get("note", ""),
                idempotency_key=data.get("idempotencyKey"),
                client_created_at=data.get("clientCreatedAt"),
            )
        except services.MaterialError as exc:
            return _error(exc)

        return Response(MaterialSerializer(material).data)

    @action(detail=False, methods=["post"], permission_classes=[IsManager])
    def sell(self, request):
        """
        POS checkout. The request carries ids and quantities only — pricing is
        the server's job (see services.sell_materials).
        """
        serializer = SellMaterialsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch = manager_branch(request)

        try:
            patient = Patient.objects.get(pk=data["patient"], branch=branch)
        except Patient.DoesNotExist:
            return Response(
                {"detail": "Patient not found in this branch."}, status=status.HTTP_404_NOT_FOUND
            )

        items = [
            {"material_id": item["material"], "quantity": item["quantity"]}
            for item in data["items"]
        ]

        try:
            payment, _sale_items, created = services.sell_materials(
                actor=request.user,
                branch=branch,
                patient=patient,
                items=items,
                method=data["method"],
                idempotency_key=data.get("idempotencyKey") or None,
            )
        except services.MaterialError as exc:
            return _error(exc)

        return Response(
            {"payment": PaymentSerializer(payment).data},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SaleHistoryViewSet(viewsets.ViewSet):
    """
    /api/materials/sales/ — what was sold at the counter, to whom.

    Branch scoping is done by hand rather than through
    `BranchScopedQuerySetMixin`: the rows are Payments, not Materials, so the
    mixin's model-level queryset doesn't apply, but the rule it enforces does
    — a Manager sees their own branch and nothing else, Admin sees every
    branch and may narrow to one with `?branch=`.
    """

    permission_classes = [IsAuthenticated]
    # A plain ViewSet gives drf-spectacular nothing to infer a response shape
    # from, and a view it cannot resolve is dropped from the schema silently
    # -- which `test_openapi_schema` fails the build over. Naming the
    # serializer here is what keeps these three endpoints in /api/docs/.
    serializer_class = SaleHistorySerializer
    lookup_value_regex = "[0-9]+"

    def _scope_branch(self, request):
        if request.user.is_manager:
            return request.user.branch_id
        return request.query_params.get("branch") or None

    def _get_sale(self, request, pk):
        """A sale this user is allowed to touch, or 404 — never 403."""
        from apps.payments.models import Payment, PaymentCategory

        queryset = Payment.objects.filter(
            pk=pk, category=PaymentCategory.MATERIAL_SALE
        ).select_related("patient", "branch")

        if request.user.is_manager:
            queryset = queryset.filter(branch_id=request.user.branch_id)

        sale = queryset.first()
        if sale is None:
            raise NotFound("That sale doesn't exist.")
        return sale

    @extend_schema(
        tags=["materials"],
        parameters=[
            OpenApiParameter("branch", str, description="Admin only; a Manager is always scoped to their own."),
            OpenApiParameter("search", str, description="Buyer name or phone, product name or code, receipt number."),
            OpenApiParameter("date", str, description="A single day, YYYY-MM-DD. Overrides the range below."),
            OpenApiParameter("dateFrom", str),
            OpenApiParameter("dateTo", str),
            OpenApiParameter("pageSize", int),
        ],
        responses=SaleHistorySerializer(many=True),
    )
    def list(self, request):
        queryset = services.sale_history_queryset(
            branch_id=self._scope_branch(request), params=request.query_params
        )

        paginator = PageNumberPagination()
        paginator.page_size = int(request.query_params.get("pageSize", 10) or 10)
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(
            SaleHistorySerializer(page, many=True).data
        )

    @extend_schema(
        tags=["materials"],
        request=SaleCustomerSerializer,
        responses=SaleHistorySerializer,
    )
    @action(detail=True, methods=["patch"])
    def customer(self, request, pk=None):
        """PATCH /api/materials/sales/{id}/customer/ — fix the buyer's name or phone."""
        sale = self._get_sale(request, pk)

        serializer = SaleCustomerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            sale = services.update_sale_customer(
                actor=request.user,
                payment=sale,
                name=serializer.validated_data["name"],
                phone=serializer.validated_data["phone"],
            )
        except services.MaterialError as exc:
            return _error(exc)

        sale.refresh_from_db()
        return Response(SaleHistorySerializer(sale).data)

    @extend_schema(tags=["materials"], responses=SaleHistorySerializer)
    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        """
        POST /api/materials/sales/{id}/void/ — remove a sale from the history.

        The same operation and the same rules as voiding any other payment
        (Manager: today only; Admin: any day), reached from this screen
        instead of Transactions. The money record survives as a voided
        payment; the goods go back on the shelf.
        """
        from apps.payments import services as payment_services

        sale = self._get_sale(request, pk)
        reason = (request.data.get("reason") or "").strip()

        try:
            payment_services.void_payment(
                actor=request.user, payment=sale, reason=reason
            )
        except payment_services.PaymentError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sale.refresh_from_db()
        return Response(SaleHistorySerializer(sale).data)
