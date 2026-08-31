from rest_framework import serializers

from apps.common.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    """
    Read-only. AuditLog rows are never created or modified through the API --
    see `apps/common/audit.py` for the one place entries are written, from
    inside the service functions that actually perform each action.
    """

    actorEmail = serializers.CharField(source="actor_email", read_only=True)
    targetType = serializers.CharField(source="target_type", read_only=True)
    targetId = serializers.CharField(source="target_id", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True, default=None)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            "id", "actorEmail", "action", "targetType", "targetId",
            "branchId", "branchName", "reason", "changes", "createdAt",
        ]
        read_only_fields = fields
