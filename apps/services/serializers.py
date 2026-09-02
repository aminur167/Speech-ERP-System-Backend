"""
Service serializers.

camelCase output matching the frontend's `Service` type — minus
`registrationFee`, which is deliberately absent (docs/03: registration is free,
and a money field that's displayed but never charged corrupts reconciliation).
"""

from decimal import Decimal

from rest_framework import serializers

from apps.services.models import Service


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
    """

    class Meta:
        model = Service
        fields = [
            "name", "code", "category", "fee", "is_online", "description",
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

    def validate_code(self, value):
        value = value.strip().upper()
        branch = self.context.get("branch") or (self.instance.branch if self.instance else None)
        existing = Service.all_objects.filter(code=value, branch=branch)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError(
                "This branch already has a package with this code."
            )
        return value

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
