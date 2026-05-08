from django.urls import path

from .views import (
    ChannelSearchView,
    ChannelSubscribeView,
    ChannelSubscriptionSettingsView,
    MySubscriptionsView,
    UserSubscribersView,
    UserSubscriptionsView,
)


# Mounted at /api/ in backend/urls.py
urlpatterns = [
    path('subscriptions/', MySubscriptionsView.as_view(), name='my-subscriptions'),
    path('channels/search/', ChannelSearchView.as_view(), name='channels-search'),
    path('channels/<int:user_id>/subscribe/', ChannelSubscribeView.as_view(), name='channel-subscribe'),
    path('channels/<int:user_id>/subscription/', ChannelSubscriptionSettingsView.as_view(), name='channel-subscription-settings'),
    path('users/<int:user_id>/subscribers/', UserSubscribersView.as_view(), name='user-subscribers'),
    path('users/<int:user_id>/subscriptions/', UserSubscriptionsView.as_view(), name='user-subscriptions'),
]
