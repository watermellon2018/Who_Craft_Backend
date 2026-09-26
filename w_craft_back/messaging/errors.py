"""Domain errors use the existing API's canonical error envelope."""

from rest_framework.exceptions import APIException


class MessagingError(APIException):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.status_code = status_code
        super().__init__({'error': {'code': code, 'message': message}})
