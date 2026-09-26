"""Signed upload and read slots for images.

This service never carries the bytes. It decides whether an upload is allowed,
what it may be called, and how large it may be — then hands back a URL that
permits exactly that and nothing else. A ten-megabyte screenshot goes from the
browser straight to R2.

Which makes this route the whole boundary. Everything it signs, it has already
checked: the type is on a short allowlist, the size is under the cap, and the
key it mints starts with the caller's own uid so the read side can tell whose
object it is without trusting anything the caller said.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import RedirectResponse

from app.controllers.deps import AppSettings, ReadUser, WriteUser
from app.core.errors import AppError, NotFoundError, PermissionDeniedError
from app.core.security import anonymous_rate_limit
from app.models.schemas.common import ErrorResponse
from app.models.schemas.upload import ReadUrl, UploadRequest, UploadSlot
from app.services import r2

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        501: {"model": ErrorResponse, "description": "Storage not configured"},
    },
)


@router.post("", response_model=UploadSlot, summary="Get somewhere to put an image")
def create_slot(
    user: WriteUser, payload: UploadRequest, settings: AppSettings
) -> UploadSlot:
    if payload.content_type not in r2.ALLOWED_TYPES:
        raise AppError(
            "That file type cannot be uploaded.",
            code="unsupported_type",
        )

    if payload.content_length > settings.max_upload_bytes:
        megabytes = settings.max_upload_bytes // (1024 * 1024)
        raise AppError(
            f"Images must be {megabytes}MB or smaller.",
            code="too_large",
        )

    key = r2.object_key(user.uid, payload.kind, payload.content_type)

    return UploadSlot(
        key=key,
        url=r2.presign_put(
            key=key,
            content_type=payload.content_type,
            content_length=payload.content_length,
            settings=settings,
        ),
        expires_in=settings.r2_url_ttl_seconds,
    )


@router.get("/url", response_model=ReadUrl, summary="Read one stored image")
def read_slot(
    user: ReadUser,
    settings: AppSettings,
    key: str = Query(max_length=200, description="The key returned when it was stored"),
) -> ReadUrl:
    """A short-lived URL for an object the caller owns.

    Ownership is read off the key's first segment rather than looked up. That
    is only sound because this service mints every key — a caller naming
    someone else's object has to name their uid, and this is where that is
    refused.
    """
    if not r2.readable(user.uid, key):
        raise PermissionDeniedError("That image is not yours.")

    return ReadUrl(
        url=r2.presign_get(key=key, settings=settings),
        expires_in=settings.r2_url_ttl_seconds,
    )


@router.get(
    "/public/{key:path}",
    summary="Fetch a publicly readable object",
    dependencies=[Depends(anonymous_rate_limit)],
    responses={
        302: {"description": "Redirect to a freshly signed URL"},
        404: {"model": ErrorResponse, "description": "Not a public object"},
    },
)
def public_object(
    settings: AppSettings,
    key: str = Path(min_length=3, max_length=256),
) -> RedirectResponse:
    """Serve an object that has no reader to authenticate.

    This exists for one thing: images a coach puts in an invitation email. A
    mail client opens that message days later carrying nobody's session, and
    Gmail fetches through its own proxy — so a signed URL handed out at send
    time would be dead long before anyone read it.

    The bytes still do not come through this service. It signs and redirects,
    so R2 serves the object and a free-tier instance carries a 302 rather than
    a megabyte.

    What keeps this from being an open door is ``r2.is_public``: only the
    ``university/`` prefix is servable, only coaches can write there, and
    everything in it is written to be posted to strangers. A key in any other
    folder is refused here exactly as if it did not exist — not "forbidden",
    because distinguishing the two would confirm which keys are real.
    """
    if not r2.is_public(key):
        raise NotFoundError("No such object.")

    # Unconfigured storage raises a 501 from inside _presign, which is the one
    # place that decision is made — repeating the check here would be a second
    # copy of it to keep in step.
    return RedirectResponse(
        url=r2.presign_get(key=key, settings=settings),
        status_code=302,
        # Long enough that a mail client's proxy fetches once, short enough
        # that removing an image from a template eventually takes effect.
        headers={"Cache-Control": "public, max-age=3600"},
    )
