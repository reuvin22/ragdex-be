"""What can go wrong, as exceptions.

Raising one of these is how a service or repository reports a failure it chose
to surface. None of them knows what a response looks like — the status code
here is a fact about the failure, and ``views.errors`` is what renders it.
"""

from __future__ import annotations

from fastapi import status


class AppError(Exception):
    """A failure we chose to surface, with wording fit for a person."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        code: str = "bad_request",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class NotFoundError(AppError):
    def __init__(self, message: str = "Not found.") -> None:
        super().__init__(message, status_code=status.HTTP_404_NOT_FOUND, code="not_found")


class PermissionDeniedError(AppError):
    def __init__(self, message: str = "You cannot do that.") -> None:
        super().__init__(
            message, status_code=status.HTTP_403_FORBIDDEN, code="forbidden"
        )


class UpstreamError(AppError):
    """A dependency failed. The detail is logged, not returned."""

    def __init__(self, message: str = "An upstream service failed.") -> None:
        super().__init__(
            message, status_code=status.HTTP_502_BAD_GATEWAY, code="upstream_error"
        )
