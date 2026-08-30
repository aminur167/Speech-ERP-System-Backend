from rest_framework.routers import DefaultRouter

from apps.branches.views import BranchViewSet

app_name = "branches"

router = DefaultRouter()
router.register("", BranchViewSet, basename="branch")

urlpatterns = router.urls
