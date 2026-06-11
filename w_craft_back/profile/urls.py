from django.urls import path
from .views import (
    DashboardView,
    ImageModelView,
    ProfileAvatarView,
    ProfileCoverView,
    ProfileMeView,
    ProfileSettingsView,
)

urlpatterns = [
    path('dashboard/', DashboardView.as_view(), name='profile-dashboard'),
    path('settings/', ProfileSettingsView.as_view(), name='profile-settings'),
    path('me/', ProfileMeView.as_view(), name='profile-me'),
    path('me/avatar/', ProfileAvatarView.as_view(), name='profile-me-avatar'),
    path('me/cover/', ProfileCoverView.as_view(), name='profile-me-cover'),
    path('me/image-model/', ImageModelView.as_view(), name='profile-me-image-model'),
]
