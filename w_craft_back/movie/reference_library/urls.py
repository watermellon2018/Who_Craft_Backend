"""Project-scoped Reference Library routes."""

from django.urls import path

from w_craft_back.movie.reference_library.views import (
    ReferenceArchiveView,
    ReferenceCapabilitiesView,
    ReferenceCollectionView,
    ReferenceDetailView,
    ReferenceGenerationJobCancellationView,
    ReferenceGenerationJobDetailView,
    ReferenceGenerationJobRetryView,
    ReferenceGenerationJobsView,
    ReferenceLinkOptionsView,
    ReferenceRestoreView,
    ReferenceVariantApplyView,
    ReferenceVersionsView,
    ReferenceVersionUploadView,
)


urlpatterns = [
    path("", ReferenceCollectionView.as_view(), name="project-references"),
    path(
        "capabilities/",
        ReferenceCapabilitiesView.as_view(),
        name="project-reference-capabilities",
    ),
    path(
        "link-options/",
        ReferenceLinkOptionsView.as_view(),
        name="project-reference-link-options",
    ),
    path(
        "<uuid:reference_id>/archive/",
        ReferenceArchiveView.as_view(),
        name="project-reference-archive",
    ),
    path(
        "<uuid:reference_id>/restore/",
        ReferenceRestoreView.as_view(),
        name="project-reference-restore",
    ),
    path(
        "<uuid:reference_id>/versions/",
        ReferenceVersionsView.as_view(),
        name="project-reference-versions",
    ),
    path(
        "<uuid:reference_id>/versions/upload/",
        ReferenceVersionUploadView.as_view(),
        name="project-reference-version-upload",
    ),
    path(
        "<uuid:reference_id>/generation-jobs/",
        ReferenceGenerationJobsView.as_view(),
        name="project-reference-generation-jobs",
    ),
    path(
        "<uuid:reference_id>/generation-jobs/<uuid:job_id>/cancellation-request/",
        ReferenceGenerationJobCancellationView.as_view(),
        name="project-reference-job-cancellation",
    ),
    path(
        "<uuid:reference_id>/generation-jobs/<uuid:job_id>/retry/",
        ReferenceGenerationJobRetryView.as_view(),
        name="project-reference-job-retry",
    ),
    path(
        (
            "<uuid:reference_id>/generation-jobs/<uuid:job_id>/"
            "variants/<uuid:variant_id>/apply/"
        ),
        ReferenceVariantApplyView.as_view(),
        name="project-reference-variant-apply",
    ),
    path(
        "<uuid:reference_id>/generation-jobs/<uuid:job_id>/",
        ReferenceGenerationJobDetailView.as_view(),
        name="project-reference-generation-job-detail",
    ),
    path(
        "<uuid:reference_id>/",
        ReferenceDetailView.as_view(),
        name="project-reference-detail",
    ),
]
