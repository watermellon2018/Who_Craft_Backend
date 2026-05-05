class CharacterStudioError(Exception):
    error_code = "CHARACTER_STUDIO_ERROR"
    status_code = 400
    message = "Character Studio request failed."

    def __init__(self, message=None, error_code=None, status_code=None):
        super().__init__(message or self.message)
        self.message = message or self.message
        if error_code:
            self.error_code = error_code
        if status_code:
            self.status_code = status_code


class PermissionDeniedError(CharacterStudioError):
    error_code = "PERMISSION_DENIED"
    status_code = 403
    message = "You do not have permission to access this project."


class NotFoundError(CharacterStudioError):
    error_code = "NOT_FOUND"
    status_code = 404
    message = "Requested object was not found."


class ValidationError(CharacterStudioError):
    error_code = "VALIDATION_ERROR"
    status_code = 400
    message = "Invalid request."


class SafetyRejectedError(CharacterStudioError):
    error_code = "SAFETY_REJECTED"
    status_code = 400
    message = "This character request violates content safety rules."


class IdentityLockedError(CharacterStudioError):
    error_code = "IDENTITY_LOCKED"
    status_code = 409
    message = "This change may alter the locked identity. Create a new version or unlock identity."

