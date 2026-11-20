"""Authenticated JSON endpoints for personal messaging."""

from typing import Any
from uuid import UUID

from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import (
    ChangesSerializer,
    EditMessageSerializer,
    HistorySerializer,
    OpenConversationSerializer,
    PageSerializer,
    ReadSerializer,
    SearchSerializer,
    SendMessageSerializer,
)


def _validated(serializer_type: type[serializers.Serializer], data: Any) -> dict:
    serializer = serializer_type(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class MessagingUsersView(APIView):
    def get(self, request: Request) -> Response:
        return Response(services.search_users(
            request.user, request=request,
            **_validated(SearchSerializer, request.query_params),
        ))


class MessagingUnreadView(APIView):
    def get(self, request: Request) -> Response:
        return Response({'unread_count': services.unread_count(request.user)})


class MessagingConversationsView(APIView):
    def get(self, request: Request) -> Response:
        return Response(services.list_conversations(
            request.user, request=request,
            **_validated(PageSerializer, request.query_params),
        ))

    def post(self, request: Request) -> Response:
        return Response(services.open_conversation(
            request.user, request=request,
            **_validated(OpenConversationSerializer, request.data),
        ))


class MessagingConversationView(APIView):
    def get(self, request: Request, conversation_id: UUID) -> Response:
        return Response(services.get_conversation(
            request.user, conversation_id, request=request,
        ))

    def delete(self, request: Request, conversation_id: UUID) -> Response:
        return Response(services.clear_conversation(
            request.user, conversation_id, request=request,
        ))


class MessagingMessagesView(APIView):
    def get(self, request: Request, conversation_id: UUID) -> Response:
        return Response(services.message_history(
            request.user, conversation_id, request=request,
            **_validated(HistorySerializer, request.query_params),
        ))

    def post(self, request: Request, conversation_id: UUID) -> Response:
        return Response(services.send_message(
            request.user, conversation_id, request=request,
            **_validated(SendMessageSerializer, request.data),
        ), status=201)


class MessagingMessageView(APIView):
    def patch(
        self, request: Request, conversation_id: UUID, message_id: int,
    ) -> Response:
        return Response(services.edit_message(
            request.user, conversation_id, message_id, request=request,
            **_validated(EditMessageSerializer, request.data),
        ))

    def delete(
        self, request: Request, conversation_id: UUID, message_id: int,
    ) -> Response:
        return Response(services.delete_message(
            request.user, conversation_id, message_id, request=request,
        ))


class MessagingChangesView(APIView):
    def get(self, request: Request, conversation_id: UUID) -> Response:
        return Response(services.message_changes(
            request.user, conversation_id, request=request,
            **_validated(ChangesSerializer, request.query_params),
        ))


class MessagingReadView(APIView):
    def post(self, request: Request, conversation_id: UUID) -> Response:
        return Response(services.mark_read(
            request.user, conversation_id, request=request,
            **_validated(ReadSerializer, request.data),
        ))
