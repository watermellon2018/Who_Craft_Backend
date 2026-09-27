"""Authenticated endpoints never serialize a provider secret."""

from uuid import UUID

from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import (
    CreateConnectionSerializer,
    PreferenceSerializer,
    StrictSerializer,
    UpdateConnectionSerializer,
)


@sensitive_variables('data', 'serializer')
def _validated(serializer_type: type[StrictSerializer], data: object) -> dict:
    serializer = serializer_type(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class PersonalAPISettingsView(APIView):
    def get(self, request: Request) -> Response:
        response = Response(services.get_settings(request.user))
        response['Cache-Control'] = 'no-store'
        return response


@method_decorator(sensitive_post_parameters('apiKey'), name='dispatch')
class PersonalAPIConnectionsView(APIView):
    def post(self, request: Request) -> Response:
        return Response(services.create_connection(
            request.user, **_validated(CreateConnectionSerializer, request.data),
        ), status=201)


@method_decorator(sensitive_post_parameters('apiKey'), name='dispatch')
class PersonalAPIConnectionView(APIView):
    def patch(self, request: Request, connection_id: UUID) -> Response:
        return Response(services.update_connection(
            request.user, connection_id,
            **_validated(UpdateConnectionSerializer, request.data),
        ))

    def delete(self, request: Request, connection_id: UUID) -> Response:
        services.revoke_connection(request.user, connection_id)
        return Response(status=204)


class PersonalAPIPreferenceView(APIView):
    def put(self, request: Request, modality: str) -> Response:
        return Response(services.save_preference(
            request.user, modality,
            **_validated(PreferenceSerializer, request.data),
        ))
