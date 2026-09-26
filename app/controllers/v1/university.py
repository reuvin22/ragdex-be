"""Coaching: invitations, rosters, and one student's journal.

This is the first family in the service where one account reads another's
data, so it is worth being exact about what permits it.

Every route still takes its identity from the session. Two of them name
another person — ``/students/{uid}`` — and that uid is *the other participant*,
the way ``/chat/threads/{uid}`` is. It is not who the caller is; it is who the
caller is asking about, and the answer is refused unless
``enrolment.is_active`` says there is an accepted enrolment between the two.
A pending invitation is not enough, and a declined one certainly is not.

Nothing here lets a caller discover an account they could not already reach.
An invitation goes out to an email address, which is the same thing contact
search already accepts, and the roster only ever returns people who said yes.
"""

from __future__ import annotations

from fastapi import APIRouter, Path, status

from app.controllers.deps import ReadUser, StandardRateLimit, WriteUser
from app.core.errors import AppError, NotFoundError, PermissionDeniedError
from app.models.repositories import directory as directory_repo
from app.models.repositories import enrolment as enrolment_repo
from app.models.repositories import profiles as profiles_repo
from app.models.repositories import trades as trades_repo
from app.models.schemas.common import ErrorResponse, Message
from app.models.schemas.trade import TradePage
from app.models.schemas.university import (
    InvitationList,
    InviteRequest,
    MyCoach,
    SentInviteList,
    StudentList,
)
from app.services import university as service

router = APIRouter(
    prefix="/university",
    tags=["university"],
    dependencies=[StandardRateLimit],
    responses={
        401: {"model": ErrorResponse, "description": "Not signed in"},
        429: {"model": ErrorResponse, "description": "Rate limited"},
    },
)

# Firebase uids are 28 alphanumerics. Constraining the path keeps anything
# stranger from reaching the client library at all.
OtherUid = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def _require_coach(uid: str) -> None:
    """Only a coach account may hold a roster.

    Read from the stored profile rather than taken from the request, so
    switching account type is a profile write somebody made deliberately and
    not a field on this call.
    """
    profile = profiles_repo.get_profile(uid)
    if profile.account_type != "coach":
        raise PermissionDeniedError(
            "Your account type is not Coach. Change it on your profile first."
        )


@router.get("/students", response_model=StudentList, summary="Your students")
async def list_students(user: ReadUser) -> StudentList:
    """The roster, with each student's figures derived from their journal."""
    return StudentList(students=service.roster(user.uid))


@router.get("/coach", response_model=MyCoach, summary="Who is teaching you")
async def my_coach(user: ReadUser) -> MyCoach:
    """Answers with a null coach rather than a 404 — having none is ordinary."""
    return MyCoach(coach=service.coach_of(user.uid))


@router.post(
    "/invites",
    response_model=Message,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a trader to your programme",
    responses={
        403: {"model": ErrorResponse, "description": "Not a coach account"},
        404: {"model": ErrorResponse, "description": "No account at that address"},
    },
)
async def invite(user: WriteUser, payload: InviteRequest) -> Message:
    """Invite by email. The invitation is pending until they accept.

    Answers 404 for an address with no account, which does tell the caller
    whether an address is registered. That is the same thing contact search
    already tells them, and an invite that silently went nowhere would be
    worse: a coach would sit waiting on a student who was never asked.
    """
    _require_coach(user.uid)

    found = directory_repo.find_by_email(payload.email)
    if found is None:
        raise NotFoundError("No account uses that address.")

    if found.uid == user.uid:
        raise AppError("You cannot invite yourself.", code="invalid")

    enrolment_repo.invite(user.uid, found.uid, note=payload.note)
    return Message(message=f"Invitation sent to {found.email}.")


@router.get("/invites", response_model=SentInviteList, summary="Invitations you sent")
async def sent_invites(user: ReadUser) -> SentInviteList:
    return SentInviteList(invites=service.sent(user.uid))


@router.get(
    "/invitations",
    response_model=InvitationList,
    summary="Invitations waiting for you",
)
async def invitations(user: ReadUser) -> InvitationList:
    return InvitationList(invitations=service.pending(user.uid))


@router.post(
    "/invitations/{uid}/accept",
    response_model=Message,
    summary="Accept an invitation",
    responses={404: {"model": ErrorResponse, "description": "Nothing pending"}},
)
async def accept(user: WriteUser, uid: str = OtherUid) -> Message:
    """`uid` names the coach who invited you, not you.

    The row is addressed as ``<coach>_<caller>``, so a caller can only ever
    answer an invitation that was sent to them — there is no id here that
    could name somebody else's.
    """
    if enrolment_repo.coaches_of(user.uid):
        raise AppError(
            "You already have a coach. Leave that programme before joining another.",
            code="already_enrolled",
        )

    if enrolment_repo.respond(uid, user.uid, accept=True) is None:
        raise NotFoundError("That invitation is no longer waiting.")

    return Message(message="You have joined the programme.")


@router.post(
    "/invitations/{uid}/decline",
    response_model=Message,
    summary="Decline an invitation",
    responses={404: {"model": ErrorResponse, "description": "Nothing pending"}},
)
async def decline(user: WriteUser, uid: str = OtherUid) -> Message:
    if enrolment_repo.respond(uid, user.uid, accept=False) is None:
        raise NotFoundError("That invitation is no longer waiting.")

    return Message(message="Invitation declined.")


@router.delete(
    "/students/{uid}",
    response_model=Message,
    summary="End an enrolment",
    responses={404: {"model": ErrorResponse, "description": "Not your student"}},
)
async def remove_student(user: WriteUser, uid: str = OtherUid) -> Message:
    """Either side may end it, so this is addressed both ways round.

    The grant goes with the row: once it is gone the coach's next read of that
    student's journal is refused like anybody else's.
    """
    ended = enrolment_repo.remove(user.uid, uid) or enrolment_repo.remove(uid, user.uid)
    if not ended:
        raise NotFoundError("No enrolment between you two.")

    return Message(message="Enrolment ended.")


@router.get(
    "/students/{uid}/journal",
    response_model=TradePage,
    summary="One student's journal",
    responses={403: {"model": ErrorResponse, "description": "Not your student"}},
)
async def student_journal(user: ReadUser, uid: str = OtherUid) -> TradePage:
    """A student's trades, for the coach they accepted.

    The only read in this service that crosses accounts, and the check is the
    first thing it does. Read-only by design: there is no write anywhere in
    this family that touches somebody else's journal, because a coach
    correcting a student's record would destroy the thing being coached.
    """
    if not enrolment_repo.is_active(user.uid, uid):
        raise PermissionDeniedError("They are not one of your students.")

    items, next_cursor = trades_repo.list_trades(uid, limit=trades_repo.MAX_PAGE_SIZE)
    return TradePage(items=items, next_cursor=next_cursor)
