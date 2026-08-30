"""Material inventory and POS endpoints."""

from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.branches.models import Branch
from apps.common.mixins import BranchScopedQuerySetMixin
from apps.common.permissions import IsManager
from apps.materials import services
from apps.materials.models import Material
from apps.materials.serializers import (
    AdjustStockSerializer,
    MaterialMovementSerializer,
    MaterialSerializer,
    MaterialsSummarySerializer,
    MaterialWriteSerializer,
    SellMaterialsSerializer,
)
from apps.patients.models import Patient
from apps.payments.serializers import PaymentSerializer


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

        branch = Branch.objects.get(pk=request.user.branch_id)
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
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Aggregated in the database rather than by iterating rows — the
        difference is invisible with five materials and matters with years of
        catalogue growth.
        """
        queryset = self.get_queryset()

        totals = queryset.aggregate(
            stock_value=Sum(
                ExpressionWrapper(
                    F("quantity") * F("unit_cost"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                )
            )
        )
        low_stock = queryset.filter(quantity__lte=F("reorder_level")).count()

        return Response(
            MaterialsSummarySerializer(
                {
                    "totalItems": queryset.count(),
                    "totalStockValue": totals["stock_value"] or Decimal("0.00"),
                    "lowStockCount": low_stock,
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

        branch = Branch.objects.get(pk=request.user.branch_id)

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
