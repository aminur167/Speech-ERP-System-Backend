from rest_framework.routers import DefaultRouter

from apps.services.views import PackageActionRequestViewSet, ServiceViewSet

app_name = "services"

router = DefaultRouter()
# Registered before the catalog on purpose: the catalog sits at the root
# prefix, and its detail route `/{id}/` would otherwise capture
# "action-requests" as a package id.
router.register(
    "action-requests", PackageActionRequestViewSet, basename="package-action-request"
)
router.register("", ServiceViewSet, basename="service")

urlpatterns = router.urls
