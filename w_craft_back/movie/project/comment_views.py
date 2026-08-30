from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.comment_models import VideoShotComment
from w_craft_back.movie.project.comment_serializers import (
    VideoShotCommentCreateSerializer,
    VideoShotCommentSerializer,
)
from w_craft_back.movie.project.comment_service import (
    CommentAccessDenied,
    comment_decision,
    create_comment,
)
from w_craft_back.movie.project.dashboard_models import VideoShot
from w_craft_back.movie.project.models import Project
from w_craft_back.notifications.throttles import NotificationEventThrottle


class VideoShotCommentsView(APIView):
    def get_throttles(self):
        if self.request.method == 'POST':
            return [NotificationEventThrottle()]
        return super().get_throttles()

    @staticmethod
    def _resolve(request, project_id: int, shot_id: int):
        project = Project.objects.filter(pk=project_id).first()
        if project is None:
            return None, Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        if not policy.can_view(request.user, project):
            return None, Response({'detail': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        shot = VideoShot.objects.select_related('project__owner').filter(
            pk=shot_id,
            project=project,
        ).first()
        if shot is None:
            return None, Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return shot, None

    def get(self, request, project_id: int, shot_id: int):
        shot, error = self._resolve(request, project_id, shot_id)
        if error is not None:
            return error
        decision = comment_decision(request.user, shot)
        comments = VideoShotComment.objects.filter(shot=shot).select_related(
            'author',
            'author__profile',
        )
        return Response({
            'can_comment': decision.allowed,
            'comment_block_reason': decision.reason or None,
            'comments': VideoShotCommentSerializer(comments, many=True).data,
        })

    def post(self, request, project_id: int, shot_id: int):
        shot, error = self._resolve(request, project_id, shot_id)
        if error is not None:
            return error
        serializer = VideoShotCommentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            comment = create_comment(
                user=request.user,
                shot=shot,
                body=serializer.validated_data['body'],
            )
        except CommentAccessDenied as exc:
            return Response(
                {'detail': 'commenting is not allowed', 'code': exc.code},
                status=status.HTTP_403_FORBIDDEN,
            )
        comment = VideoShotComment.objects.select_related(
            'author',
            'author__profile',
        ).get(pk=comment.pk)
        return Response(
            VideoShotCommentSerializer(comment).data,
            status=status.HTTP_201_CREATED,
        )
