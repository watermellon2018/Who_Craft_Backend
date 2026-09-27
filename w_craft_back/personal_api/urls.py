from django.urls import path

from .views import (
    PersonalAPIConnectionsView,
    PersonalAPIConnectionView,
    PersonalAPIPreferenceView,
    PersonalAPISettingsView,
)


urlpatterns = [
    path('settings/', PersonalAPISettingsView.as_view(), name='personal-api-settings'),
    path('connections/', PersonalAPIConnectionsView.as_view(),
         name='personal-api-connections'),
    path('connections/<uuid:connection_id>/', PersonalAPIConnectionView.as_view(),
         name='personal-api-connection'),
    path('preferences/<str:modality>/', PersonalAPIPreferenceView.as_view(),
         name='personal-api-preference'),
]
