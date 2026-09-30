from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.common.approvals import ApprovalPulseView
from apps.common.views import AuditLogViewSet, SystemSettingsView

app_name = "common"

router = DefaultRouter()
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

urlpatterns = router.urls + [
    path("settings/system/", SystemSettingsView.as_view(), name="system-settings"),
    path("approvals/pulse/", ApprovalPulseView.as_view(), name="approval-pulse"),
]
