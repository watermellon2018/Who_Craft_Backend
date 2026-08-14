from django.urls import path

from .views import (
    CreditDemoTopUpView,
    CreditHistoryView,
    CreditSummaryView,
    CreditTransferView,
    GenerationEstimateView,
)


urlpatterns = [
    path("summary/", CreditSummaryView.as_view(), name="credit-summary"),
    path("history/", CreditHistoryView.as_view(), name="credit-history"),
    path("demo-top-up/", CreditDemoTopUpView.as_view(), name="credit-demo-top-up"),
    path("transfers/", CreditTransferView.as_view(), name="credit-transfer"),
    path(
        "generation-estimate/",
        GenerationEstimateView.as_view(),
        name="credit-generation-estimate",
    ),
]
