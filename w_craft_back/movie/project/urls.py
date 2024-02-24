from django.urls import path

from w_craft_back.movie.project.views import ProjectView, get_list_projects

urlpatterns = [
    path('create/', ProjectView.as_view(), name='project'),
    path('get-list-projects/', get_list_projects, name='get_list_projects'),
]
