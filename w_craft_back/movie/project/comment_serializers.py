from rest_framework import serializers

from w_craft_back.movie.project.comment_models import VideoShotComment


class VideoShotCommentCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=4000, trim_whitespace=True)

    def validate_body(self, value):
        if not value:
            raise serializers.ValidationError('comment must not be empty')
        return value


class VideoShotCommentSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()

    class Meta:
        model = VideoShotComment
        fields = ['id', 'author', 'body', 'created_at', 'updated_at']

    @staticmethod
    def get_author(comment):
        profile = getattr(comment.author, 'profile', None)
        return {
            'id': comment.author_id,
            'username': (
                profile.effective_username if profile is not None
                else comment.author.username
            ),
        }
