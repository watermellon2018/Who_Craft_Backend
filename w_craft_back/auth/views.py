import logging
import uuid

from django.contrib.auth import authenticate
from django.http import HttpResponse, JsonResponse

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from w_craft_back.auth.models import UserKey
from w_craft_back.auth.serializers import UserSerializer

logger = logging.getLogger(__name__)


class _AuthAnonThrottle(AnonRateThrottle):
    scope = 'auth'


class RegistrationView(APIView):
    throttle_classes = [_AuthAnonThrottle]

    def post(self, request):
        serializer = UserSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save(last_login=timezone.now())
            key = uuid.uuid4()
            UserKey.objects.create(user=user, key=key)
            return JsonResponse({'token': key}, safe=False,
                                status=status.HTTP_201_CREATED)

        logger.warning('Registration failed: validation error')
        return HttpResponse('Ошибка регистрации!', status=status.HTTP_400_BAD_REQUEST)


class LoginView(APIView):
    throttle_classes = [_AuthAnonThrottle]

    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')

        if not username or not password:
            return Response({'status': 'fail'}, status=status.HTTP_400_BAD_REQUEST)

        user = authenticate(username=username, password=password)
        if user is None:
            logger.info('Login failed for unknown user')
            return Response({'status': 'fail'}, status=status.HTTP_401_UNAUTHORIZED)

        user_key, _ = UserKey.objects.get_or_create(user=user)
        key = user_key.key
        return Response({
            'status': 200,
            'refresh': str(key),
            'access': str(key),
        })
