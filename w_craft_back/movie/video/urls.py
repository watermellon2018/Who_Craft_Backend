from django.urls import path

from .views import JobView, JobsView, OptionsView, SelectView, ShotView, WorkspaceView

urlpatterns = [
    path("workspace/", WorkspaceView.as_view(), name="video-workspace"),
    path("options/", OptionsView.as_view(), name="video-options"),
    path("jobs/", JobsView.as_view(), name="video-jobs"),
    path("jobs/<uuid:job_id>/", JobView.as_view(), name="video-job"),
    path("shots/<int:shot_id>/", ShotView.as_view(), name="video-shot"),
    path("shots/<int:shot_id>/select/", SelectView.as_view(), name="video-select"),
]
