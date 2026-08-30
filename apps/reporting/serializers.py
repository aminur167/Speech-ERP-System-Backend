"""
Response-shape serializers for OpenAPI documentation only.

Reporting endpoints return plain dicts straight from `services.py` — there is
no model instance to serialize, so the views never construct or call these.
They exist purely so drf-spectacular can describe each endpoint's response in
`/api/docs/` instead of falling back to "unable to guess serializer" and
omitting the shape entirely.
"""

from rest_framework import serializers


class ByMethodRowSerializer(serializers.Serializer):
    method = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)


class TransactionsSummarySerializer(serializers.Serializer):
    totalCollected = serializers.DecimalField(max_digits=14, decimal_places=2)
    transactionCount = serializers.IntegerField()
    todayCollected = serializers.DecimalField(max_digits=14, decimal_places=2)
    monthCollected = serializers.DecimalField(max_digits=14, decimal_places=2)
    monthRefunded = serializers.DecimalField(max_digits=14, decimal_places=2)
    byMethod = ByMethodRowSerializer(many=True)


class RevenueTrendPointSerializer(serializers.Serializer):
    date = serializers.DateField()
    label = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)


class RevenueByCategoryRowSerializer(serializers.Serializer):
    category = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)


class DashboardMetricsSerializer(serializers.Serializer):
    todayPatientsSeen = serializers.IntegerField()
    todayDueCollected = serializers.DecimalField(max_digits=14, decimal_places=2)


class CollectionForDateSerializer(serializers.Serializer):
    date = serializers.DateField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)


class RefundOrVoidRowSerializer(serializers.Serializer):
    id = serializers.CharField()
    receiptNumber = serializers.CharField()
    patientName = serializers.CharField()
    patientCode = serializers.CharField()
    method = serializers.CharField()
    status = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    createdAt = serializers.DateTimeField()


class NetRevenueSerializer(serializers.Serializer):
    grossCollected = serializers.DecimalField(max_digits=14, decimal_places=2)
    refunded = serializers.DecimalField(max_digits=14, decimal_places=2)
    expenses = serializers.DecimalField(max_digits=14, decimal_places=2)
    netRevenue = serializers.DecimalField(max_digits=14, decimal_places=2)
