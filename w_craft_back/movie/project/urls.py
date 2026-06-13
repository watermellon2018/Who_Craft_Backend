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
from w_craft_back.movie.poster.dashboard_views import (
    ProjectPosterGenerateView,
    ProjectPosterJobDetailView,
    ProjectPosterSelectView,
    ProjectPosterVariantDeleteView,
    ProjectPosterVariantsView,
    ProjectPosterView,
)
from w_craft_back.movie.project.team_views import (
    ProjectInvitationCancelView,
    ProjectInvitationsView,
    ProjectLeaveView,
    ProjectMemberDetailView,
    ProjectMembersView,
    ProjectTeamView,
    ProjectTransferOwnershipView,
)
from w_craft_back.movie.project.scene_views import (
    SceneDetailView,
    LocationDetailView,
    MusicTrackDetailView,
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

    # Concurrent-edit-guarded entity detail endpoints (GET/PATCH with version).
    path('<int:project_id>/scenes/<int:scene_id>/', SceneDetailView.as_view(), name='project-scene-detail'),
    path('<int:project_id>/locations/<int:location_id>/', LocationDetailView.as_view(), name='project-location-detail'),
    path('<int:project_id>/music/<int:track_id>/', MusicTrackDetailView.as_view(), name='project-music-detail'),

    # Team collaboration.
    path('<int:project_id>/team/', ProjectTeamView.as_view(), name='project-team'),
    path('<int:project_id>/team/members/', ProjectMembersView.as_view(), name='project-team-members'),
    path('<int:project_id>/team/members/<int:member_id>/', ProjectMemberDetailView.as_view(), name='project-team-member-detail'),
    path('<int:project_id>/team/leave/', ProjectLeaveView.as_view(), name='project-team-leave'),
    path('<int:project_id>/team/transfer-ownership/', ProjectTransferOwnershipView.as_view(), name='project-team-transfer'),
    path('<int:project_id>/team/invitations/', ProjectInvitationsView.as_view(), name='project-team-invitations'),
    path('<int:project_id>/team/invitations/<int:invitation_id>/', ProjectInvitationCancelView.as_view(), name='project-team-invitation-cancel'),

    # Poster generation page (/create-project/gen-poster).
    path('<int:project_id>/poster/', ProjectPosterView.as_view(), name='project-poster'),
    path('<int:project_id>/poster/generate/', ProjectPosterGenerateView.as_view(), name='project-poster-generate'),
    path('<int:project_id>/poster/jobs/<int:job_id>/', ProjectPosterJobDetailView.as_view(), name='project-poster-job'),
    path('<int:project_id>/poster/variants/', ProjectPosterVariantsView.as_view(), name='project-poster-variants'),
    path('<int:project_id>/poster/variants/<int:variant_id>/', ProjectPosterVariantDeleteView.as_view(), name='project-poster-variant-detail'),
    path('<int:project_id>/poster/select/', ProjectPosterSelectView.as_view(), name='project-poster-select'),
]
