from django.urls import path

from .views import (
    PersonalAPIConnectionsView,
    PersonalAPIConnectionView,
    PersonalAPIPreferenceView,
    PersonalAPISettingsView,
    PersonalAPIVideoModelsRefreshView,
)


urlpatterns = [
    path('settings/', PersonalAPISettingsView.as_view(), name='personal-api-settings'),
    path('video-models/refresh/', PersonalAPIVideoModelsRefreshView.as_view(),
         name='personal-api-video-models-refresh'),
    path('connections/', PersonalAPIConnectionsView.as_view(),
         name='personal-api-connections'),
    path('connections/<uuid:connection_id>/', PersonalAPIConnectionView.as_view(),
         name='personal-api-connection'),
    path('preferences/<str:modality>/', PersonalAPIPreferenceView.as_view(),
         name='personal-api-preference'),
]
