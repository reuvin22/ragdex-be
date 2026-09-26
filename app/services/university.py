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

from app.models.repositories import directory as directory_repo
from app.models.repositories import enrolment as enrolment_repo
from app.models.repositories import trades as trades_repo
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
