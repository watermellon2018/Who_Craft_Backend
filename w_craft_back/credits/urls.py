from django.urls import path

from .views import (
    CreditAdminAuditView,
    CreditAdminOperationView,
    CreditDemoTopUpView,
    CreditHistoryView,
    CreditSummaryView,
    CreditSpendingStatisticsView,
    CreditTransferView,
    GenerationEstimateView,
    ProjectCreditBudgetDetailView,
    ProjectCreditBudgetListView,
)


urlpatterns = [
    path("summary/", CreditSummaryView.as_view(), name="credit-summary"),
    path("history/", CreditHistoryView.as_view(), name="credit-history"),
    path(
        "spending-statistics/",
        CreditSpendingStatisticsView.as_view(),
        name="credit-spending-statistics",
    ),
    path("demo-top-up/", CreditDemoTopUpView.as_view(), name="credit-demo-top-up"),
    path("transfers/", CreditTransferView.as_view(), name="credit-transfer"),
    path(
        "project-budgets/",
        ProjectCreditBudgetListView.as_view(),
        name="credit-project-budget-list",
    ),
    path(
        "project-budgets/<int:project_id>/",
        ProjectCreditBudgetDetailView.as_view(),
        name="credit-project-budget-detail",
    ),
    path(
        "admin/operations/",
        CreditAdminOperationView.as_view(),
        name="credit-admin-operation",
    ),
    path(
        "admin/audit/",
        CreditAdminAuditView.as_view(),
        name="credit-admin-audit",
    ),
    path(
        "generation-estimate/",
        GenerationEstimateView.as_view(),
        name="credit-generation-estimate",
    ),
]
