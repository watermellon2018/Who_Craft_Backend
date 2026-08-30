from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import NotificationReadSerializer, NotificationSerializer


def _authenticated_user(request):
    user = getattr(request, 'user', None)
    return user if user is not None and user.is_authenticated else None


class NotificationListView(APIView):
    def get(self, request):
        user = _authenticated_user(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        raw_limit = request.query_params.get('limit', '20')
        try:
            limit = min(max(int(raw_limit), 1), 100)
        except (TypeError, ValueError):
            return Response(
                {'detail': 'validation error', 'errors': {'limit': ['must be an integer']}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = Notification.objects.filter(recipient=user)
        return Response({
            'unread_count': queryset.filter(is_read=False).count(),
            'results': NotificationSerializer(queryset[:limit], many=True).data,
        })


class NotificationReadView(APIView):
    def post(self, request, notification_id: int):
        user = _authenticated_user(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = NotificationReadSerializer(data={'is_read': True})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        notification = Notification.objects.filter(
            pk=notification_id,
            recipient=user,
        ).first()
        if notification is None:
            return Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=['is_read', 'read_at'])
        return Response(NotificationSerializer(notification).data)


class NotificationReadAllView(APIView):
    def post(self, request):
        user = _authenticated_user(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        updated = Notification.objects.filter(
            recipient=user,
            is_read=False,
        ).update(is_read=True, read_at=timezone.now())
        return Response({'unread_count': 0, 'updated': updated})
