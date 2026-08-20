"""Project-scoped Sound Effects routes for root integration."""

from django.urls import path

from w_craft_back.movie.sound_effects.views import (
    SoundEffectAssignmentsView,
    SoundEffectCapabilitiesView,
    SoundEffectCollectionView,
    SoundEffectGenerationJobCancellationView,
    SoundEffectGenerationJobDetailView,
    SoundEffectGenerationJobRetryView,
    SoundEffectGenerationJobsView,
    SoundEffectVariantApplyView,
)


urlpatterns = [
    path("", SoundEffectCollectionView.as_view(), name="project-sound-effects"),
    path(
        "capabilities/",
        SoundEffectCapabilitiesView.as_view(),
        name="project-sound-effect-capabilities",
    ),
    path(
        "assignments/",
        SoundEffectAssignmentsView.as_view(),
        name="project-sound-effect-assignments",
    ),
    path(
        "generation-jobs/",
        SoundEffectGenerationJobsView.as_view(),
        name="project-sound-effect-generation-jobs",
    ),
    path(
        "generation-jobs/<uuid:job_id>/cancellation-request/",
        SoundEffectGenerationJobCancellationView.as_view(),
        name="project-sound-effect-job-cancellation",
    ),
    path(
        "generation-jobs/<uuid:job_id>/retry/",
        SoundEffectGenerationJobRetryView.as_view(),
        name="project-sound-effect-job-retry",
    ),
    path(
        "generation-jobs/<uuid:job_id>/variants/<uuid:variant_id>/apply/",
        SoundEffectVariantApplyView.as_view(),
        name="project-sound-effect-variant-apply",
    ),
    path(
        "generation-jobs/<uuid:job_id>/",
        SoundEffectGenerationJobDetailView.as_view(),
        name="project-sound-effect-generation-job-detail",
    ),
]
