from django.urls import path

from w_craft_back.movie.project.views import ProjectView, \
    get_list_projects, \
    delete_project, select_project_info, update_info_project

urlpatterns = [
    path('create/', ProjectView.as_view(), name='project'),
    path('get-list-projects/', get_list_projects, name='get_list_projects'),
    path('delete-project-by-id/', delete_project, name='delete_project'),
    path('select-project-by-id/', select_project_info, name='select_project_info'),
    path('update-project-by-id/', update_info_project, name='update_info_project'),
]
