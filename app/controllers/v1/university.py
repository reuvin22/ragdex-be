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

from fastapi import APIRouter, Path, Response, status

from app.controllers.deps import AppSettings, ReadUser, StandardRateLimit, WriteUser
from app.core.errors import AppError, NotFoundError, PermissionDeniedError
from app.models.repositories import directory as directory_repo
from app.models.repositories import documents as documents_repo
from app.models.repositories import enrolment as enrolment_repo
from app.models.repositories import profiles as profiles_repo
from app.models.repositories import trades as trades_repo
from app.models.repositories import university_settings as settings_repo
from app.models.schemas.common import ErrorResponse, Message
from app.models.schemas.documents import (
    DocumentList,
    DocumentWrite,
    Submission,
    SubmissionList,
    SubmissionWrite,
    UniversityDocument,
)
from app.models.schemas.trade import TradePage
from app.models.schemas.university import (
    ApplicationList,
    Inbox,
    Intake,
    InvitationList,
    InviteRequest,
    MyCoach,
    SentInviteList,
    StudentList,
    UniversitySettings,
)
from app.services import email as email_service
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
    summary="Invite a trader to your program",
    responses={
        403: {"model": ErrorResponse, "description": "Not a coach account"},
        404: {"model": ErrorResponse, "description": "No account at that address"},
    },
)
async def invite(
    user: WriteUser, payload: InviteRequest, app_settings: AppSettings
) -> Message:
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

    # The row is written first and the mail sent second, deliberately. A send
    # that fails leaves an invitation the student can still find on their own
    # screen; a row that failed to write would leave a mail pointing at
    # nothing. So the durable half goes first and the delivery is best effort.
    try:
        await email_service.send_invitation(
            email=found.email,
            student_name=found.display_name,
            coach_name=user.name or (user.email or "Your coach"),
            note=payload.note,
            settings_record=settings_repo.get_settings(user.uid),
            settings=app_settings,
        )
    except AppError as exc:
        # Reported rather than swallowed: a coach who thinks a mail went out
        # will sit waiting on a reply to something nobody received.
        return Message(
            message=(
                f"{found.email} can now accept from their My University screen, "
                f"but the email did not go out: {exc.message}"
            )
        )

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
            "You already have a coach. Leave that program before joining another.",
            code="already_enrolled",
        )

    # A coach with an intake form asked a question, and this endpoint would
    # otherwise be a way round it — straight from invited to enrolled, with
    # neither the answers nor the coach's decision. The form is the route in.
    if documents_repo.intake_for(uid) is not None:
        raise AppError(
            "Answer this coach's form first — they review it before you join.",
            code="form_required",
        )

    # Checked rather than written. `respond` used to be called here and it
    # writes `active` — so for the moment between that write and `settle`
    # correcting it, somebody with documents outstanding was fully enrolled
    # and on the coach's roster. If `settle` then failed they stayed there.
    # One write, made by whichever function knows the right answer.
    row = enrolment_repo.get(uid, user.uid)
    if row is None or row.status != "pending":
        raise NotFoundError("That invitation is no longer waiting.")

    return Message(message=service.settle(uid, user.uid))


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


@router.get(
    "/settings",
    response_model=UniversitySettings,
    summary="Your program and its invitation email",
)
async def read_settings(user: ReadUser) -> UniversitySettings:
    """Empty rather than 404 for a coach who has never opened this."""
    return settings_repo.get_settings(user.uid)


@router.put(
    "/settings",
    response_model=UniversitySettings,
    summary="Save your program and invitation email",
    responses={403: {"model": ErrorResponse, "description": "Not a coach account"}},
)
async def write_settings(
    user: WriteUser, payload: UniversitySettings
) -> UniversitySettings:
    """The three markup sections are sanitised before they are stored.

    What comes back is what was kept, not what was sent — so an editor that
    round-trips this sees exactly what a recipient will, and a tag that was
    dropped is visibly dropped rather than silently dropped at send time.
    """
    _require_coach(user.uid)
    return settings_repo.save_settings(user.uid, payload)


# --------------------------------------------------------------- documents
#
# Agreements to sign and forms to answer. The access rule is the same one the
# rest of this family uses and is worth saying once, here:
#
#   * a coach may touch a document whose ``coachUid`` is their own uid;
#   * a student may read a *published* document belonging to a coach they have
#     an active enrolment with, and may submit to it;
#   * nobody else can see that either exists.
#
# There is no route that takes a coach uid. A student's documents are found
# through their own enrolment, which is the same reason `/chat/threads/{uid}`
# cannot address a conversation the caller is not in.


DocumentId = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _own_document(uid: str, document_id: str) -> UniversityDocument:
    """A document the caller wrote, or a 404.

    404 and not 403: telling a stranger that a document exists but is not
    theirs is telling them it exists.
    """
    document = documents_repo.get(document_id)
    if document.coach_uid != uid:
        raise NotFoundError("No such document.")
    return document


def _readable_document(uid: str, document_id: str) -> UniversityDocument:
    """A document the caller may read.

    Three ways in, and the third is the one worth stating. An intake form has
    to be readable by somebody who is *not* a student yet — that is its whole
    job — so an invitation that has been sent or applied to is enough for that
    one document. Every other document still needs an accepted enrolment.
    """
    document = documents_repo.get(document_id)

    if document.coach_uid == uid:
        return document

    if not document.published:
        raise NotFoundError("No such document.")

    row = enrolment_repo.get(document.coach_uid, uid)

    # `documents` as well as `active`: somebody part-way through signing has
    # to be able to read what they are signing, and that is the entire state.
    if row is not None and row.status in ("active", "documents"):
        return document

    if document.is_intake and row is not None and row.status in ("pending", "applied"):
        return document

    raise NotFoundError("No such document.")


@router.get(
    "/documents",
    response_model=DocumentList,
    summary="Agreements and forms",
)
async def list_documents(user: ReadUser) -> DocumentList:
    """Yours if you are a coach; your coach's published ones if you are not.

    A coach sees drafts and a completion count. A student sees only what has
    been published to them, and only whether *they themselves* have completed
    it — who else has signed what is nobody else's business.
    """
    return DocumentList(documents=service.documents_for(user.uid))


@router.post(
    "/documents",
    response_model=UniversityDocument,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agreement or a form",
    responses={403: {"model": ErrorResponse, "description": "Not a coach account"}},
)
async def create_document(user: WriteUser, payload: DocumentWrite) -> UniversityDocument:
    _require_coach(user.uid)
    created = documents_repo.create(user.uid, payload)

    if created.is_intake:
        documents_repo.clear_intake(user.uid, except_id=created.id)

    return created


@router.put(
    "/documents/{document_id}",
    response_model=UniversityDocument,
    summary="Edit one",
    responses={404: {"model": ErrorResponse}},
)
async def update_document(
    user: WriteUser, payload: DocumentWrite, document_id: str = DocumentId
) -> UniversityDocument:
    """Question ids survive an edit, so answers already collected still line up.

    Editing an agreement that has signatures does not invalidate them — but it
    does make them stop matching, because a signature records a digest of what
    was signed. A coach comparing the two can see that the text moved.
    """
    _own_document(user.uid, document_id)
    saved = documents_repo.update(user.uid, document_id, payload)

    if saved.is_intake:
        documents_repo.clear_intake(user.uid, except_id=saved.id)

    return saved


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one, and everything sent back to it",
    responses={404: {"model": ErrorResponse}},
)
async def delete_document(user: WriteUser, document_id: str = DocumentId) -> Response:
    _own_document(user.uid, document_id)
    documents_repo.delete(document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/documents/{document_id}",
    response_model=UniversityDocument,
    summary="Read one",
    responses={404: {"model": ErrorResponse}},
)
async def read_document(
    user: ReadUser, document_id: str = DocumentId
) -> UniversityDocument:
    document = _readable_document(user.uid, document_id)

    if document.coach_uid == user.uid:
        document.submission_count = len(documents_repo.submitted_uids(document_id))
    else:
        existing = documents_repo.submission_for(document_id, user.uid)
        document.submitted_at = existing.submitted_at if existing else None

    return document


@router.post(
    "/documents/{document_id}/submit",
    response_model=Submission,
    status_code=status.HTTP_201_CREATED,
    summary="Sign it, or answer it",
    responses={
        400: {"model": ErrorResponse, "description": "Something required is missing"},
        404: {"model": ErrorResponse},
    },
)
async def submit_document(
    user: WriteUser, payload: SubmissionWrite, document_id: str = DocumentId
) -> Submission:
    """The student's own submission, and only their own.

    The uid is the session's; the document is the path's. There is nothing in
    the body that says who this is from, which is what makes the record worth
    keeping — a signature a client could address to somebody else is not
    evidence of anything.
    """
    document = _readable_document(user.uid, document_id)

    if document.coach_uid == user.uid:
        raise AppError("You cannot submit to your own document.", code="invalid")

    service.check_complete(document, payload)

    submission = documents_repo.submit(
        document,
        user.uid,
        signed_name=payload.signed_name,
        answers=payload.answers,
    )

    # Answering the intake form *is* the application. Done after the answers
    # are stored, so a coach never sees a pending application with nothing
    # behind it to read.
    if document.is_intake:
        enrolment_repo.apply(document.coach_uid, user.uid)
    else:
        # Signing the last outstanding document is what enrols somebody. Done
        # here rather than on a later read, so the moment it is true is the
        # moment it takes effect.
        service.complete_if_signed(document.coach_uid, user.uid)

    return submission


@router.get(
    "/documents/{document_id}/submissions",
    response_model=SubmissionList,
    summary="Who has signed or answered",
    responses={404: {"model": ErrorResponse}},
)
async def list_submissions(
    user: ReadUser, document_id: str = DocumentId
) -> SubmissionList:
    """The author's view. A student asking gets the same 404 as a stranger."""
    _own_document(user.uid, document_id)
    return service.submissions(user.uid, document_id)


# ------------------------------------------------------- joining a program


@router.get(
    "/intake",
    response_model=Intake,
    summary="What is waiting for you, and the form to fill in",
)
async def intake(user: ReadUser) -> Intake:
    """Where the invitation email lands.

    Resolves everything from the session: which invitation is open, how far it
    has got, and whether that coach set an intake form. No uid in the URL, so
    the link in an email is the same for everybody and cannot be edited into
    somebody else's invitation.
    """
    return service.intake_for(user.uid)


@router.get(
    "/applications",
    response_model=ApplicationList,
    summary="Students waiting on your decision",
)
async def applications(user: ReadUser) -> ApplicationList:
    return ApplicationList(applications=service.applications(user.uid))


@router.post(
    "/applications/{uid}/approve",
    response_model=Message,
    summary="Approve an application",
    responses={404: {"model": ErrorResponse, "description": "Nothing to decide"}},
)
async def approve(user: WriteUser, uid: str = OtherUid) -> Message:
    """`uid` names the student who applied.

    Approving is what makes the enrolment active — and that one state change
    is also what puts the coach's required documents in front of them, because
    every document read is gated on exactly this. There is no separate "send
    the documents" step to forget.
    """
    _require_coach(user.uid)

    if enrolment_repo.decide(user.uid, uid, approve=True) is None:
        raise NotFoundError("No application from them to decide.")

    return Message(message=service.settle(user.uid, uid, as_coach=True))


@router.post(
    "/applications/{uid}/reject",
    response_model=Message,
    summary="Turn an application down",
    responses={404: {"model": ErrorResponse, "description": "Nothing to decide"}},
)
async def reject(user: WriteUser, uid: str = OtherUid) -> Message:
    _require_coach(user.uid)

    if enrolment_repo.decide(user.uid, uid, approve=False) is None:
        raise NotFoundError("No application from them to decide.")

    return Message(message="Application turned down.")


@router.get(
    "/inbox",
    response_model=Inbox,
    summary="Everything the notification bell needs",
)
async def inbox(user: ReadUser) -> Inbox:
    """Both sides at once, because one account can be both.

    Answers empty halves rather than 403-ing a student who has no applications
    — the bell asks the same question for everybody and renders what applies.
    """
    return service.inbox(user.uid)
