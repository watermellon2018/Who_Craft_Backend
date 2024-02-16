from django.urls import path

from w_craft_back.views.views import GenerateImageView, \
    GenerateImageUndefinedView, \
    GenerateImg2ImgView
from w_craft_back.auth.views import RegistrationView, LoginView

urlpatterns = [
    path('register/', RegistrationView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),

    path('generate_image/',
         GenerateImageView.as_view(),
         name='generate_image'),
    path('generate_image_undefined/',
         GenerateImageUndefinedView.as_view(),
         name='generate_image_undefined'),
    path('generate_image_to_image/',
         GenerateImg2ImgView.as_view(),
         name='generate_image_to_image'),
]
