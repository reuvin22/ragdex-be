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
from app.models.schemas.documents import (
    SubmissionList,
    SubmissionWrite,
    UniversityDocument,
)
from app.models.schemas.university import (
    CoachSummary,
    Invitation,
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

    coaches = enrolment_repo.coaches_of(uid)
    if not coaches:
        return []

    documents = documents_repo.published_for(coaches[0].coach_uid)
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
