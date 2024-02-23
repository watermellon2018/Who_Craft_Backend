from django.urls import path

from w_craft_back.movie.project.views import ProjectView

urlpatterns = [
    path('create/', ProjectView.as_view(), name='project'),
]
