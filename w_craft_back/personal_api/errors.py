"""Stable, secret-free errors shared by API and generation workers."""

from rest_framework.exceptions import APIException


class PersonalAPIError(APIException):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status
        self.http_status = status
        self.retryable = False
        super().__init__({'code': code, 'detail': message}, code=code)


def unavailable_connection() -> PersonalAPIError:
    return PersonalAPIError(
        'PERSONAL_API_CONNECTION_UNAVAILABLE',
        'The personal API connection is unavailable. Update your API settings.',
        403,
    )
