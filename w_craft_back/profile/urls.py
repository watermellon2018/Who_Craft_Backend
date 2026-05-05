from django.urls import path
from .views import DashboardView, ProfileSettingsView

urlpatterns = [
    path('dashboard/', DashboardView.as_view(), name='profile-dashboard'),
    path('settings/', ProfileSettingsView.as_view(), name='profile-settings'),
]
