"""
Service serializers.

camelCase output matching the frontend's `Service` type — minus
`registrationFee`, which is deliberately absent (docs/03: registration is free,
and a money field that's displayed but never charged corrupts reconciliation).
"""

from decimal import Decimal

from rest_framework import serializers

from apps.services.models import PackageActionRequest, Service


class ServiceSerializer(serializers.ModelSerializer):
    branchId = serializers.CharField(source="branch_id", read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True)
    isOnline = serializers.BooleanField(source="is_online", read_only=True)
    originalFee = serializers.DecimalField(
        source="original_fee", max_digits=12, decimal_places=2, read_only=True
    )
    durationLabel = serializers.CharField(source="duration_label", read_only=True)
    sessionsLabel = serializers.CharField(source="sessions_label", read_only=True)
    expiryLabel = serializers.CharField(source="expiry_label", read_only=True)
    isActive = serializers.BooleanField(source="is_active", read_only=True)
    reviewStatus = serializers.CharField(source="review_status", read_only=True)
    proposedBy = serializers.CharField(source="proposed_by.name", read_only=True, default="")
    reviewNote = serializers.CharField(source="review_note", read_only=True)
    reviewedBy = serializers.CharField(source="reviewed_by.name", read_only=True, default="")
    reviewedAt = serializers.DateTimeField(source="reviewed_at", read_only=True)

    class Meta:
        model = Service
        fields = [
            "id", "branchId", "branchName", "name", "code", "category", "fee", "isOnline",
            "description", "originalFee", "durationLabel", "sessionsLabel", "expiryLabel",
            "isActive", "reviewStatus", "proposedBy", "reviewNote", "reviewedBy", "reviewedAt",
        ]
        read_only_fields = fields


class ServiceWriteSerializer(serializers.ModelSerializer):
    """
    Create/update payload.

    Note the absent `registration_fee`: because ModelSerializer only binds
    declared fields, posting one is silently ignored rather than stored — which
    is the desired behaviour while the frontend still sends it.

    `code` is absent for the same reason: the system issues it on create
    (services.next_service_code) and it never changes afterwards, so a code
    sent by a client — on create or edit — is ignored.
    """

    class Meta:
        model = Service
        fields = [
            "name", "category", "fee", "is_online", "description",
            "original_fee", "duration_label", "sessions_label", "expiry_label",
        ]
        extra_kwargs = {
            "description": {"required": False, "allow_blank": True},
            "original_fee": {"required": False, "allow_null": True},
            "duration_label": {"required": False, "allow_blank": True},
            "sessions_label": {"required": False, "allow_blank": True},
            "expiry_label": {"required": False, "allow_blank": True},
            "is_online": {"required": False},
        }

    def validate_fee(self, value):
        if value <= Decimal("0"):
            raise serializers.ValidationError("Fee must be greater than zero.")
        return value

    def validate_original_fee(self, value):
        if value is not None and value <= Decimal("0"):
            raise serializers.ValidationError("Original fee must be greater than zero.")
        return value


class ServiceReviewSerializer(serializers.Serializer):
    """Admin approves or rejects a Manager's proposed package."""

    approve = serializers.BooleanField()
    reviewNote = serializers.CharField(required=False, allow_blank=True)


class PackageActionRequestSerializer(serializers.ModelSerializer):
    serviceId = serializers.CharField(source="service_id", read_only=True)
    serviceName = serializers.CharField(source="service.name", read_only=True)
    serviceCode = serializers.CharField(source="service.code", read_only=True)
    serviceIsActive = serializers.BooleanField(source="service.is_active", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True)
    # `expired` is derived, so it is reported rather than stored.
    status = serializers.CharField(source="effective_status", read_only=True)
    requestedById = serializers.SerializerMethodField()
    requestedBy = serializers.CharField(source="requested_by.name", read_only=True, default="")
    requestedAt = serializers.DateTimeField(source="created_at", read_only=True)
    reviewedBy = serializers.CharField(source="reviewed_by.name", read_only=True, default="")
    reviewedAt = serializers.DateTimeField(source="reviewed_at", read_only=True)
    reviewNote = serializers.CharField(source="review_note", read_only=True)
    expiresAt = serializers.DateTimeField(source="expires_at", read_only=True)
    usedAt = serializers.DateTimeField(source="used_at", read_only=True)

    class Meta:
        model = PackageActionRequest
        fields = [
            "id", "serviceId", "serviceName", "serviceCode", "serviceIsActive",
            "branchId", "branchName", "action", "reason", "status",
            "requestedById", "requestedBy", "requestedAt",
            "reviewedBy", "reviewedAt", "reviewNote", "expiresAt", "usedAt",
        ]
        read_only_fields = fields

    def get_requestedById(self, obj) -> str:
        return str(obj.requested_by_id) if obj.requested_by_id else ""


class PackageActionRequestCreateSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=PackageActionRequest.Action.choices)
    reason = serializers.CharField(max_length=1000)


class PackageActionReviewSerializer(serializers.Serializer):
    reviewNote = serializers.CharField(required=False, allow_blank=True, max_length=1000)
