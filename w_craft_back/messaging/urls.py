from django.urls import path

from .views import (
    MessagingChangesView,
    MessagingConversationsView,
    MessagingConversationView,
    MessagingMessagesView,
    MessagingMessageView,
    MessagingReadView,
    MessagingUnreadView,
    MessagingUsersView,
)


urlpatterns = [
    path('users/', MessagingUsersView.as_view(), name='messaging-users'),
    path('unread/', MessagingUnreadView.as_view(), name='messaging-unread'),
    path('conversations/', MessagingConversationsView.as_view(),
         name='messaging-conversations'),
    path('conversations/<uuid:conversation_id>/',
         MessagingConversationView.as_view(), name='messaging-conversation'),
    path('conversations/<uuid:conversation_id>/messages/',
         MessagingMessagesView.as_view(), name='messaging-messages'),
    path('conversations/<uuid:conversation_id>/messages/<int:message_id>/',
         MessagingMessageView.as_view(), name='messaging-message'),
    path('conversations/<uuid:conversation_id>/changes/',
         MessagingChangesView.as_view(), name='messaging-changes'),
    path('conversations/<uuid:conversation_id>/read/',
         MessagingReadView.as_view(), name='messaging-read'),
]
