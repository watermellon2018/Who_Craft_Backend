"""Authenticated project video endpoints; generation is always asynchronous."""
from functools import wraps

from rest_framework.exceptions import NotAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.credits.services import CreditServiceError
from w_craft_back.services.video_generation.contracts import VideoProviderError

from . import services
from .errors import VideoError


def handle_errors(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except VideoError as exc:
            return Response(exc.as_payload(), status=exc.http_status)
        except PersonalAPIError as exc:
            return Response(
                {"code": exc.code, "detail": exc.message, "retryable": False},
                status=exc.http_status)
        except CreditServiceError as exc:
            return Response({"code": exc.code, "detail": str(exc), "retryable": False},
                            status=exc.http_status)
        except VideoProviderError as exc:
            return Response(
                {"code": exc.code, "detail": str(exc), "retryable": exc.retryable},
                status=503 if exc.retryable else 400)
    return wrapped


class VideoView(APIView):
    @staticmethod
    def actor(request):
        if not request.user or not request.user.is_authenticated:
            raise NotAuthenticated("X-User-Token header is required.")
        return request.user


class WorkspaceView(VideoView):
    @handle_errors
    def get(self, request, project_id):
        return Response(services.workspace(self.actor(request), project_id, request))


class OptionsView(VideoView):
    @handle_errors
    def post(self, request, project_id):
        return Response(services.options(self.actor(request), project_id, request.data))


class JobsView(VideoView):
    @handle_errors
    def get(self, request, project_id):
        return Response(services.list_jobs(
            self.actor(request), project_id,
            request.query_params.get("sceneId"), request))

    @handle_errors
    def post(self, request, project_id):
        return Response(services.create_job(
            self.actor(request), project_id, request.data, request), status=202)


class JobView(VideoView):
    @handle_errors
    def get(self, request, project_id, job_id):
        return Response(services.retrieve_job(
            self.actor(request), project_id, job_id, request))


class ShotView(VideoView):
    @handle_errors
    def patch(self, request, project_id, shot_id):
        return Response(services.update_shot(
            self.actor(request), project_id, shot_id, request.data))


class SelectView(VideoView):
    @handle_errors
    def post(self, request, project_id, shot_id):
        return Response(services.update_shot(
            self.actor(request), project_id, shot_id, request.data, select=True))
