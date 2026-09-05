"""
Response-shape serializers for OpenAPI documentation only.

Both due-payment endpoints return plain dicts assembled in `services.py` —
there is no model instance to serialize, so the views never construct or call
these. They exist purely so drf-spectacular can describe each response in
`/api/docs/` instead of falling back to "unable to guess serializer".
"""

from rest_framework import serializers


class DuePaymentItemSerializer(serializers.Serializer):
    """
    A discriminated union in practice: `type` is "monthly" or "installment",
    and the three `installment*` fields are only present for the latter.
    """

    key = serializers.CharField()
    type = serializers.CharField()
    refId = serializers.CharField()
    itemId = serializers.CharField()
    patientId = serializers.CharField()
    patientName = serializers.CharField()
    patientCode = serializers.CharField()
    serviceId = serializers.CharField()
    serviceName = serializers.CharField()
    branchId = serializers.CharField()
    label = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    # Everything unpaid on the enrollment/plan, which is what terminating
    # writes off — `amount` is only the part payable right now.
    outstandingTotal = serializers.DecimalField(max_digits=12, decimal_places=2)
    dueDate = serializers.DateField()
    status = serializers.CharField()
    installmentIndex = serializers.IntegerField(required=False)
    installmentsTotal = serializers.IntegerField(required=False)
    installmentsRemaining = serializers.IntegerField(required=False)


class DuePaymentListSerializer(serializers.Serializer):
    """
    Paginated in Python, not by DRF's paginator -- the rows are assembled
    across two tables and already bounded to one item per active enrollment,
    so the response shape is hand-built rather than the usual
    {count, next, previous, results} pagination class.
    """

    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = DuePaymentItemSerializer(many=True)


class DueSummarySerializer(serializers.Serializer):
    totalDue = serializers.DecimalField(max_digits=14, decimal_places=2)
    monthlyDue = serializers.DecimalField(max_digits=14, decimal_places=2)
    installmentDue = serializers.DecimalField(max_digits=14, decimal_places=2)
