from django.urls import include, path

from w_craft_back.movie.project.dashboard_views import (
    ProjectAssetDetailView,
    ProjectAssetsView,
    ProjectCharactersView,
    ProjectDashboardView,
    ProjectDetailView,
    ProjectListCreateView,
    ProjectLocationsView,
    ProjectScenesView,
)
from w_craft_back.movie.poster.dashboard_views import (
    ProjectPosterEditView,
    ProjectPosterGenerateView,
    ProjectPosterJobDetailView,
    ProjectPosterJobCancellationView,
    ProjectPosterJobRetryView,
    ProjectPosterJobsView,
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
)
from w_craft_back.movie.reference_library.views import SceneReferencesView

urlpatterns = [
    # New dashboard / project API.
    path('', ProjectListCreateView.as_view(), name='project-list-create'),
    path('<int:project_id>/', ProjectDetailView.as_view(), name='project-detail'),
    path('<int:project_id>/dashboard/', ProjectDashboardView.as_view(), name='project-dashboard'),
    path('<int:project_id>/characters/', ProjectCharactersView.as_view(), name='project-characters'),
    path('<int:project_id>/scenes/', ProjectScenesView.as_view(), name='project-scenes'),
    path(
        '<int:project_id>/scenes/<int:scene_id>/references/',
        SceneReferencesView.as_view(),
        name='project-scene-references',
    ),
    path(
        '<int:project_id>/references/',
        include('w_craft_back.movie.reference_library.urls'),
    ),
    path(
        '<int:project_id>/music/',
        include('w_craft_back.movie.music.urls'),
    ),
    path(
        '<int:project_id>/sound-effects/',
        include('w_craft_back.movie.sound_effects.urls'),
    ),
    path('<int:project_id>/locations/', ProjectLocationsView.as_view(), name='project-locations'),
    path('<int:project_id>/assets/', ProjectAssetsView.as_view(), name='project-assets'),
    path(
        '<int:project_id>/assets/<int:asset_id>/',
        ProjectAssetDetailView.as_view(),
        name='project-asset-detail',
    ),

    # Concurrent-edit-guarded entity detail endpoints (GET/PATCH with version).
    path('<int:project_id>/scenes/<int:scene_id>/', SceneDetailView.as_view(), name='project-scene-detail'),
    path('<int:project_id>/locations/<int:location_id>/', LocationDetailView.as_view(), name='project-location-detail'),

    # Team collaboration.
    path('<int:project_id>/team/', ProjectTeamView.as_view(), name='project-team'),
    path('<int:project_id>/team/members/', ProjectMembersView.as_view(), name='project-team-members'),
    path('<int:project_id>/team/members/<int:member_id>/', ProjectMemberDetailView.as_view(), name='project-team-member-detail'),
    path('<int:project_id>/team/leave/', ProjectLeaveView.as_view(), name='project-team-leave'),
    path('<int:project_id>/team/transfer-ownership/', ProjectTransferOwnershipView.as_view(), name='project-team-transfer'),
    path('<int:project_id>/team/invitations/', ProjectInvitationsView.as_view(), name='project-team-invitations'),
    path('<int:project_id>/team/invitations/<int:invitation_id>/', ProjectInvitationCancelView.as_view(), name='project-team-invitation-cancel'),

    # Poster generation page (/projects/:projectId/poster).
    path('<int:project_id>/poster/', ProjectPosterView.as_view(), name='project-poster'),
    path('<int:project_id>/poster/generate/', ProjectPosterGenerateView.as_view(), name='project-poster-generate'),
    path(
        '<int:project_id>/poster/edit/',
        ProjectPosterEditView.as_view(),
        name='project-poster-edit',
    ),
    path(
        '<int:project_id>/poster/jobs/',
        ProjectPosterJobsView.as_view(),
        name='project-poster-jobs',
    ),
    path(
        '<int:project_id>/poster/jobs/<int:job_id>/retry/',
        ProjectPosterJobRetryView.as_view(),
        name='project-poster-job-retry',
    ),
    path(
        '<int:project_id>/poster/jobs/<int:job_id>/cancellation-request/',
        ProjectPosterJobCancellationView.as_view(),
        name='project-poster-job-cancellation',
    ),
    path('<int:project_id>/poster/jobs/<int:job_id>/', ProjectPosterJobDetailView.as_view(), name='project-poster-job'),
    path('<int:project_id>/poster/variants/', ProjectPosterVariantsView.as_view(), name='project-poster-variants'),
    path('<int:project_id>/poster/variants/<int:variant_id>/', ProjectPosterVariantDeleteView.as_view(), name='project-poster-variant-detail'),
    path('<int:project_id>/poster/select/', ProjectPosterSelectView.as_view(), name='project-poster-select'),
]
