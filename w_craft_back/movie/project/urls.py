from django.urls import path

from w_craft_back.movie.project.views import (
    ProjectView,
    delete_project,
    get_list_projects,
    select_project_info,
    update_info_project,
)
from w_craft_back.movie.project.dashboard_views import (
    ProjectAssetsView,
    ProjectCharactersView,
    ProjectDashboardView,
    ProjectDetailView,
    ProjectGenerationJobsView,
    ProjectListCreateView,
    ProjectLocationsView,
    ProjectMusicView,
    ProjectScenesView,
)

urlpatterns = [
    # Legacy endpoints (kept for back-compat).
    path('create/', ProjectView.as_view(), name='project'),
    path('get-list-projects/', get_list_projects, name='get_list_projects'),
    path('delete-project-by-id/', delete_project, name='delete_project'),
    path('select-project-by-id/', select_project_info, name='select_project_info'),
    path('update-project-by-id/', update_info_project, name='update_info_project'),

    # New dashboard / project API.
    path('', ProjectListCreateView.as_view(), name='project-list-create'),
    path('<int:project_id>/', ProjectDetailView.as_view(), name='project-detail'),
    path('<int:project_id>/dashboard/', ProjectDashboardView.as_view(), name='project-dashboard'),
    path('<int:project_id>/characters/', ProjectCharactersView.as_view(), name='project-characters'),
    path('<int:project_id>/scenes/', ProjectScenesView.as_view(), name='project-scenes'),
    path('<int:project_id>/music/', ProjectMusicView.as_view(), name='project-music'),
    path('<int:project_id>/locations/', ProjectLocationsView.as_view(), name='project-locations'),
    path('<int:project_id>/assets/', ProjectAssetsView.as_view(), name='project-assets'),
    path('<int:project_id>/generation-jobs/', ProjectGenerationJobsView.as_view(), name='project-generation-jobs'),
]
