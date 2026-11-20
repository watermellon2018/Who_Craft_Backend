"""Safe public video errors."""


class VideoError(Exception):
    def __init__(self, code: str, detail: str, status: int = 400,
                 retryable: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = status
        self.retryable = retryable

    def as_payload(self) -> dict:
        return {"code": self.code, "detail": self.detail,
                "retryable": self.retryable}


class VideoLeaseLost(VideoError):
    def __init__(self) -> None:
        super().__init__("VIDEO_LEASE_LOST", "Video lease was lost.", 409)
