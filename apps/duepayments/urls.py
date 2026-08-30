from django.urls import path

from apps.duepayments.views import DuePaymentListView, DuePaymentSummaryView

app_name = "duepayments"

urlpatterns = [
    path("", DuePaymentListView.as_view(), name="due-list"),
    path("summary/", DuePaymentSummaryView.as_view(), name="due-summary"),
]
