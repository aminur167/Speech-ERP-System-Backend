from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.payments.views import PaymentViewSet, RefundRequestViewSet

app_name = "payments"

payment_router = DefaultRouter()
payment_router.register("payments", PaymentViewSet, basename="payment")
payment_router.register("refund-requests", RefundRequestViewSet, basename="refund-request")

urlpatterns = [path("", include(payment_router.urls))]
