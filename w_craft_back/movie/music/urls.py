"""Project-scoped Music Studio routes."""

from django.urls import path

from w_craft_back.movie.music.views import (
    MusicCapabilitiesView,
    MusicCollectionView,
    MusicGenerationJobCancellationView,
    MusicGenerationJobDetailView,
    MusicGenerationJobRetryView,
    MusicGenerationJobsView,
    MusicReferenceAssetDetailView,
    MusicReferenceAssetsView,
    MusicSceneOptionsView,
    MusicTrackArchiveView,
    MusicTrackAssignmentsView,
    MusicTrackDetailView,
    MusicVariantApplyView,
)


urlpatterns = [
    path("", MusicCollectionView.as_view(), name="project-music"),
    path(
        "capabilities/",
        MusicCapabilitiesView.as_view(),
        name="project-music-capabilities",
    ),
    path(
        "scene-options/",
        MusicSceneOptionsView.as_view(),
        name="project-music-scene-options",
    ),
    path(
        "reference-assets/",
        MusicReferenceAssetsView.as_view(),
        name="project-music-reference-assets",
    ),
    path(
        "reference-assets/<uuid:asset_id>/",
        MusicReferenceAssetDetailView.as_view(),
        name="project-music-reference-asset-detail",
    ),
    path(
        "generation-jobs/",
        MusicGenerationJobsView.as_view(),
        name="project-music-generation-jobs",
    ),
    path(
        "generation-jobs/<uuid:job_id>/cancellation-request/",
        MusicGenerationJobCancellationView.as_view(),
        name="project-music-generation-job-cancellation",
    ),
    path(
        "generation-jobs/<uuid:job_id>/retry/",
        MusicGenerationJobRetryView.as_view(),
        name="project-music-generation-job-retry",
    ),
    path(
        "generation-jobs/<uuid:job_id>/variants/<uuid:variant_id>/apply/",
        MusicVariantApplyView.as_view(),
        name="project-music-variant-apply",
    ),
    path(
        "generation-jobs/<uuid:job_id>/",
        MusicGenerationJobDetailView.as_view(),
        name="project-music-generation-job-detail",
    ),
    path(
        "<int:track_id>/archive/",
        MusicTrackArchiveView.as_view(),
        name="project-music-track-archive",
    ),
    path(
        "<int:track_id>/assignments/",
        MusicTrackAssignmentsView.as_view(),
        name="project-music-track-assignments",
    ),
    path(
        "<int:track_id>/",
        MusicTrackDetailView.as_view(),
        name="project-music-detail",
    ),
]
