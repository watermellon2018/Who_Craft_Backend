import logging
import uuid

from django.contrib.auth import authenticate
from django.http import HttpResponse, JsonResponse

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.views import APIView

from w_craft_back.auth.models import UserKey
from w_craft_back.auth.serializers import UserSerializer, UserKeySerializer

logger = logging.getLogger(__name__)

class RegistrationView(APIView):
    def post(self, request):
        serializer = UserSerializer(data=request.data)
        logger.info(serializer)
        if serializer.is_valid():
            logger.info('valid')
            user = serializer.save(last_login=timezone.now())
            logger.info('save')
            key = uuid.uuid4()
            UserKey.objects.create(user=user, key=key)
            return JsonResponse({'token': key}, safe=False,
                                status=status.HTTP_201_CREATED)

        logger.error('Error registration!')
        return HttpResponse('Ошибка регистрации!', status=status.HTTP_400_BAD_REQUEST)


class LoginView(APIView):
    def get(self, request):
        username = request.GET.get('username')
        password = request.GET.get('password')
        logger.info(f'User {username} tried to log')

        user = authenticate(username=username, password=password)
        if user is not None:
            # Создаем токен
            refresh = RefreshToken.for_user(user)

            # Возвращаем токен в ответе
            return Response({
                'status': 'success',
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            })

        else:
            return Response({'status': 'fail'})
