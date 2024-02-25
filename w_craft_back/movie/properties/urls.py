from django.urls import path

from w_craft_back.movie.properties.views import GenreView

urlpatterns = [
    path('select/', GenreView.as_view(), name='genre'),
]
