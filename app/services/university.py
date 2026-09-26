"""Turning enrolments into the things a coaching screen shows.

Routes validate and delegate, repositories persist; the assembling happens
here. That matters more than usual in this family: a roster row is one
enrolment, one directory entry and one journal summary joined together, and
none of those three pieces knows about the other two.

Every figure on a student is computed from their trades by ``stats.summarise``
— the same function the coach and the leak detector use. A client never sends
one and this module never invents one.
"""

from __future__ import annotations

from datetime import datetime

from app.core.errors import AppError
from app.models.repositories import directory as directory_repo
from app.models.repositories import documents as documents_repo
from app.models.repositories import enrolment as enrolment_repo
from app.models.repositories import trades as trades_repo
from app.models.repositories import university_settings as settings_repo
from app.models.schemas.documents import (
    SubmissionList,
    SubmissionWrite,
    UniversityDocument,
)
from app.models.schemas.university import (
    Application,
    CoachSummary,
    Inbox,
    Intake,
    Invitation,
    NextDocument,
    SentInvite,
    StudentSummary,
)
from app.services.stats import summarise

#: How much of a student's journal a roster row is summarised from.
#:
#: A roster costs one journal read per student, so this is the figure that
#: decides whether a coach with thirty students waits a second or ten. Two
#: hundred trades is enough for a win rate and a P&L to be worth reading; the
#: student's own page fetches the journal properly.
ROSTER_SAMPLE = 200


def _summarise_uid(uid: str) -> tuple[dict[str, float | int | None], datetime | None]:
    """One student's headline figures, and when they last logged anything."""
    items, _ = trades_repo.list_trades(uid, limit=ROSTER_SAMPLE)
    summary = summarise(items)

    # list_trades answers newest first, so the head of the page is the latest.
    last = items[0].entry_at if items else None

    score = summary.rule_adherence.score

    return (
        {
            "trade_count": summary.trade_count,
            "closed_count": summary.closed_count,
            "win_rate": summary.win_rate,
            "net_pl": summary.net_pl,
            "rule_score": None if score is None else round(score),
        },
        last,
    )


def roster(coach_uid: str) -> list[StudentSummary]:
    """Every accepted student, with their figures.

    Sorted by who has been quiet longest last — a coach opens this to find who
    needs them, and the person who logged a trade an hour ago is not it.
    """
    rows = enrolment_repo.students_of(coach_uid)
    if not rows:
        return []

    people = directory_repo.get_many([row.student_uid for row in rows])
    students: list[StudentSummary] = []

    for row in rows:
        person = people.get(row.student_uid)
        figures, last = _summarise_uid(row.student_uid)

        students.append(
            StudentSummary(
                uid=row.student_uid,
                display_name=person.display_name if person else "",
                email=person.email if person else "",
                photo_url=person.photo_url if person else "",
                since=row.responded_at or row.invited_at,
                last_trade_at=last,
                **figures,  # type: ignore[arg-type]
            )
        )

    students.sort(key=lambda entry: entry.display_name.lower() or entry.email.lower())
    return students


def coach_of(student_uid: str) -> CoachSummary | None:
    """The one active coach, or none.

    A list under the hood because the store does not forbid two; the first is
    taken because accepting a second invitation is refused at the route. If
    that ever changes this is the place that has to grow a real answer.
    """
    rows = enrolment_repo.coaches_of(student_uid)
    if not rows:
        return None

    row = rows[0]
    person = directory_repo.get_many([row.coach_uid]).get(row.coach_uid)

    return CoachSummary(
        uid=row.coach_uid,
        display_name=person.display_name if person else "",
        email=person.email if person else "",
        photo_url=person.photo_url if person else "",
        since=row.responded_at or row.invited_at,
    )


def pending(student_uid: str) -> list[Invitation]:
    """Invitations waiting on this trader, whoever sent them."""
    rows = enrolment_repo.pending_for_student(student_uid)
    if not rows:
        return []

    people = directory_repo.get_many([row.coach_uid for row in rows])

    return [
        Invitation(
            coach_uid=row.coach_uid,
            coach_name=people[row.coach_uid].display_name
            if row.coach_uid in people
            else "",
            coach_email=people[row.coach_uid].email if row.coach_uid in people else "",
            coach_photo=people[row.coach_uid].photo_url
            if row.coach_uid in people
            else "",
            note=row.note,
            invited_at=row.invited_at,
        )
        for row in rows
    ]


def sent(coach_uid: str) -> list[SentInvite]:
    """What a coach has asked for, answered or not.

    Accepted rows are left out: they are on the roster, and an invitation that
    worked is not outstanding business.
    """
    rows = [row for row in enrolment_repo.sent_by(coach_uid) if row.status != "active"]
    if not rows:
        return []

    people = directory_repo.get_many([row.student_uid for row in rows])

    return [
        SentInvite(
            student_uid=row.student_uid,
            student_name=people[row.student_uid].display_name
            if row.student_uid in people
            else "",
            student_email=people[row.student_uid].email
            if row.student_uid in people
            else "",
            student_photo=people[row.student_uid].photo_url
            if row.student_uid in people
            else "",
            status=row.status,
            note=row.note,
            invited_at=row.invited_at,
            responded_at=row.responded_at,
        )
        for row in rows
    ]


# --------------------------------------------------------------- documents


def documents_for(uid: str) -> list[UniversityDocument]:
    """What this account should see in its documents list.

    A coach sees their own, drafts included, each with a count of who has
    completed it. A student sees their coach's published ones, each marked
    with whether *they* have. The two views never mix: the count is left null
    for a student and the personal timestamp is left null for a coach, so a
    client cannot render one as the other by mistake.
    """
    own = documents_repo.for_coach(uid)
    if own:
        for document in own:
            document.submission_count = len(documents_repo.submitted_uids(document.id))
        return own

    # `current_for_student`, not `coaches_of`: somebody in the middle of
    # signing is not enrolled yet, and they are exactly who needs this list.
    row = enrolment_repo.current_for_student(uid)
    if row is None or row.status not in ("active", "documents"):
        return []

    documents = documents_repo.published_for(row.coach_uid)
    for document in documents:
        existing = documents_repo.submission_for(document.id, uid)
        document.submitted_at = existing.submitted_at if existing else None

    return documents


def check_complete(document: UniversityDocument, payload: SubmissionWrite) -> None:
    """Refuse a submission that is missing something the coach marked required.

    Enforced here as well as in the browser, because the browser is a
    courtesy. An agreement without a signature and a form missing its required
    answers are both submissions that would look complete on a roster and not
    be.
    """
    if document.kind == "agreement":
        if not payload.signed_name.strip():
            raise AppError("Type your name to sign this.", code="unsigned")
        return

    answered = {
        answer.question_id
        for answer in payload.answers
        if answer.value.strip() or answer.values
    }

    missing = [
        question.prompt
        for question in document.questions
        if question.required and question.id not in answered
    ]

    if missing:
        raise AppError(
            f"{len(missing)} required question{'s' if len(missing) > 1 else ''} "
            "still needs an answer.",
            code="incomplete",
        )


def submissions(coach_uid: str, document_id: str) -> SubmissionList:
    """Everything sent back, with names, and who has not sent anything.

    The outstanding list is the useful half — a coach opens this to find who
    has not done it, and a list of the people who have does not answer that.
    """
    entries = documents_repo.submissions_for(document_id)
    roster = enrolment_repo.students_of(coach_uid)

    people = directory_repo.get_many(
        [entry.student_uid for entry in entries] + [row.student_uid for row in roster]
    )

    for entry in entries:
        person = people.get(entry.student_uid)
        if person is not None:
            entry.student_name = person.display_name
            entry.student_email = person.email

    done = {entry.student_uid for entry in entries}
    outstanding = [
        (people[row.student_uid].display_name or people[row.student_uid].email)
        if row.student_uid in people
        else row.student_uid
        for row in roster
        if row.student_uid not in done
    ]

    return SubmissionList(submissions=entries, outstanding=outstanding)


def intake_for(student_uid: str) -> Intake:
    """What to show somebody who followed an invitation link.

    Empty when nothing is open — which is the ordinary case for anybody who
    did not arrive from an email, and is not an error.
    """
    row = enrolment_repo.current_for_student(student_uid)
    if row is None:
        return Intake()

    person = directory_repo.get_many([row.coach_uid]).get(row.coach_uid)
    programme = settings_repo.get_settings(row.coach_uid)
    form = documents_repo.intake_for(row.coach_uid)

    required = documents_repo.required_ids(row.coach_uid)
    outstanding = (
        documents_repo.unsigned_ids(student_uid, required)
        if row.status == "documents"
        else []
    )

    return Intake(
        status=row.status,
        coach_uid=row.coach_uid,
        coach_name=person.display_name if person else "",
        coach_email=person.email if person else "",
        university_name=programme.name,
        note=row.note,
        document_id=form.id if form is not None else "",
        outstanding=len(outstanding),
        required_total=len(required),
    )


def _as_applications(
    rows: list[enrolment_repo.Enrolment], coach_uid: str
) -> list[Application]:
    """Enrolment rows plus who the people are. Shared by the two queues."""
    if not rows:
        return []

    people = directory_repo.get_many([row.student_uid for row in rows])
    form = documents_repo.intake_for(coach_uid)

    return [
        Application(
            student_uid=row.student_uid,
            student_name=people[row.student_uid].display_name
            if row.student_uid in people
            else "",
            student_email=people[row.student_uid].email
            if row.student_uid in people
            else "",
            applied_at=row.applied_at,
            document_id=form.id if form is not None else "",
        )
        for row in rows
    ]


def signing(coach_uid: str) -> list[Application]:
    """Approved students still working through their documents."""
    return _as_applications(enrolment_repo.signing_for(coach_uid), coach_uid)


def applications(coach_uid: str) -> list[Application]:
    """Everyone waiting on this coach, with the form they answered.

    The document id travels with each one so the coach can open the answers
    without a second lookup per applicant.
    """
    rows = enrolment_repo.applications_for(coach_uid)
    if not rows:
        return []

    people = directory_repo.get_many([row.student_uid for row in rows])
    form = documents_repo.intake_for(coach_uid)

    return [
        Application(
            student_uid=row.student_uid,
            student_name=people[row.student_uid].display_name
            if row.student_uid in people
            else "",
            student_email=people[row.student_uid].email
            if row.student_uid in people
            else "",
            applied_at=row.applied_at,
            document_id=form.id if form is not None else "",
        )
        for row in rows
    ]


def settle(coach_uid: str, student_uid: str, *, as_coach: bool = False) -> str:
    """Put a just-approved enrolment in the right state, and say what happened.

    There are two right answers and the difference matters to the person
    reading the message. With required documents outstanding the enrolment is
    not finished — it moves to ``documents`` and the student has something to
    do. With none, approval *is* the end of it and they are enrolled.
    """
    outstanding = documents_repo.unsigned_ids(
        student_uid, documents_repo.required_ids(coach_uid)
    )

    if not outstanding:
        enrolment_repo.set_status(coach_uid, student_uid, "active")
        return "Approved and enrolled." if as_coach else "You have joined the program."


    enrolment_repo.set_status(coach_uid, student_uid, "documents")
    count = len(outstanding)
    noun = "document" if count == 1 else "documents"

    return (
        f"Approved. They have {count} {noun} to sign before they are enrolled."
        if as_coach
        else f"You are approved. Sign {count} {noun} to finish joining."
    )


def complete_if_signed(coach_uid: str, student_uid: str) -> bool:
    """Enrol a student the moment nothing is left to sign.

    Only from ``documents``: a submission from somebody already enrolled is an
    ordinary form being answered, and one from somebody not yet approved
    should not enrol them however much they sign.
    """
    row = enrolment_repo.get(coach_uid, student_uid)
    if row is None or row.status != "documents":
        return False

    if documents_repo.unsigned_ids(student_uid, documents_repo.required_ids(coach_uid)):
        return False

    enrolment_repo.set_status(coach_uid, student_uid, "active")
    return True


def inbox(uid: str) -> Inbox:
    """One answer for the notification bell.

    Both halves are fetched for everybody rather than branching on account
    type: a coach who is also somebody's student has both, and asking the
    profile first would be a read to save two queries that answer empty.
    """
    return Inbox(
        invitations=pending(uid),
        intake=intake_for(uid),
        applications=applications(uid),
        signing=signing(uid),
        declined=[invite for invite in sent(uid) if invite.status == "declined"],
    )


def next_document(student_uid: str) -> NextDocument:
    """Where to send a student next.

    The signing sequence is driven from here rather than from a list in the
    browser, so the order is the same on every device and a half-finished run
    picks up where it stopped rather than starting again.
    """
    row = enrolment_repo.current_for_student(student_uid)
    if row is None or row.status not in ("documents", "active"):
        return NextDocument(done=True)

    required = documents_repo.required_ids(row.coach_uid)
    unsigned = documents_repo.unsigned_ids(student_uid, required)

    if not unsigned:
        return NextDocument(done=True, total=len(required))

    return NextDocument(
        document_id=unsigned[0],
        remaining=len(unsigned),
        total=len(required),
    )
