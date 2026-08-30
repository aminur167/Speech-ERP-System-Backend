from django.urls import path

from apps.reporting import views

app_name = "reporting"

urlpatterns = [
    path("", views.TransactionListView.as_view(), name="transaction-list"),
    path("summary/", views.TransactionsSummaryView.as_view(), name="summary"),
    path("trend/", views.RevenueTrendView.as_view(), name="trend"),
    path("by-method/", views.RevenueByMethodView.as_view(), name="by-method"),
    path("by-category/", views.RevenueByCategoryView.as_view(), name="by-category"),
    path("dashboard-metrics/", views.DashboardMetricsView.as_view(), name="dashboard-metrics"),
    path("collection-for-date/", views.CollectionForDateView.as_view(), name="collection-for-date"),
    path("refunds-voids/", views.RefundsAndVoidsView.as_view(), name="refunds-voids"),
    path("net-revenue/", views.NetRevenueView.as_view(), name="net-revenue"),
]
