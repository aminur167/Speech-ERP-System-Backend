from rest_framework import serializers

from apps.notifications.models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    isRead = serializers.BooleanField(source="is_read", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Notification
        fields = ["id", "title", "message", "link", "isRead", "createdAt"]
        read_only_fields = fields
