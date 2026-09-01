from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.profile.views import _get_user_from_request

from . import services


MAX_LIMIT = 50
DEFAULT_LIMIT = 20


class _PaginationError(Exception):
    def __init__(self, detail: str):
        self.detail = detail


def _parse_pagination(request):
    raw_limit = request.query_params.get('limit')
    raw_offset = request.query_params.get('offset')
    try:
        limit = int(raw_limit) if raw_limit not in (None, '') else DEFAULT_LIMIT
    except (TypeError, ValueError):
        raise _PaginationError('limit must be an integer')
    try:
        offset = int(raw_offset) if raw_offset not in (None, '') else 0
    except (TypeError, ValueError):
        raise _PaginationError('offset must be an integer')
    if limit < 1 or limit > MAX_LIMIT:
        raise _PaginationError(f'limit must be between 1 and {MAX_LIMIT}')
    if offset < 0:
        raise _PaginationError('offset must be >= 0')
    return limit, offset


def _pagination_or_400(request):
    """Return ``(limit, offset, None)`` or ``(None, None, Response400)``."""
    try:
        limit, offset = _parse_pagination(request)
    except _PaginationError as exc:
        return None, None, Response({'detail': exc.detail}, status=status.HTTP_400_BAD_REQUEST)
    return limit, offset, None


def _parse_user_id(raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise _PaginationError('invalid user_id')


def _state_to_payload(state: services.SubscriptionState) -> dict:
    return {
        'targetUserId': state.target_user_id,
        'isSubscribed': state.is_subscribed,
        'isFavorite': state.is_favorite,
        'notificationsEnabled': state.notifications_enabled,
    }


class MySubscriptionsView(APIView):
    """GET /api/subscriptions/ — current user's subscriptions."""

    def get(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        limit, offset, err = _pagination_or_400(request)
        if err:
            return err
        data = services.list_my_subscriptions(user, limit, offset)
        return Response(data)


class ChannelSearchView(APIView):
    """GET /api/channels/search/?q=... — trigram search across users with public_username."""

    def get(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        q = request.query_params.get('q') or ''
        limit, offset, err = _pagination_or_400(request)
        if err:
            return err
        data = services.search_channels(user, q, limit, offset)
        return Response(data)


class ChannelSubscribeView(APIView):
    """POST   /api/channels/<int:user_id>/subscribe/ — subscribe.
       DELETE /api/channels/<int:user_id>/subscribe/ — unsubscribe."""

    def post(self, request, user_id: int):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            state = services.subscribe(user, int(user_id))
        except services.AccountInactiveError as e:
            return Response({'detail': str(e)}, status=status.HTTP_401_UNAUTHORIZED)
        except services.SelfSubscriptionError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except services.TargetNotFoundError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'subscription': _state_to_payload(state)})

    def delete(self, request, user_id: int):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            state = services.unsubscribe(user, int(user_id))
        except services.AccountInactiveError as e:
            return Response({'detail': str(e)}, status=status.HTTP_401_UNAUTHORIZED)
        except services.SelfSubscriptionError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except services.SubscriptionNotFoundError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'subscription': _state_to_payload(state)})


class ChannelSubscriptionSettingsView(APIView):
    """PATCH /api/channels/<int:user_id>/subscription/ — update is_favorite / notifications_enabled."""

    def patch(self, request, user_id: int):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

        body = request.data if isinstance(request.data, dict) else {}
        is_favorite = body.get('isFavorite') if 'isFavorite' in body else body.get('is_favorite')
        notifications_enabled = (
            body.get('notificationsEnabled') if 'notificationsEnabled' in body
            else body.get('notifications_enabled')
        )
        if is_favorite is None and notifications_enabled is None:
            return Response(
                {'detail': 'no fields to update; expected isFavorite and/or notificationsEnabled'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            state = services.update_settings(
                user,
                int(user_id),
                is_favorite=is_favorite,
                notifications_enabled=notifications_enabled,
            )
        except services.AccountInactiveError as e:
            return Response({'detail': str(e)}, status=status.HTTP_401_UNAUTHORIZED)
        except services.SubscriptionNotFoundError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'subscription': _state_to_payload(state)})


class UserSubscribersView(APIView):
    """GET /api/users/<int:user_id>/subscribers/"""

    def get(self, request, user_id: int):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        limit, offset, err = _pagination_or_400(request)
        if err:
            return err
        data = services.list_subscribers(int(user_id), limit, offset)
        return Response(data)


class UserSubscriptionsView(APIView):
    """GET /api/users/<int:user_id>/subscriptions/"""

    def get(self, request, user_id: int):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        limit, offset, err = _pagination_or_400(request)
        if err:
            return err
        data = services.list_user_subscriptions(int(user_id), limit, offset)
        return Response(data)
