from django.urls import path

from w_craft_back.auth.views import RegistrationView, LoginView
from w_craft_back.movie.poster.views import generate_poster, edite_generative_poster

urlpatterns = [
    path('register/', RegistrationView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),

    path('poster/', generate_poster, name='project'),
    path('edit/', edite_generative_poster, name='edite_generative_poster'),
]
